"""Mithilai Classic → Kinetic Migration Assessor – web app."""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import analyzer, outputs
from .parser import Customisation, parse_upload, read_reference_sample

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrator")

JOBS_DIR = Path(os.getenv("JOBS_DIR", Path(tempfile.gettempdir()) / "mithilai-migrator-jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS_PER_JOB", "300"))
PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))
JOB_TTL_HOURS = int(os.getenv("JOB_TTL_HOURS", "72"))
APP_USER = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

app = FastAPI(title="Mithilai Kinetic Migration Assessor")
security = HTTPBasic(auto_error=False)
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=PARALLEL)

if not APP_PASSWORD:
    log.warning("APP_PASSWORD is not set – the app is open to anyone who has the link.")


def require_login(creds: HTTPBasicCredentials | None = Depends(security)) -> str:
    if not APP_PASSWORD:
        return "anonymous"
    ok = creds is not None and secrets.compare_digest(creds.username, APP_USER) and \
        secrets.compare_digest(creds.password, APP_PASSWORD)
    if not ok:
        raise HTTPException(401, "Login required", headers={"WWW-Authenticate": "Basic"})
    return creds.username


def _public(job: dict) -> dict:
    items = []
    for i in job["items"]:
        a = i.get("analysis")
        items.append({
            "id": i["id"], "name": i["name"], "form": i["form"], "source_file": i["source_file"],
            "status": i["status"], "error": i.get("error"), "warnings": i.get("warnings", []),
            "bucket": a["bucket"] if a else None, "confidence": a["confidence"] if a else None,
            "hours": outputs.total_hours(a) if a else None, "files": len(a["files"]) if a else 0,
            "reused": i.get("reused"),
        })
    done = sum(i["status"] in ("done", "failed") for i in job["items"])
    return {
        "id": job["id"], "client": job["profile"].get("Client"), "created": job["created"],
        "status": job["status"], "total": len(job["items"]), "done": done, "items": items,
        "usage": job["usage"], "cost_usd": round(job["cost_usd"], 4),
        "samples": [n for n, _ in job["samples"]], "model": analyzer.MODEL,
    }


# ---- Stored results: an identical request (same code, client profile, rules, model and effort)
# ---- is answered from disk instead of paying for a new analysis.

def _cache_path(key: str) -> Path:
    return JOBS_DIR / "_cache" / f"{key}.json"


def _cache_get(key: str) -> dict | None:
    path = _cache_path(key)
    try:
        if time.time() - path.stat().st_mtime > JOB_TTL_HOURS * 3600:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_put(key: str, analysis: dict) -> None:
    path = _cache_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(analysis), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.warning("Could not store result %s", key)


def _cleanup_old_jobs() -> None:
    cutoff = time.time() - JOB_TTL_HOURS * 3600
    with LOCK:
        for job_id in [j for j, job in JOBS.items() if job["created"] < cutoff and job["status"] != "running"]:
            shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
            JOBS.pop(job_id, None)
    for path in (JOBS_DIR / "_cache").glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _record(item: dict) -> Customisation:
    return Customisation(**{k: item[k] for k in
                            ("id", "name", "form", "source_file", "script", "ui_context", "metadata", "warnings")})


def _finish_item(job: dict, item: dict, analysis: dict) -> None:
    item["analysis"] = analysis
    try:
        outputs.write_item_folder(JOBS_DIR / job["id"] / "output" / "customisations", item)
    except Exception:
        item.pop("analysis", None)  # a failed item must not look analysed to duplicates or reports
        raise
    item["status"] = "done"


def _analyse_item(job: dict, item: dict, system_prompt: str) -> None:
    item["status"] = "running"
    record = _record(item)
    try:
        key = analyzer.request_key(record, system_prompt)
        stored = _cache_get(key)
        if stored is not None:
            item["reused"] = "stored result"
            _finish_item(job, item, stored)
            return
        analysis, usage = analyzer.analyse(record, system_prompt)
        cost = analyzer.estimate_cost(usage) or 0.0
        with LOCK:  # count the paid call before anything else can fail
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                job["usage"][k] = job["usage"].get(k, 0) + usage[k]
            job["cost_usd"] += cost
        _finish_item(job, item, analysis)
        _cache_put(key, analysis)  # store only results that were written out successfully
    except analyzer.AnalysisError as exc:
        item["status"], item["error"] = "failed", str(exc)
    except Exception as exc:  # keep the job going; record the failure on the item
        log.exception("Unexpected error analysing %s", item["name"])
        item["status"], item["error"] = "failed", f"Unexpected error: {exc}"


def _copy_from(job: dict, dup: dict, lead: dict) -> None:
    """A customisation with exactly the same code as another in this job reuses its analysis."""
    try:
        if lead.get("analysis"):
            dup["reused"] = f"same code as {lead['name']}"
            dup["warnings"] = dup["warnings"] + [
                f"Same Classic code and UI changes as '{lead['name']}' – its analysis was reused at no cost. "
                "Generated files mention that customisation's name."]
            _finish_item(job, dup, lead["analysis"])
        else:
            dup["status"] = "failed"
            dup["error"] = f"Same code as '{lead['name']}', whose analysis failed: {lead.get('error')}"
    except Exception as exc:
        log.exception("Could not reuse analysis for %s", dup["name"])
        dup["status"], dup["error"] = "failed", f"Unexpected error: {exc}"


