const API = "https://arch-repo.onrender.com"; // <-- replace

async function run() {
  const repo = document.getElementById("repo").value;
  const r = await fetch(`${API}/run?repo_url=${encodeURIComponent(repo)}`, {
    method: "POST"
  });
  const j = await r.json();
  document.getElementById("out").textContent =
    "Job ID: " + j.job_id;
}
