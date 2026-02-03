#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM_DEPLOYMENT_GRAPH.py

(repo-wide heuristics + semantic chunking + LLM scoring)
that outputs a SYSTEM-LEVEL DEPLOYMENT ARCHITECTURE (LB/proxy/entrypoints,
replicas, shared state, request-flow arrows).

UPDATED FEATURES:
1) Infers OVERALL ARCHITECTURE TYPE first (microservices/monolith/etc.)
2) Outputs a separate EDGE PROOF JSON:
   - For every connection (edge), stores the exact snippets (text) used as evidence.

Install:
  pip install openai networkx

Optional (PNG render):
  brew install graphviz  # mac
  sudo apt-get install graphviz  # linux

Run:
  export OPENAI_API_KEY="..."
  python LLM_DEPLOYMENT_GRAPH.py https://github.com/GoogleCloudPlatform/microservices-demo -o out_arch
  python LLM_DEPLOYMENT_GRAPH.py /path/to/local/repo -o out_arch
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import networkx as nx 
from openai import OpenAI

PROJECT_NAME = "LLM_DEPLOYMENT_GRAPH"

# ----------------------------
# Utilities
# ----------------------------

def run(cmd: List[str], cwd: Optional[str] = None) -> str:
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDERR:\n{p.stderr}")
    return p.stdout

def is_git_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://") or s.endswith(".git")

def safe_read_text(path: Path, max_bytes: int = 300_000) -> str:
    try:
        data = path.read_bytes()
        if b"\x00" in data[:2000]:
            return ""
        data = data[:max_bytes]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""

def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")

def render_dot(dot_path: Path, fmt: str = "png") -> Optional[Path]:
    out_path = dot_path.with_suffix(f".{fmt}")
    try:
        run(["dot", f"-T{fmt}", str(dot_path), "-o", str(out_path)])
        return out_path
    except Exception:
        return None

def out_path_for(prefix: Path, suffix: str) -> Path:
    """
    All files are named:
      <prefix>_<PROJECT_NAME>_<suffix>
    """
    base = prefix.with_suffix("").as_posix()
    return Path(f"{base}_{PROJECT_NAME}_{suffix}")

# ----------------------------
# File scoring (DeepWiki-like heuristics)
# ----------------------------

ENTRYPOINT_PATTERNS = [
    r"(^|/)(main|app|server|index)\.(py|js|ts|go)$",
    r"(^|/)cmd/.+/main\.go$",
    r"(^|/)src/main/java/.+Application\.java$",
    r"(^|/)wsgi\.py$",
    r"(^|/)manage\.py$",
]

CORE_DIR_PATTERNS = [
    r"^src/",
    r"^lib/",
    r"^services/",
    r"^apps/",
    r"^packages/",
    r"^cmd/",
]

API_PATTERNS = [
    r"\.proto$",
    r"openapi.*\.(yaml|yml|json)$",
    r"swagger.*\.(yaml|yml|json)$",
    r"\.gql$",
    r"\.graphql$",
]

EXCLUDE_DIRS = {
    ".git", "node_modules", "dist", "build", "target",
    ".venv", "venv", "__pycache__", ".idea", ".vscode",
    ".tox", ".mypy_cache"
}

ALLOWED_EXTS = {
    ".py", ".js", ".ts", ".go", ".java", ".kt",
    ".yaml", ".yml", ".json", ".xml", ".md", ".txt",
    ".gradle", ".properties", ".conf", ".ini", ".env"
}

def path_matches(path_str: str, patterns: List[str]) -> bool:
    return any(re.search(p, path_str) for p in patterns)

