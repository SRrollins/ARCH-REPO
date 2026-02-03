let viz = new Viz();
let currentSVG = null;

async function run() {
  const repo = document.getElementById("repo").value;
  document.getElementById("status").innerText = "Running pipeline...";

  const res = await fetch("/run?repo_url=" + encodeURIComponent(repo), {
    method: "POST"
  });
  const data = await res.json();
  poll(data.job_id);
}

async function poll(job) {
  const s = await fetch(`/status/${job}`).then(r => r.json());
  if (!s.ready) {
    setTimeout(() => poll(job), 2000);
    return;
  }
  document.getElementById("status").innerText = "Rendering diagram...";
  render(job);
}

async function render(job) {
  const dot = await fetch(`/result/${job}/dot`).then(r => r.text());
  currentSVG = await viz.renderSVGElement(dot);
  document.getElementById("diagram").innerHTML = "";
  document.getElementById("diagram").appendChild(currentSVG);
}

function downloadSVG() {
  const blob = new Blob([currentSVG.outerHTML], {type: "image/svg+xml"});
  save(blob, "architecture.svg");
}

function downloadPNG() {
  const canvas = document.createElement("canvas");
  const img = new Image();
  const svgData = new XMLSerializer().serializeToString(currentSVG);
  const url = URL.createObjectURL(new Blob([svgData], {type: "image/svg+xml"}));

  img.onload = () => {
    canvas.width = img.width;
    canvas.height = img.height;
    canvas.getContext("2d").drawImage(img, 0, 0);
    canvas.toBlob(b => save(b, "architecture.png"));
  };
  img.src = url;
}

function save(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
}
