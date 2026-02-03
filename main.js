const RENDER_URL = "https://arch-repoo.onrender.com";

let currentDot = "";

async function run() {
  const repo = document.getElementById("repo").value;
  document.getElementById("status").innerText = "Running pipeline...";

  const res = await fetch(`${RENDER_URL}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_url: repo })
  });

  const data = await res.json();
  const jobId = data.job_id;

  poll(jobId);
}

async function poll(jobId) {
  document.getElementById("status").innerText = "Processing repository...";

  const res = await fetch(`${RENDER_URL}/result/${jobId}/dot`);
  if (!res.ok) {
    setTimeout(() => poll(jobId), 2000);
    return;
  }

  currentDot = await res.text();
  renderDot(currentDot);
  document.getElementById("status").innerText = "Done ✅";
}

function renderDot(dot) {
  const viz = new Viz();
  viz.renderSVGElement(dot)
    .then(svg => {
      const container = document.getElementById("diagram");
      container.innerHTML = "";
      container.appendChild(svg);
    })
    .catch(err => {
      document.getElementById("status").innerText = err.toString();
    });
}

function downloadSVG() {
  const blob = new Blob([currentDot], { type: "image/svg+xml" });
  download(blob, "architecture.svg");
}

function downloadPNG() {
  const viz = new Viz();
  viz.renderImageElement(currentDot)
    .then(img => {
      fetch(img.src)
        .then(res => res.blob())
        .then(blob => download(blob, "architecture.png"));
    });
}

function download(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
}