def score_path(rel: str) -> float:
    s = 0.0
    # Entry points
    if path_matches(rel, ENTRYPOINT_PATTERNS):
        s += 8.0
    # Core dirs
    if path_matches(rel, CORE_DIR_PATTERNS):
        s += 3.0
    # Public APIs / interfaces
    if path_matches(rel, API_PATTERNS):
        s += 7.0

    # Deployment/runtime wiring
    base = os.path.basename(rel)
    low = rel.lower()

    if base in {"docker-compose.yml", "docker-compose.yaml"}:
        s += 8.0
    if base.startswith("dockerfile") or "/dockerfile" in low:
        s += 6.0
    if low.startswith(("k8s/", "kubernetes/", "helm/", "charts/", "deploy/", "manifests/")):
        s += 7.0
    if base in {"skaffold.yaml", "skaffold.yml", "tiltfile", "compose.yaml", "compose.yml"}:
        s += 6.0

    # Reverse proxy / LB hints
    if "nginx" in low or base in {"nginx.conf"} or low.endswith(".conf"):
        s += 4.0
    if "traefik" in low or "haproxy" in low or "envoy" in low:
        s += 4.0

    # App server hints (gunicorn/uvicorn)
    if any(k in low for k in ["gunicorn", "uvicorn", "uwsgi"]):
        s += 4.0

    # Build descriptors
    if base in {"package.json", "pom.xml", "build.gradle", "settings.gradle", "go.mod", "requirements.txt", "pyproject.toml"}:
        s += 4.0

    # Docs that describe deployment
    if rel.lower().endswith(".md") and any(k in low for k in ["readme", "deploy", "architecture", "arch"]):
        s += 3.0

    return s

# ----------------------------
# Import graph proxy (high-connectivity)
# ----------------------------

IMPORT_RE = {
    "py": re.compile(r"^\s*(import\s+[\w\.]+|from\s+[\w\.]+\s+import\s+.+)", re.MULTILINE),
    "js": re.compile(r"^\s*(import\s+.+\s+from\s+['\"].+['\"]|const\s+.+\s*=\s*require\(['\"].+['\"]\))", re.MULTILINE),
    "go": re.compile(r"^\s*import\s*(\(|\".+\")", re.MULTILINE),
    "java": re.compile(r"^\s*import\s+[\w\.]+\s*;", re.MULTILINE),
}

def build_import_graph(repo: Path, files: List[Path]) -> nx.DiGraph:
    g = nx.DiGraph()
    for f in files:
        rel = str(f.relative_to(repo))
        g.add_node(rel)
    for f in files:
        rel = str(f.relative_to(repo))
        ext = f.suffix.lower().lstrip(".")
        txt = safe_read_text(f, max_bytes=120_000)
        if not txt:
            continue
        key = "py" if ext == "py" else "js" if ext in {"js", "ts"} else "go" if ext == "go" else "java" if ext in {"java", "kt"} else None
        if not key or key not in IMPORT_RE:
            continue
        imports = IMPORT_RE[key].findall(txt)[:200]
        for imp in imports:
            tgt = f"__import__:{imp[:120]}"
            g.add_node(tgt)
            g.add_edge(rel, tgt)
    return g

# ----------------------------
# Chunking into semantic snippets
# ----------------------------

@dataclass
class Snippet:
    file: str
    kind: str
    name: str
    start_line: int
    end_line: int
    text: str
    score_hint: float

