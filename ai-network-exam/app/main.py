"""FastAPI backend: upload -> AI analysis -> review/resolve -> validate -> approve -> Kafka -> job monitor.

The backend never changes router configuration. It only reads routers (validate/discover);
all configuration changes go through Kafka and worker.py.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ValidationError

from app import config, store
from app.ai import AIError, analyze_image
from app.ifnames import normalize_interface
from app.ios import config_digest, render_plan
from app.models import TopologyPlan
from app.validation import discover_interfaces, validate_plan

log = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="AI Network Automation (OSPF)")
STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
MAX_UPLOAD = 5 * 1024 * 1024
MAX_PIXELS = 20_000_000
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

_producer = None


# ------------------------------------------------------------------ helpers
def _plan_or_404(plan_id: str) -> dict:
    plan = store.load("plan", plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    return plan


def _job_or_404(job_id: str) -> dict:
    job = store.load("job", job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _topology(plan: dict) -> TopologyPlan:
    return TopologyPlan.model_validate(plan["topology"])


def _topology_sha(topology: dict) -> str:
    return hashlib.sha256(json.dumps(topology, sort_keys=True).encode()).hexdigest()


def _require_admin(token: str | None):
    if ADMIN_TOKEN and not hmac.compare_digest(token or "", ADMIN_TOKEN):
        raise HTTPException(401, "Administrator token required (X-Admin-Token)")


def _require_worker(token: str | None):
    if not hmac.compare_digest(token or "", config.worker_token()):
        raise HTTPException(401, "Invalid worker token")


def _event(job: dict, status: str, stage: str, message: str = "", device: str | None = None):
    job.setdefault("history", []).append(
        {"at": store.now(), "status": status, "stage": stage, "device": device, "message": message})


def _producer_get():
    global _producer
    if _producer is None:
        from confluent_kafka import Producer

        _producer = Producer({"bootstrap.servers": config.KAFKA_BOOTSTRAP_SERVERS, "acks": "all",
                              "enable.idempotence": True, "message.timeout.ms": 15000})
    return _producer


def _public_plan(plan: dict) -> dict:
    return {k: v for k, v in plan.items() if k not in ("image_path",)}


# ------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
def page_index():
    return FileResponse(STATIC / "index.html")


@app.get("/jobs", response_class=HTMLResponse)
def page_jobs():
    return FileResponse(STATIC / "jobs.html")


@app.get("/health")
def health():
    inv = config.load_inventory()
    return {"status": "ok", "model": config.GROQ_MODEL, "topic": config.KAFKA_TOPIC,
            "kafka": config.KAFKA_BOOTSTRAP_SERVERS, "routers": {n: r.host for n, r in inv.items()},
            "admin_token_required": bool(ADMIN_TOKEN), "groq_key_set": bool(config.GROQ_API_KEY),
            "ssh_credentials_set": bool(config.SSH_USERNAME and config.SSH_PASSWORD)}


@app.get("/api/inventory")
def inventory():
    return {n: r.as_dict() for n, r in config.load_inventory().items()}


# ------------------------------------------------------------------ analysis
@app.post("/api/analyze")
async def analyze(image: UploadFile = File(...)):
    data = await image.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Image larger than 5 MB")
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if fmt not in ("PNG", "JPEG"):
            raise HTTPException(415, "Only PNG or JPEG images are accepted")
        if img.width * img.height > MAX_PIXELS:
            raise HTTPException(413, "Image larger than 20 megapixels")
        img.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(415, f"Not a valid PNG/JPEG image: {exc}") from exc

    plan_id = str(uuid.uuid4())
    ext = "png" if fmt == "PNG" else "jpg"
    path = config.UPLOADS_DIR / f"{plan_id}.{ext}"
    path.write_bytes(data)
    inv = config.load_inventory()
    try:
        topo, meta = analyze_image(data, inv)
    except AIError as exc:
        raise HTTPException(exc.status, {"error": str(exc), "raw": exc.raw}) from exc

    topology = topo.model_dump()
    plan = {
        "id": plan_id, "created_at": store.now(), "status": "DRAFT", "deployment_status": "NOT_DEPLOYED",
        "image_path": str(path), "image_name": image.filename, "image_sha256": hashlib.sha256(data).hexdigest(),
        "ai": meta, "ai_topology": topology, "topology": topology, "resolved_ambiguities": [], "edit_count": 0,
        "validation": None, "approved_config": None, "config_sha256": None, "jobs": [], "current_job_id": None,
    }
    with store.LOCK:
        store.save("plan", plan)
    log.info("plan %s analysed: %d routers, %d ambiguities", plan_id, len(topo.routers), len(topo.ambiguities))
    return _public_plan(plan)


@app.get("/api/plans")
def list_plans():
    return [{k: p.get(k) for k in ("id", "created_at", "status", "deployment_status", "image_name",
                                    "config_sha256", "current_job_id")} for p in store.list_all("plan")]


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: str):
    return _public_plan(_plan_or_404(plan_id))


@app.get("/api/plans/{plan_id}/image")
def plan_image(plan_id: str):
    plan = _plan_or_404(plan_id)
    return FileResponse(plan["image_path"])


class ResolvedItem(BaseModel):
    ambiguity: str
    note: str = ""


class TopologyEdit(BaseModel):
    topology: dict
    resolved: list[ResolvedItem] = []


@app.put("/api/plans/{plan_id}/topology")
def edit_topology(plan_id: str, body: TopologyEdit, x_admin_token: str | None = Header(None)):
    _require_admin(x_admin_token)
    with store.LOCK:
        plan = _plan_or_404(plan_id)
        if plan["status"] != "DRAFT":
            raise HTTPException(409, f"Plan is {plan['status']}; only DRAFT plans can be edited")
        try:
            new = TopologyPlan.model_validate(body.topology)
        except ValidationError as exc:
            raise HTTPException(422, f"Invalid topology: {exc}") from exc
        old = _topology(plan)
        if sorted(r.name for r in new.routers) != sorted(r.name for r in old.routers):
            raise HTTPException(422, "Routers cannot be added, removed or renamed in the editor")
        for r in new.routers:
            for i in r.interfaces:
                i.name = normalize_interface(i.name)
                if i.role == "management":
                    i.ipv4_cidr = None
        for pc in new.pcs:
            pc.port = "e0"
        resolved_texts = {r.ambiguity.strip() for r in body.resolved}
        unknown = resolved_texts - set(old.ambiguities)
        if unknown:
            raise HTTPException(422, f"Unknown ambiguity: {sorted(unknown)[0]}")
        for r in body.resolved:
            if not r.note.strip():
                raise HTTPException(422, f"Add a note explaining how '{r.ambiguity}' was resolved")
        new.ambiguities = [a for a in old.ambiguities if a not in resolved_texts]
        plan["topology"] = new.model_dump()
        plan["resolved_ambiguities"] += [{"ambiguity": r.ambiguity, "note": r.note.strip(), "at": store.now()}
                                         for r in body.resolved]
        plan["edit_count"] = plan.get("edit_count", 0) + 1
        plan["validation"] = None
        store.save("plan", plan)
    return _public_plan(plan)


@app.post("/api/plans/{plan_id}/discover")
def discover(plan_id: str):
    plan = _plan_or_404(plan_id)
    topo = _topology(plan)
    return discover_interfaces([r.name for r in topo.routers], config.load_inventory())


@app.get("/api/plans/{plan_id}/review")
def review(plan_id: str):
    plan = _plan_or_404(plan_id)
    topo = _topology(plan)
    commands, errors = render_plan(topo, config.load_inventory())
    v = plan.get("validation")
    return {
        "plan_id": plan_id, "status": plan["status"], "deployment_status": plan["deployment_status"],
        "commands": plan["approved_config"] if plan["status"] == "APPROVED" else commands,
        "generation_errors": errors, "open_ambiguities": topo.ambiguities,
        "resolved_ambiguities": plan.get("resolved_ambiguities", []),
        "validation": v if v and v.get("topology_sha") == _topology_sha(plan["topology"]) else None,
        "config_sha256": plan.get("config_sha256"),
    }


@app.post("/api/plans/{plan_id}/validate")
def validate(plan_id: str):
    plan = _plan_or_404(plan_id)
    result = validate_plan(_topology(plan), config.load_inventory(), live=True)
    result["at"] = store.now()
    with store.LOCK:
        plan = _plan_or_404(plan_id)
        result["topology_sha"] = _topology_sha(plan["topology"])
        plan["validation"] = result
        store.save("plan", plan)
    return result


class ApproveBody(BaseModel):
    confirmation: str
    approved_by: str = "administrator"


@app.post("/api/plans/{plan_id}/approve")
def approve(plan_id: str, body: ApproveBody, x_admin_token: str | None = Header(None)):
    _require_admin(x_admin_token)
    if body.confirmation != "APPROVE":
        raise HTTPException(400, 'Explicit approval required: confirmation must be "APPROVE"')
    plan = _plan_or_404(plan_id)
    if plan["status"] != "DRAFT":
        raise HTTPException(409, f"Plan is already {plan['status']}")
    topo = _topology(plan)
    inv = config.load_inventory()
    result = validate_plan(topo, inv, live=True)           # re-validate at approval time
    result["at"] = store.now()
    result["topology_sha"] = _topology_sha(plan["topology"])
    if not result["passed"]:
        with store.LOCK:
            plan = _plan_or_404(plan_id)
            plan["validation"] = result
            store.save("plan", plan)
        raise HTTPException(409, {"error": "Validation failed; not approved", "validation": result})
    commands, errors = render_plan(topo, inv)
    if errors:
        raise HTTPException(409, {"error": "Configuration generation failed", "errors": errors})
    with store.LOCK:
        plan = _plan_or_404(plan_id)
        if plan["status"] != "DRAFT" or _topology_sha(plan["topology"]) != result["topology_sha"]:
            raise HTTPException(409, "Plan changed during approval; review again")
        plan.update(status="APPROVED", validation=result, approved_config=commands,
                    config_sha256=config_digest(commands), approved_at=store.now(),
                    approved_by=body.approved_by.strip()[:64] or "administrator")
        store.save("plan", plan)
    log.info("plan %s APPROVED by %s sha256=%s", plan_id, plan["approved_by"], plan["config_sha256"])
    return _public_plan(plan)


@app.get("/api/plans/{plan_id}/approved-config")
def approved_config(plan_id: str):
    plan = _plan_or_404(plan_id)
    if plan["status"] != "APPROVED" or not plan.get("approved_config"):
        raise HTTPException(409, f"Plan is {plan['status']}, no approved configuration")
    topo = _topology(plan)
    inv = config.load_inventory()
    return {"plan_id": plan_id, "config_sha256": plan["config_sha256"], "commands": plan["approved_config"],
            "approved_by": plan.get("approved_by"), "approved_at": plan.get("approved_at"),
            "ospf_process_id": topo.ospf_process_id, "topology": plan["topology"],
            "management_interfaces": {n: (inv[n].management_interface if n in inv else None)
                                      for n in plan["approved_config"]}}


@app.post("/api/plans/{plan_id}/deploy")
def deploy(plan_id: str, x_admin_token: str | None = Header(None)):
    _require_admin(x_admin_token)
    with store.LOCK:
        plan = _plan_or_404(plan_id)
        if plan["status"] != "APPROVED":
            raise HTTPException(409, "Plan is not approved - approval is mandatory before deployment")
        v = plan.get("validation") or {}
        if not v.get("passed") or v.get("topology_sha") != _topology_sha(plan["topology"]):
            raise HTTPException(409, "Validation has not passed for this exact plan")
        if config_digest(plan["approved_config"]) != plan["config_sha256"]:
            raise HTTPException(409, "Approved configuration hash mismatch - refusing")
        if plan["deployment_status"] in ("QUEUED", "RUNNING"):
            raise HTTPException(409, f"A deployment is already {plan['deployment_status']} "
                                     f"(job {plan.get('current_job_id')})")
        if plan["deployment_status"] == "SUCCESS":
            raise HTTPException(409, "This plan is already deployed successfully")
        job_id = str(uuid.uuid4())
        devices = sorted(plan["approved_config"])
        job = {"id": job_id, "plan_id": plan_id, "status": "PENDING", "stage": "queued", "devices": devices,
               "config_sha256": plan["config_sha256"], "created_at": store.now(), "started_at": None,
               "finished_at": None, "worker_id": None, "router_results": {}, "verification": {},
               "error": None, "history": [], "kafka": None}
        _event(job, "PENDING", "queued", "Job created; publishing to Kafka")
        message = {"job_id": job_id, "plan_id": plan_id, "device_ids": devices,
                   "config_sha256": plan["config_sha256"],
                   "approved_config_ref": f"/api/plans/{plan_id}/approved-config",
                   "requested_at": job["created_at"]}
        job["kafka_message"] = message
        store.save("job", job)
        plan["deployment_status"] = "QUEUED"
        plan["current_job_id"] = job_id
        plan["jobs"].append(job_id)
        store.save("plan", plan)

    delivered: dict = {}

    def _cb(err, msg):
        delivered["err"] = err
        if msg is not None and err is None:
            delivered.update(topic=msg.topic(), partition=msg.partition(), offset=msg.offset())

    try:
        p = _producer_get()
        p.produce(config.KAFKA_TOPIC, key=job_id.encode(), value=json.dumps(message).encode(), on_delivery=_cb)
        p.flush(15)
        if delivered.get("err") is not None or "offset" not in delivered:
            raise RuntimeError(str(delivered.get("err") or "no delivery report within 15 s"))
    except Exception as exc:  # noqa: BLE001
        with store.LOCK:
            job = _job_or_404(job_id)
            job.update(status="FAILED", stage="kafka", error=f"Kafka publish failed: {exc}",
                       finished_at=store.now())
            _event(job, "FAILED", "kafka", job["error"])
            store.save("job", job)
            plan = _plan_or_404(plan_id)
            plan["deployment_status"] = "FAILED"
            store.save("plan", plan)
        raise HTTPException(502, f"Kafka publish failed: {exc}") from exc

    with store.LOCK:
        job = _job_or_404(job_id)
        job["kafka"] = delivered
        _event(job, job["status"], job["stage"],
               f"Published to {delivered['topic']} partition {delivered['partition']} offset {delivered['offset']}")
        store.save("job", job)
    log.info("job %s published %s", job_id, delivered)
    return {"job_id": job_id, "kafka": delivered, "message": message}


# ------------------------------------------------------------------ worker callbacks
class ClaimBody(BaseModel):
    config_sha256: str
    worker_id: str


@app.post("/api/jobs/{job_id}/claim")
def claim(job_id: str, body: ClaimBody, x_worker_token: str | None = Header(None)):
    _require_worker(x_worker_token)
    with store.LOCK:
        job = _job_or_404(job_id)
        if job["status"] != "PENDING":
            raise HTTPException(409, f"Job is {job['status']}; it will not be executed again")
        plan = _plan_or_404(job["plan_id"])
        problem = None
        if plan["status"] != "APPROVED":
            problem = f"plan is {plan['status']}, not APPROVED"
        elif plan.get("current_job_id") != job_id or plan["deployment_status"] != "QUEUED":
            problem = "job is not the current queued deployment of its plan"
        elif not (body.config_sha256 == job["config_sha256"] == plan["config_sha256"]
                  == config_digest(plan["approved_config"])):
            problem = "configuration hash does not match the approved configuration"
        if problem:
            job.update(status="FAILED", stage="claim", error=f"Approval re-check failed: {problem}",
                       finished_at=store.now())
            _event(job, "FAILED", "claim", job["error"])
            store.save("job", job)
            if plan.get("current_job_id") == job_id:
                plan["deployment_status"] = "FAILED"
                store.save("plan", plan)
            raise HTTPException(409, job["error"])
        job.update(status="RUNNING", stage="claimed", started_at=store.now(), worker_id=body.worker_id)
        _event(job, "RUNNING", "claimed", f"Approval re-checked; claimed by {body.worker_id}")
        store.save("job", job)
        plan["deployment_status"] = "RUNNING"
        store.save("plan", plan)
    return {"job_id": job_id, "status": "RUNNING", "approved_config_ref": f"/api/plans/{plan['id']}/approved-config"}


class ProgressBody(BaseModel):
    status: str = "RUNNING"
    stage: str
    message: str = ""
    device: str | None = None
    router_results: dict | None = None
    verification: dict | None = None
    error: str | None = None


@app.post("/api/jobs/{job_id}/progress")
def progress(job_id: str, body: ProgressBody, x_worker_token: str | None = Header(None)):
    _require_worker(x_worker_token)
    if body.status not in ("RUNNING", "SUCCESS", "FAILED"):
        raise HTTPException(422, "status must be RUNNING, SUCCESS or FAILED")
    with store.LOCK:
        job = _job_or_404(job_id)
        if job["status"] != "RUNNING":
            raise HTTPException(409, f"Job is {job['status']}; progress not accepted")
        job["stage"] = body.stage
        if body.router_results:
            for r, res in body.router_results.items():
                job["router_results"].setdefault(r, {}).update(res)
        if body.verification:
            job["verification"].update(body.verification)
        if body.status == "SUCCESS":
            job["error"] = None
        elif body.error:
            job["error"] = body.error
        job["status"] = body.status
        if body.status in ("SUCCESS", "FAILED"):
            job["finished_at"] = store.now()
        _event(job, body.status, body.stage, body.message or (body.error or ""), body.device)
        store.save("job", job)
        if body.status in ("SUCCESS", "FAILED"):
            plan = _plan_or_404(job["plan_id"])
            if plan.get("current_job_id") == job_id:
                plan["deployment_status"] = body.status
                store.save("plan", plan)
    return {"ok": True}


# ------------------------------------------------------------------ monitor
@app.get("/api/jobs")
def list_jobs():
    out = []
    for j in store.list_all("job"):
        out.append({k: j.get(k) for k in ("id", "plan_id", "status", "stage", "devices", "created_at",
                                          "started_at", "finished_at", "error")})
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job_or_404(job_id)


@app.post("/api/jobs/{job_id}/revoke")
def revoke(job_id: str, x_admin_token: str | None = Header(None)):
    """Recovery after a FAILED job: un-approve the plan so it can be corrected and approved again."""
    _require_admin(x_admin_token)
    with store.LOCK:
        job = _job_or_404(job_id)
        if job["status"] != "FAILED":
            raise HTTPException(409, "Only FAILED jobs can be revoked")
        plan = _plan_or_404(job["plan_id"])
        if plan.get("current_job_id") == job_id and plan["status"] == "APPROVED":
            plan.update(status="DRAFT", approved_config=None, config_sha256=None, validation=None,
                        deployment_status="NOT_DEPLOYED", current_job_id=None, revoked_at=store.now())
            store.save("plan", plan)
        job["status"] = "REVOKED"
        _event(job, "REVOKED", "revoked", "Approval revoked by administrator; plan back to DRAFT")
        store.save("job", job)
    return {"job_id": job_id, "status": "REVOKED", "plan_id": plan["id"], "plan_status": plan["status"]}
