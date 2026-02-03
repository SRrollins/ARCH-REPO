from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
import uuid, subprocess, os, json
from api import router  # wherever your endpoints are

app = FastAPI()
app.include_router(router)
app = FastAPI()
OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)


@app.post("/run")
def run_pipeline(repo_url: str, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())
    bg.add_task(run_job, repo_url, job_id)
    return {"job_id": job_id}


def run_job(repo_url: str, job_id: str):
    out_prefix = f"{OUTPUT_DIR}/{job_id}"
    subprocess.run([
        "python",
        "pipeline_runner.py",
        repo_url,
        "-o",
        out_prefix
    ], check=True)


@app.get("/status/{job_id}")
def status(job_id: str):
    dot = f"{OUTPUT_DIR}/{job_id}_LLM_DEPLOYMENT_GRAPH_diagram.dot"
    return {"ready": os.path.exists(dot)}


@app.get("/result/{job_id}/dot")
def get_dot(job_id: str):
    path = f"{OUTPUT_DIR}/{job_id}_LLM_DEPLOYMENT_GRAPH_diagram.dot"
    return FileResponse(path, media_type="text/plain")


@app.get("/result/{job_id}/arch")
def get_arch(job_id: str):
    path = f"{OUTPUT_DIR}/{job_id}_LLM_DEPLOYMENT_GRAPH_arch.json"
    return FileResponse(path, media_type="application/json")


@app.get("/result/{job_id}/proofs")
def get_proofs(job_id: str):
    path = f"{OUTPUT_DIR}/{job_id}_LLM_DEPLOYMENT_GRAPH_edge_proofs.json"
    return FileResponse(path, media_type="application/json")