def chunk_python(text: str) -> List[Tuple[str, str, int, int, str]]:
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*(def|class)\s+([A-Za-z_]\w*)\s*\(?.*:\s*$", lines[i])
        if m:
            kind = m.group(1)
            name = m.group(2)
            start = i
            base_indent = len(lines[i]) - len(lines[i].lstrip(" "))
            i += 1
            while i < len(lines):
                line = lines[i]
                if line.strip() == "":
                    i += 1
                    continue
                indent = len(line) - len(line.lstrip(" "))
                if indent <= base_indent and not line.lstrip().startswith(("#", "@")):
                    break
                i += 1
            end = i - 1
            block = "\n".join(lines[start:i])
            out.append((kind, name, start + 1, end + 1, block))
        else:
            i += 1
    if not out:
        out.append(("toplevel", "toplevel", 1, min(len(lines), 200), "\n".join(lines[:200])))
    return out

def chunk_js_like(text: str) -> List[Tuple[str, str, int, int, str]]:
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.search(r"\b(class)\s+([A-Za-z_]\w*)\b", line)
        if not m:
            m = re.search(r"\bfunction\s+([A-Za-z_]\w*)\b", line)
            if m:
                kind, name = "function", m.group(1)
            else:
                m2 = re.search(r"\b([A-Za-z_]\w*)\s*=\s*\(?.*\)?\s*=>\s*{", line)
                if m2:
                    kind, name = "arrow", m2.group(1)
                else:
                    i += 1
                    continue
        else:
            kind, name = "class", m.group(2)
        start = i
        brace = 0
        while i < len(lines) and "{" not in lines[i]:
            i += 1
        if i < len(lines):
            brace += lines[i].count("{") - lines[i].count("}")
        i += 1
        while i < len(lines) and brace > 0:
            brace += lines[i].count("{") - lines[i].count("}")
            i += 1
        end = min(i, len(lines)) - 1
        out.append((kind, name, start + 1, end + 1, "\n".join(lines[start:i])))
    if not out:
        out.append(("toplevel", "toplevel", 1, min(len(lines), 200), "\n".join(lines[:200])))
    return out

def chunk_go(text: str) -> List[Tuple[str, str, int, int, str]]:
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*func\s+([A-Za-z_]\w*)\s*\(.*", lines[i])
        if m:
            kind, name = "func", m.group(1)
            start = i
            brace = 0
            while i < len(lines) and "{" not in lines[i]:
                i += 1
            if i < len(lines):
                brace += lines[i].count("{") - lines[i].count("}")
            i += 1
            while i < len(lines) and brace > 0:
                brace += lines[i].count("{") - lines[i].count("}")
                i += 1
            end = min(i, len(lines)) - 1
            out.append((kind, name, start + 1, end + 1, "\n".join(lines[start:i])))
        else:
            i += 1
    if not out:
        out.append(("toplevel", "toplevel", 1, min(len(lines), 200), "\n".join(lines[:200])))
    return out

def chunk_java(text: str) -> List[Tuple[str, str, int, int, str]]:
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = re.search(r"\b(class|interface)\s+([A-Za-z_]\w*)\b", lines[i])
        if m:
            kind, name = m.group(1), m.group(2)
            start = i
            brace = 0
            while i < len(lines) and "{" not in lines[i]:
                i += 1
            if i < len(lines):
                brace += lines[i].count("{") - lines[i].count("}")
            i += 1
            while i < len(lines) and brace > 0:
                brace += lines[i].count("{") - lines[i].count("}")
                i += 1
            end = min(i, len(lines)) - 1
            out.append((kind, name, start + 1, end + 1, "\n".join(lines[start:i])))
        else:
            i += 1
    if not out:
        out.append(("toplevel", "toplevel", 1, min(len(lines), 200), "\n".join(lines[:200])))
    return out

def chunk_config(text: str, max_lines: int = 260) -> List[Tuple[str, str, int, int, str]]:
    lines = text.splitlines()
    return [("config", "config", 1, min(len(lines), max_lines), "\n".join(lines[:max_lines]))]

def make_snippets(repo: Path, ranked_files: List[Tuple[str, float]], max_files: int = 100, max_snips_per_file: int = 30) -> List[Snippet]:
    snippets: List[Snippet] = []
    for rel, base_score in ranked_files[:max_files]:
        path = repo / rel
        txt = safe_read_text(path)
        if not txt.strip():
            continue
        ext = path.suffix.lower()
        if ext == ".py":
            chunks = chunk_python(txt)
        elif ext in {".js", ".ts"}:
            chunks = chunk_js_like(txt)
        elif ext == ".go":
            chunks = chunk_go(txt)
        elif ext in {".java", ".kt"}:
            chunks = chunk_java(txt)
        elif ext in {".yaml", ".yml", ".json", ".xml", ".properties", ".gradle", ".conf", ".ini", ".env"} or path.name in {
            "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"
        }:
            chunks = chunk_config(txt)
        else:
            continue

        for (kind, name, sline, eline, block) in chunks[:max_snips_per_file]:
            block = block.strip()
            if len(block) < 40:
                continue
            if len(block) > 7000:
                block = block[:7000] + "\n...<truncated>..."
            snippets.append(Snippet(
                file=rel,
                kind=kind,
                name=name,
                start_line=sline,
                end_line=eline,
                text=block,
                score_hint=base_score
            ))
    return snippets

# ----------------------------
# OpenAI calls (robust JSON)
# ----------------------------

def oai_json(client: OpenAI, model: str, system: str, user: str, max_repair_tries: int = 1) -> dict:
    def _call(prompt: str) -> str:
        resp = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ],
        )
        return resp.output_text.strip()

    text = _call(user)

    for attempt in range(max_repair_tries + 1):
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        if attempt < max_repair_tries:
            repair_system = "You output strict JSON only. Fix invalid JSON without changing meaning."
            repair_user = "Fix the following into valid JSON. Return ONLY JSON.\n\n" + text
            text = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": repair_system},
                    {"role": "user", "content": repair_user}
                ],
            ).output_text.strip()

    raise ValueError(f"Model did not return valid JSON. Got:\n{text[:1200]}")

