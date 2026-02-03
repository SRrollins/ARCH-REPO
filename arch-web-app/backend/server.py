import os
import uuid
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from LLM_DEPLOYMENT_GRAPH import run_from_web

app = FastAPI()
OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

@app.post("/run")
def run(repo_url: str, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    prefix = os.path.join(OUTPUT_DIR, job_id)

    background_tasks.add_task(run_from_web, repo_url, prefix)
    return {"job_id": job_id}

@app.get("/status/{job_id}")
def status(job_id: str):
    return {
        "done": os.path.exists(f"{OUTPUT_DIR}/{job_id}_LLM_DEPLOYMENT_GRAPH_arch.json")
    }

@app.get("/result/{job_id}/{kind}")
def result(job_id: str, kind: str):
    file_map = {
        "arch": f"{job_id}_LLM_DEPLOYMENT_GRAPH_arch.json",
        "type": f"{job_id}_LLM_DEPLOYMENT_GRAPH_arch_type.json",
        "snippets": f"{job_id}_LLM_DEPLOYMENT_GRAPH_snippets.json",
        "edges": f"{job_id}_LLM_DEPLOYMENT_GRAPH_edge_proofs.json",
        "png": f"{job_id}_LLM_DEPLOYMENT_GRAPH_diagram.png",
        "dot": f"{job_id}_LLM_DEPLOYMENT_GRAPH_diagram.dot",
    }

    if kind not in file_map:
        return JSONResponse({"error": "Invalid result type"}, status_code=400)

    path = os.path.join(OUTPUT_DIR, file_map[kind])
    if not os.path.exists(path):
        return JSONResponse({"error": "Not ready"}, status_code=404)

    return FileResponse(path)
