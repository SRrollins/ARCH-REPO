// ===============================
// CONFIG
// ===============================
const API_BASE = "https://arch-repoo.onrender.com"; 
// ⬆️ replace with your actual Render backend URL

let viz = new Viz();
let currentSVG = null;

// ===============================
// RUN PIPELINE
// ===============================
async function run() {
  const repo = document.getElementById("repo").value.trim();
  if (!repo) {
    alert("Please enter a repository URL");
    return;
  }

  document.getElementById("status").innerText =
    "Waking up server (may take ~30 seconds)...";

  try {
    const res = await fetch(
      `${API_BASE}/run?repo_url=${encodeURIComponent(repo)}`,
      { method: "POST" }
    );

    if (!res.ok) {
      throw new Error(`Run failed: ${res.status}`);
    }

    const data = await res.json();
    poll(data.job_id);

  } catch (err) {
    console.error(err);
    document.getElementById("status").innerText =
      "Failed to connect to backend.";
  }
}

// ===============================
// POLL STATUS
// ===============================
async function poll(job) {
  try {
    const res = await fetch(`${API_BASE}/status/${job}`);
    if (!res.ok) throw new Error("Status error");

    const s = await res.json();

    if (!s.ready) {
      document.getElementById("status").innerText =
        "Running pipeline...";
      setTimeout(() => poll(job), 2000);
      return;
    }

    document.getElementById("status").innerText =
      "Rendering diagram...";
    render(job);

  } catch (err) {
    console.error(err);
    document.getElementById("status").innerText =
      "Waiting for backend...";
    setTimeout(() => poll(job), 3000);
  }
}

// ===============================
// RENDER DOT → SVG
// ===============================
async function render(job) {
  try {
    const dotRes = await fetch(`${API_BASE}/result/${job}/dot`);
    if (!dotRes.ok) throw new Error("DOT fetch failed");

    const dot = await dotRes.text();

    currentSVG = await viz.renderSVGElement(dot);

    const container = document.getElementById("diagram");
    container.innerHTML = "";
    container.appendChild(currentSVG);

    document.getElementById("status").innerText = "Done ✅";

  } catch (err) {
    console.error(err);
    document.getElementById("status").innerText =
      "Graphviz render failed.";
  }
}

// ===============================
// DOWNLOAD SVG
// ===============================
function downloadSVG() {
  if (!currentSVG) return;

  const blob = new Blob(
    [currentSVG.outerHTML],
    { type: "image/svg+xml;charset=utf-8" }
  );
  save(blob, "architecture.svg");
}

// ===============================
// DOWNLOAD PNG (CLIENT-SIDE)
// ===============================
function downloadPNG() {
  if (!currentSVG) return;

  const svgData =
    new XMLSerializer().serializeToString(currentSVG);

  const svgBlob = new Blob(
    [svgData],
    { type: "image/svg+xml;charset=utf-8" }
  );

  const url = URL.createObjectURL(svgBlob);
  const img = new Image();

  img.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = img.width;
    canvas.height = img.height;

    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);

    canvas.toBlob(blob => {
      save(blob, "architecture.png");
      URL.revokeObjectURL(url);
    });
  };

  img.src = url;
}

// ===============================
// SAVE FILE
// ===============================
function save(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