def score_snippets_with_llm(client: OpenAI, model: str, repo_hint: str, snippets: List[Snippet], take: int = 60) -> List[Dict]:
    packed = []
    for idx, sn in enumerate(snippets):
        body = sn.text
        if len(body) > 900:
            body = body[:900] + "\n...<truncated>..."
        packed.append({
            "id": idx,
            "file": sn.file,
            "kind": sn.kind,
            "name": sn.name,
            "start_line": sn.start_line,
            "end_line": sn.end_line,
            "body": body,
            "score_hint": sn.score_hint
        })

    system = "You are an expert software architect. Score snippets for deployment architecture recovery."
    user = (
        f"Repository context:\n{repo_hint}\n\n"
        "Rank snippets by usefulness for SYSTEM-LEVEL DEPLOYMENT ARCHITECTURE (entry points, LB/proxy, replicas, workers, shared state, request flow).\n"
        "Prefer snippets that mention:\n"
        "- nginx/traefik/envoy/haproxy/load balancer/reverse proxy\n"
        "- docker-compose/k8s/helm/deployment/replicas/autoscaling\n"
        "- gunicorn/uvicorn/workers/threads/processes\n"
        "- redis/db/queue and connection URLs\n"
        "- ports, ingress, service exposure\n\n"
        "Return JSON: {\"ranked\": [{\"id\": <int>, \"score\": 0-100, \"reason\": \"...\"}, ...]}\n"
        "Include at most 140 items.\n\n"
        "Snippets:\n" + json.dumps(packed, ensure_ascii=False)
    )

    out = oai_json(client, model, system, user)
    ranked = out.get("ranked", [])
    ranked.sort(key=lambda x: x.get("score", 0), reverse=True)
    return ranked[:take]
def infer_deployment_arch(client: OpenAI, model: str, repo_hint: str, chosen: List[Snippet]) -> Dict:
    evidence = []
    for sn in chosen:
        evidence.append({
            "file": sn.file,
            "range": f"{sn.start_line}-{sn.end_line}",
            "kind": sn.kind,
            "name": sn.name,
            "text": sn.text
        })

    system = "You are an expert in system-level deployment architecture recovery from repositories."
    user = (
        f"Repository context:\n{repo_hint}\n\n"
        "Infer the SYSTEM-LEVEL DEPLOYMENT ARCHITECTURE (deployment/runtime view), not code-level classes.\n\n"
        "You must:\n"
        "- Identify load balancers / reverse proxies / entry points.\n"
        "- Identify horizontally scalable components and draw multiple instances.\n"
        "- Model runtime worker replication when implied by configs, docker-compose, k8s/helm, docs.\n"
        "- Represent shared state explicitly (DB/cache/queue/object-store/checkpoint store). If the backend is not explicit but\n"
        "  shared state is strongly implied by the system, include a node 'SharedStateStore' with inferred=true and note.\n"
        "- Use arrows to reflect runtime request flow.\n\n"
        "Evidence rules:\n"
        "- Prefer explicit evidence.\n"
        "- If inferred, set inferred=true and explain why in note/replication_assumptions.\n\n"
        "Return ONLY JSON with this schema:\n"
        "{\n"
        '  "architecture_type": "deployment",\n'
        '  "entry_points": ["..."],\n'
        '  "replication_assumptions": ["..."],\n'
        '  "nodes": [\n'
        '    {\n'
        '      "id": "name",\n'
        '      "type": "lb|proxy|service|worker|db|cache|queue|external",\n'
        '      "replicas": 1,\n'
        '      "note": "...",\n'
        '      "inferred": false,\n'
        '      "style": {"fillcolor":"#RRGGBB","color":"#RRGGBB","shape":"box"}\n'
        '    }\n'
        "  ],\n"
        '  "edges": [\n'
        '    {\n'
        '      "from": "name",\n'
        '      "to": "name",\n'
        '      "label": "http|https|grpc|rpc|msg|db|cache|other",\n'
        '      "evidence": [{"file":"...","range":"..."}],\n'
        '      "flow_step": 1,\n'
        '      "style": {"color":"#RRGGBB","penwidth":2,"style":"solid"}\n'
        '    }\n'
        "  ]\n"
        "}\n\n"
        "Evidence snippets:\n" + json.dumps(evidence, ensure_ascii=False)
    )
    return oai_json(client, model, system, user)