def _run_job(job: dict) -> None:
    system_prompt = analyzer.build_system_prompt(job["profile"], job["samples"])
    items = job["items"]
    try:
        groups: dict[str, list[dict]] = {}
        for it in items:
            groups.setdefault(analyzer.content_key(_record(it)), []).append(it)
        leaders = [group[0] for group in groups.values()]
        # First item alone warms the prompt cache for the shared rules + client profile.
        if leaders:
            _analyse_item(job, leaders[0], system_prompt)
        list(EXECUTOR.map(lambda it: _analyse_item(job, it, system_prompt), leaders[1:]))
        for group in groups.values():
            for dup in group[1:]:
                _copy_from(job, dup, group[0])
        out = JOBS_DIR / job["id"] / "output"
        out.mkdir(parents=True, exist_ok=True)
        outputs.write_inventory(out / "inventory.xlsx", job)
        outputs.write_client_report(out / "client_report.docx", job)
        outputs.build_zip(JOBS_DIR / job["id"], JOBS_DIR / job["id"] / "results.zip")
        job["status"] = "completed" if any(i["status"] == "done" for i in items) else "failed"
    except Exception:
        log.exception("Job %s failed", job["id"])
        job["status"] = "failed"


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": analyzer.MODEL}


@app.get("/", response_class=HTMLResponse)
def index(_: str = Depends(require_login)) -> HTMLResponse:
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8"))


@app.post("/api/jobs")
async def create_job(
    client: str = Form(...),
    current_version: str = Form(""),
    target_version: str = Form(""),
    naming_prefix: str = Form(""),
    hourly_rate: str = Form(""),
    requirements: str = Form(""),
    files: list[UploadFile] = File(...),
    samples: list[UploadFile] | None = File(None),
    _: str = Depends(require_login),
) -> dict:
    _cleanup_old_jobs()
    records: list[Customisation] = []
    errors: list[str] = []
    total_bytes = 0
    for upload in files:
        data = await upload.read()
        total_bytes += len(data)
        if total_bytes > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(413, f"Uploads exceed {MAX_UPLOAD_MB} MB.")
        try:
            records.extend(parse_upload(upload.filename or "upload.xml", data))
        except Exception as exc:
            errors.append(f"{upload.filename}: {exc}")
    if not records:
        raise HTTPException(400, "No customisations found in the uploaded files. " + " ".join(errors))
    if len(records) > MAX_ITEMS:
        raise HTTPException(400, f"{len(records)} customisations found; the limit per job is {MAX_ITEMS}.")

    sample_texts = []
    for upload in samples or []:
        if upload.filename:
            sample_texts.append(read_reference_sample(upload.filename, await upload.read()))

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "created": time.time(),
        "status": "running",
        "profile": {
            "Client": client.strip(),
            "Current version": current_version.strip(),
            "Target version": target_version.strip(),
            "Naming prefix": naming_prefix.strip(),
            "Hourly rate": hourly_rate.strip(),
            "Requirements and preferences": requirements.strip(),
        },
        "samples": sample_texts,
        "usage": {},
        "cost_usd": 0.0,
        "items": [{**r.__dict__, "status": "queued"} for r in records],
        "parse_errors": errors,
    }
    (JOBS_DIR / job_id / "output").mkdir(parents=True, exist_ok=True)
    with LOCK:
        JOBS[job_id] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    log.info("Job %s: %d customisations for %s", job_id, len(records), client)
    return {"id": job_id, "customisations": len(records), "parse_errors": errors}


@app.get("/api/jobs")
def list_jobs(_: str = Depends(require_login)) -> list[dict]:
    with LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
    return [{k: v for k, v in _public(j).items() if k != "items"} for j in jobs]


def _get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found (jobs are kept in memory and cleared when the app restarts).")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, _: str = Depends(require_login)) -> dict:
    job = _get_job(job_id)
    return {**_public(job), "parse_errors": job["parse_errors"]}


@app.get("/api/jobs/{job_id}/items/{item_id}")
def item_detail(job_id: str, item_id: str, _: str = Depends(require_login)) -> dict:
    job = _get_job(job_id)
    item = next((i for i in job["items"] if i["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Customisation not found.")
    detail = {k: item.get(k) for k in ("id", "name", "form", "source_file", "status", "error", "warnings", "script")}
    if item.get("analysis"):
        detail["analysis"] = item["analysis"]
        detail["analysis_md"] = outputs.analysis_markdown(item)
        detail["test_steps_md"] = outputs.test_steps_markdown(item)
    return detail


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, _: str = Depends(require_login)) -> FileResponse:
    job = _get_job(job_id)
    zip_path = JOBS_DIR / job_id / "results.zip"
    if not zip_path.exists():
        raise HTTPException(409, "Results are not ready yet.")
    name = outputs.safe_name(f"{job['profile']['Client']}_Kinetic_Migration", "Kinetic_Migration")
    return FileResponse(zip_path, filename=f"{name}.zip", media_type="application/zip")