def infer_architecture_type(client: OpenAI, model: str, repo_hint: str, chosen: List[Snippet]) -> Dict:
    evidence = []
    for sn in chosen:
        evidence.append({
            "file": sn.file,
            "range": f"{sn.start_line}-{sn.end_line}",
            "kind": sn.kind,
            "name": sn.name,
            "text": sn.text
        })

    system = "You are an expert in software architecture recovery from repositories."
    user = (
        f"Repository context:\n{repo_hint}\n\n"
        "Infer the OVERALL ARCHITECTURE TYPE of this repository at a high level.\n"
        "Choose ONE primary type from (most dominant pattern):\n"
        "- monolith\n"
        "- modular_monolith\n"
        "- microservices\n"
        "- service_oriented\n"
        "- event_driven\n"
        "- layered\n"
        "- serverless\n"
        "- microkernel\n"
        "- pipeline_dataflow\n"
        "- peer_to_peer\n"
        "- distributed_system\n"
        "- hybrid\n"
        "- unknown\n\n"
        "Rules:\n"
        "- Base your decision ONLY on the evidence snippets.\n"
        "- Choose the most dominant architecture pattern even if others also appear.\n"
        "- If multiple styles are strongly present, choose \"hybrid\".\n"
        "- Provide a short rationale and key indicators found in evidence.\n\n"
        "Return ONLY JSON with this schema:\n"
        "{\n"
        '  "architecture_type": "<one_of_the_types_above>",\n'
        '  "confidence": 0-100,\n'
        '  "rationale": "1-3 sentences",\n'
        '  "signals": ["bullet-like short phrases"],\n'
        '  "evidence": [{"file":"...","range":"..."}]\n'
        "}\n\n"
        "Evidence snippets:\n" + json.dumps(evidence, ensure_ascii=False)
    )
    return oai_json(client, model, system, user)


# ----------------------------
# NEW: Edge proof JSON builder (connection proofs)
# ----------------------------

def build_edge_proofs(arch: Dict, chosen: List[Snippet]) -> Dict:
    """
    Returns a separate JSON object that proves each edge using the snippet texts.

    Output schema:
    {
      "project": "LLM_DEPLOYMENT_GRAPH",
      "edges": [
        {
          "from": "...",
          "to": "...",
          "label": "...",
          "flow_step": 1,
          "evidence": [
            {"file":"...","range":"...","snippet_text":"..."}
          ]
        }
      ]
    }
    """
    # Build lookup from (file, range) -> snippet text
    lookup: Dict[Tuple[str, str], str] = {}
    for s in chosen:
        r = f"{s.start_line}-{s.end_line}"
        lookup[(s.file, r)] = s.text

    edges = arch.get("edges", []) or []
    out_edges = []

    for e in edges:
        evs = e.get("evidence", []) or []
        expanded = []
        for ev in evs:
            f = (ev.get("file") or "").strip()
            r = (ev.get("range") or "").strip()
            txt = lookup.get((f, r), "")
            expanded.append({
                "file": f,
                "range": r,
                "snippet_text": txt
            })

        out_edges.append({
            "from": e.get("from"),
            "to": e.get("to"),
            "label": e.get("label"),
            "flow_step": e.get("flow_step"),
            "evidence": expanded
        })

    return {
        "project": PROJECT_NAME,
        "edges": out_edges
    }
def to_dot(arch: Dict) -> str:
    nodes = arch.get("nodes", []) or []
    edges = arch.get("edges", []) or []
    rendered: Dict[str, List[str]] = {}

    # Default styles per node type (eye-catching, consistent)
    TYPE_STYLE = {
        "lb":      {"shape": "hexagon", "fillcolor": "#FFE08A", "color": "#B37B00"},
        "proxy":   {"shape": "box",     "fillcolor": "#FFD6E7", "color": "#B03060"},
        "service": {"shape": "box",     "fillcolor": "#D6F5FF", "color": "#007A99"},
        "worker":  {"shape": "box",     "fillcolor": "#E6FFCC", "color": "#3E8E00"},
        "db":      {"shape": "cylinder","fillcolor": "#E8E0FF", "color": "#5A3DB8"},
        "cache":   {"shape": "component","fillcolor": "#FFF0C2","color": "#B37B00"},
        "queue":   {"shape": "cds",     "fillcolor": "#FFE7D1", "color": "#C05A00"},
        "external":{"shape": "octagon", "fillcolor": "#F2F2F2", "color": "#666666"},
    }

    def esc(s: str) -> str:
        return (s or "").replace('"', "'").strip()

    dot = [
        "digraph LLMDeploymentGraph {",
        "rankdir=LR;",
        'graph [bgcolor="white", fontname="Helvetica", fontsize=18, labelloc="t", label="LLM Deployment Graph"];',
        'node  [fontname="Helvetica", fontsize=11, style="rounded,filled", penwidth=1.6];',
        'edge  [fontname="Helvetica", fontsize=10, arrowsize=0.9, penwidth=1.4];',
        ""
    ]

    # --- Cluster helpers (grouping makes it look like a system) ---
    def cluster_open(cid: str, label: str) -> None:
        dot.append(f"subgraph cluster_{cid} {{")
        dot.append('  style="rounded";')
        dot.append('  color="#DDDDDD";')
        dot.append('  penwidth=1.2;')
        dot.append(f'  label="{label}";')
        dot.append('  fontname="Helvetica";')
        dot.append('  fontsize=12;')

    def cluster_close() -> None:
        dot.append("}")
        dot.append("")

    # Assign clusters by type (simple + effective)
    CLUSTER_OF = {
        "lb": "ingress",
        "proxy": "ingress",
        "service": "control",
        "worker": "workers",
        "db": "state",
        "cache": "state",
        "queue": "state",
        "external": "security",
    }
    CLUSTER_LABEL = {
        "ingress": "Ingress / Entry",
        "control": "Control Plane / Services",
        "workers": "Workers / Executors",
        "state": "State / Storage",
        "security": "Security / External",
        "misc": "Other"
    }

    # Bucket nodes
    buckets: Dict[str, List[Dict]] = {k: [] for k in CLUSTER_LABEL.keys()}
    for n in nodes:
        t = esc(n.get("type") or "service")
        cid = CLUSTER_OF.get(t, "misc")
        buckets[cid].append(n)

    # Render nodes by clusters
    for cid, label in CLUSTER_LABEL.items():
        if not buckets[cid]:
            continue
        cluster_open(cid, label)

        for n in buckets[cid]:
            nid = esc(n.get("id"))
            if not nid:
                continue

            t = esc(n.get("type") or "service")
            reps = int(n.get("replicas", 1) or 1)
            note = esc(n.get("note") or "")
            inferred = bool(n.get("inferred", False))

            # Style priority: LLM-provided style overrides defaults
            base_style = TYPE_STYLE.get(t, TYPE_STYLE["service"]).copy()
            llm_style = n.get("style") or {}
            for k, v in llm_style.items():
                if v is not None:
                    base_style[k] = v

            shape = base_style.get("shape", "box")
            fillcolor = base_style.get("fillcolor", "#FFFFFF")
            color = base_style.get("color", "#444444")

            # inferred nodes get dashed border
            border_style = "dashed" if inferred else "solid"
            # Put note + inferred marker into label
            label_lines = [f"{nid}", f"({t})"]
            if inferred:
                label_lines.append("[inferred]")
            if note:
                label_lines.append(note[:80])

            def emit(node_name: str, label_txt: str) -> None:
                dot.append(
                    f'  "{node_name}" [shape="{shape}", fillcolor="{fillcolor}", color="{color}", '
                    f'style="rounded,filled,{border_style}", label="{label_txt}"];'
                )

            rendered_ids = []
            if reps <= 1:
                emit(nid, "\\n".join(label_lines))
                rendered_ids.append(nid)
            else:
                # Keep your distributed naming, but still render each replica
                for i in range(1, reps + 1):
                    inst = f"{nid}[{i}/{reps}]"
                    inst_label = "\\n".join([f"{nid} [{i}/{reps}]", f"({t})"] + (["[inferred]"] if inferred else []) + ([note[:80]] if note else []))
                    emit(inst, inst_label)
                    rendered_ids.append(inst)

            rendered[nid] = rendered_ids

        cluster_close()

    def expand(node_id: str) -> List[str]:
        node_id = esc(node_id)
        return rendered.get(node_id, [node_id])

    # Render edges (flow ordered)
    edges_sorted = sorted(edges, key=lambda e: e.get("flow_step", 10**9))

    for e in edges_sorted:
        a = esc(e.get("from"))
        b = esc(e.get("to"))
        if not a or not b:
            continue

        lab = esc(e.get("label") or "other")

        # Edge styling
        llm_es = e.get("style") or {}
        ecolor = llm_es.get("color", "#444444")
        penwidth = llm_es.get("penwidth", 1.6)
        estyle = llm_es.get("style", "solid")

        for aa in expand(a):
            for bb in expand(b):
                dot.append(
                    f'"{aa}" -> "{bb}" [label="{lab}", color="{ecolor}", penwidth={penwidth}, style="{estyle}"];'
                )

    dot.append("}")
    return "\n".join(dot)


# ----------------------------
# Main
# ----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", help="GitHub URL or local repo path")
    ap.add_argument("-o", "--out", default="llm_deployment_graph", help="Output prefix")
    ap.add_argument("--model", default="gpt-4.1-mini", help="OpenAI model name")
    ap.add_argument("--max_files", type=int, default=140, help="Max files to consider after ranking")
    ap.add_argument("--max_snips", type=int, default=70, help="How many snippets to keep after LLM scoring")
    args = ap.parse_args()

    out_prefix = Path(args.out)
    tmpdir = None

    # Acquire repo
    if is_git_url(args.repo):
        tmpdir = Path(tempfile.mkdtemp(prefix="dw_repo_"))
        print(f"⏬ Cloning {args.repo} ...")
        run(["git", "clone", "--depth", "1", args.repo, str(tmpdir)])
        repo_path = tmpdir
    else:
        repo_path = Path(args.repo).expanduser().resolve()
        if not repo_path.exists():
            raise FileNotFoundError(f"Repo path not found: {repo_path}")
        if not (repo_path / ".git").exists():
            print("⚠️ Not a git repo (no .git). Will traverse filesystem anyway.")

    print(f"📁 Repo: {repo_path}")

    # List files
    all_files: List[Path] = []
    if (repo_path / ".git").exists():
        try:
            ls = run(["git", "ls-files"], cwd=str(repo_path))
            for line in ls.splitlines():
                if line.strip():
                    all_files.append(repo_path / line.strip())
        except Exception:
            all_files = []

    if not all_files:
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fn in files:
                all_files.append(Path(root) / fn)

    # Filter to text-ish & relevant types
    kept: List[Path] = []
    for p in all_files:
        rel = str(p.relative_to(repo_path))
        parts = rel.split("/")
        if any(part in EXCLUDE_DIRS for part in parts):
            continue
        if p.suffix.lower() in ALLOWED_EXTS or p.name in {
            "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
            "compose.yml", "compose.yaml", "Makefile",
            "requirements.txt", "go.mod", "pom.xml",
            "build.gradle", "settings.gradle", "pyproject.toml"
        }:
            kept.append(p)

    # Score paths
    scored: List[Tuple[str, float]] = []
    for p in kept:
        rel = str(p.relative_to(repo_path))
        scored.append((rel, score_path(rel)))

    # Connectivity bonus
    code_files = [repo_path / rel for rel, _ in scored if rel.endswith((".py", ".js", ".ts", ".go", ".java", ".kt"))]
    g = build_import_graph(repo_path, code_files)
    file_nodes = [n for n in g.nodes if not str(n).startswith("__import__:")]
    outdeg = {n: g.out_degree(n) for n in file_nodes}
    max_out = max(outdeg.values()) if outdeg else 1

    ranked: List[Tuple[str, float]] = []
    for rel, base in scored:
        bonus = 0.0
        if rel in outdeg:
            bonus = 5.0 * (outdeg[rel] / max_out)
        ranked.append((rel, base + bonus))
    ranked.sort(key=lambda x: x[1], reverse=True)

    # Chunk into candidate snippets
    print("🧩 Chunking candidate files into semantic snippets ...")
    candidates = make_snippets(repo_path, ranked, max_files=args.max_files)

    # LLM relevance scoring
    client = OpenAI()
    repo_hint = f"repo_root={repo_path.name}; file_count={len(kept)}; top_paths={[r for r,_ in ranked[:20]]}"
    print(f"🧠 LLM scoring {len(candidates)} snippets for deployment relevance ...")
    ranked_ids = score_snippets_with_llm(client, args.model, repo_hint, candidates, take=args.max_snips)

    chosen: List[Snippet] = []
    used_ids = set()
    for item in ranked_ids:
        sid = int(item["id"])
        if sid in used_ids:
            continue
        used_ids.add(sid)
        chosen.append(candidates[sid])

    # Save selected snippets
    snippets_path = out_path_for(out_prefix, "snippets.json")
    write_json(snippets_path, [{
        "file": s.file,
        "kind": s.kind,
        "name": s.name,
        "start_line": s.start_line,
        "end_line": s.end_line,
        "score_hint": s.score_hint,
        "text": s.text
    } for s in chosen])
    print(f"✅ Saved selected snippets: {snippets_path}")

    # Infer architecture TYPE first
    print("🧭 Inferring OVERALL ARCHITECTURE TYPE ...")
    arch_type = infer_architecture_type(client, args.model, repo_hint, chosen)
    arch_type_path = out_path_for(out_prefix, "arch_type.json")
    write_json(arch_type_path, arch_type)
    print(f"✅ Saved architecture type JSON: {arch_type_path}")
    print(f"🏷️ Architecture type: {arch_type.get('architecture_type')} (confidence={arch_type.get('confidence')})")

    # Infer deployment architecture
    print("🏗️ Inferring SYSTEM-LEVEL DEPLOYMENT architecture ...")
    arch = infer_deployment_arch(client, args.model, repo_hint, chosen)

    # Attach overall architecture type result into final deployment JSON
    arch["overall_architecture_type"] = arch_type
    arch["project_name"] = PROJECT_NAME

    arch_path = out_path_for(out_prefix, "arch.json")
    write_json(arch_path, arch)
    print(f"✅ Saved architecture JSON: {arch_path}")

    # NEW: Save edge proofs JSON (proof snippets per connection)
    edge_proofs = build_edge_proofs(arch, chosen)
    edge_proofs_path = out_path_for(out_prefix, "edge_proofs.json")
    write_json(edge_proofs_path, edge_proofs)
    print(f"✅ Saved EDGE PROOFS JSON: {edge_proofs_path}")

    # DOT + PNG
    dot_text = to_dot(arch)
    dot_path = out_path_for(out_prefix, "diagram.dot")
    dot_path.write_text(dot_text, encoding="utf-8")
    print(f"✅ Saved DOT: {dot_path}")

    png_path = render_dot(dot_path, fmt="png")
    if png_path:
        print(f"🖼️ Rendered PNG: {png_path}")
    else:
        print("ℹ️ Graphviz 'dot' not found or render failed. DOT file is still created.")

    if tmpdir is not None:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    main()
def run_from_web(repo_url: str, out_prefix: str):
    """
    Web-safe entry point.
    Bypasses argparse and calls the same logic.
    """
    import sys

    old_argv = sys.argv
    try:
        sys.argv = [
            "LLM_DEPLOYMENT_GRAPH.py",
            repo_url,
            "-o", out_prefix,
        ]
        main()
    finally:
        sys.argv = old_argv

