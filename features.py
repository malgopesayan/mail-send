"""API routes for tracking, follow-ups, resumes, alerts, job sources and data privacy."""
import io
import json
import re
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

import config
import db
import gmail_service as gm
import llm
import maintenance
import matching
import notify
import pipeline
import resumes
import safety
import storage
import tracker
from auth import current_user

router = APIRouter()
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _job(uid: str, job_id: str) -> dict:
    job = db.get_job(uid, job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return job


def _idle(uid: str):
    if pipeline.state_for(uid).running:
        raise HTTPException(409, "A run is in progress — stop it or wait first")


def _download(data: bytes, filename: str, mime: str) -> Response:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{safe}"',
                                                    "Cache-Control": "no-store"})


def _view(uid: str, job_id: str) -> dict:
    import main                                          # late import: main imports this module
    return main._job_view(uid, job_id)


# --------------------------------------------------------------- stages ----

class StageIn(BaseModel):
    stage: str
    notes: str | None = Field(None, max_length=2000)


@router.post("/api/jobs/{job_id}/stage")
def set_stage(job_id: str, body: StageIn, u: dict = Depends(current_user)):
    uid = u["uid"]
    if body.stage not in tracker.STAGES:
        raise HTTPException(400, "Unknown stage")
    with pipeline.send_lock(uid):
        job = _job(uid, job_id)
        fields = tracker.stage_fields(job, body.stage)
        if body.notes is not None:
            fields["notes"] = body.notes
        db.update_job(uid, job_id, fields)
        if job.get("email_status") != "Sent" and fields.get("email_status") == "Sent" and job.get("recruiter_email"):
            safety.remember_contact(uid, job["recruiter_email"])
    return _view(uid, job_id)


# ----------------------------------------------------------- follow-ups ----

@router.get("/api/followups")
def follow_ups(u: dict = Depends(current_user)):
    uid = u["uid"]
    cand = db.get_user(uid)["candidate"]
    return [{"id": j["id"], "company": j.get("company"), "job_title": j.get("job_title"),
             "recruiter_email": j.get("recruiter_email"), "sent_date": j.get("sent_date"),
             "due_date": j.get("follow_up_date"), **tracker.follow_up_draft(j, cand)}
            for j in tracker.due_follow_ups(db.list_jobs(uid))]


@router.get("/api/jobs/{job_id}/followup")
def follow_up_preview(job_id: str, u: dict = Depends(current_user)):
    uid = u["uid"]
    job = _job(uid, job_id)
    return {"blocked": tracker.can_follow_up(job), "to": job.get("recruiter_email"),
            "same_thread": bool(job.get("gmail_thread_id") and job.get("rfc_message_id")),
            **tracker.follow_up_draft(job, db.get_user(uid)["candidate"])}


class FollowUpIn(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=10, max_length=4000)


@router.post("/api/jobs/{job_id}/followup")
def follow_up_send(job_id: str, body: FollowUpIn, u: dict = Depends(current_user)):
    """Only ever runs when the user presses Send on the preview. Schedules never call it."""
    uid = u["uid"]
    _idle(uid)
    user, sec = db.get_user(uid), db.get_secrets(uid)
    _job(uid, job_id)
    try:
        sender = pipeline.make_sender(sec, user)
    except gm.GmailAuthRequired as exc:
        raise HTTPException(401, {"needs_gmail": True, "message": str(exc)})
    try:
        with pipeline.send_lock(uid):
            pipeline.send_follow_up(uid, _job(uid, job_id), sender, user, body.subject, body.body)
    except safety.SendBlocked as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Gmail refused the mail: {str(exc)[:200]}")
    return _view(uid, job_id)


@router.post("/api/jobs/{job_id}/followup/skip")
def follow_up_skip(job_id: str, u: dict = Depends(current_user)):
    uid = u["uid"]
    _job(uid, job_id)
    db.update_job(uid, job_id, {"follow_up_date": None})
    return _view(uid, job_id)


# ------------------------------------------------------ analytics, export ----

@router.get("/api/analytics")
def get_analytics(u: dict = Depends(current_user)):
    uid = u["uid"]
    user, jobs = db.get_user(uid), db.list_jobs(uid)
    return {**tracker.analytics(jobs, int(user["settings"]["min_score"]), safety.mailed_for(uid, jobs)),
            "usage": safety.usage(uid, user)}


@router.get("/api/export/jobs.{fmt}")
def export_jobs(fmt: str, u: dict = Depends(current_user)):
    rows = db.list_all_jobs(u["uid"])
    rows.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    if fmt == "csv":
        return _download(tracker.to_csv(rows), "jobs.csv", "text/csv; charset=utf-8")
    if fmt == "xlsx":
        return _download(tracker.to_xlsx(rows), "jobs.xlsx",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    raise HTTPException(404, "Use jobs.csv or jobs.xlsx")


# ------------------------------------------------------------- privacy ----

@router.get("/api/export/all.zip")
def export_all(u: dict = Depends(current_user)):
    """Everything stored about you, except API keys and tokens."""
    uid = u["uid"]
    user = db.get_user(uid)
    jobs = db.list_all_jobs(uid)
    data = {"account": user, "jobs": jobs, "runs": db.list_runs(uid, 500), "assistant_actions": db.list_audit(uid),
            "queued_posts": db.queue_list(uid),
            "schedules": [d.to_dict() for d in db.schedules().where("uid", "==", uid).stream()]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.json", json.dumps(data, indent=2, default=str))
        z.writestr("jobs.csv", tracker.to_csv(jobs))
        for bucket, folder in ((config.RESUME_BUCKET, "resumes"), (config.SCREENSHOT_BUCKET, "screenshots")):
            for f in storage.list_files(bucket, uid):
                try:
                    z.writestr(f"{folder}/{Path(f['name']).name}", storage.download(bucket, f"{uid}/{f['name']}"))
                except Exception as exc:  # noqa: BLE001
                    z.writestr(f"{folder}/{Path(f['name']).name}.error.txt", str(exc)[:200])
    return _download(buf.getvalue(), "job-mailer-data.zip", "application/zip")


class DeleteIn(BaseModel):
    confirm: str


@router.post("/api/account/delete")
def delete_account_data(body: DeleteIn, u: dict = Depends(current_user)):
    uid = u["uid"]
    if body.confirm != "DELETE":
        raise HTTPException(400, 'Type DELETE to confirm')
    _idle(uid)
    # files first: if storage fails, nothing else is removed and the user can retry
    for bucket in (config.RESUME_BUCKET, config.SCREENSHOT_BUCKET):
        for _ in range(20):
            names = [f"{uid}/{f['name']}" for f in storage.list_files(bucket, uid)]
            if not names:
                break
            storage.remove(bucket, names)
    db.delete_all_user_data(uid)
    return {"ok": True}


@router.post("/api/privacy/purge")
def purge_now(u: dict = Depends(current_user)):
    uid = u["uid"]
    _idle(uid)
    days = int(db.get_user(uid)["settings"].get("retention_days") or 0)
    if days <= 0:
        raise HTTPException(400, "Set 'Delete jobs older than' to more than 0 and save first")
    return {"removed": maintenance.purge_old_jobs(uid, days)}


# -------------------------------------------------------------- alerts ----

@router.post("/api/telegram/test")
def telegram_test(u: dict = Depends(current_user)):
    uid = u["uid"]
    token = db.get_secrets(uid).get("telegram_token", "")
    chat = db.get_user(uid)["settings"].get("telegram_chat_id", "")
    if not token or not chat:
        raise HTTPException(400, "Save your bot token and chat id first")
    try:
        notify.send_telegram(token, chat, "Job Mailer: alerts are working ✅")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


# ------------------------------------------------------------- resumes ----

@router.post("/api/resumes")
async def add_resume(label: str = Form(...), file: UploadFile = File(...), u: dict = Depends(current_user)):
    uid = u["uid"]
    label = matching.one_line(label, 40)
    ext = Path(file.filename or "").suffix.lower()
    if not label:
        raise HTTPException(400, "Give this resume a short name, e.g. Backend")
    if ext not in (".pdf", ".docx"):
        raise HTTPException(400, "Resume must be .pdf or .docx")
    data = await file.read()
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File is larger than {config.MAX_UPLOAD_MB} MB")
    user = db.get_user(uid)
    if len(resumes.all_resumes(user)) >= config.MAX_RESUMES:
        raise HTTPException(400, f"Maximum {config.MAX_RESUMES} resumes — remove one first")
    base = re.sub(r"[^A-Za-z0-9]+", "_", user["candidate"].get("name") or "My").strip("_") or "My"
    path = f"{uid}/resume_{uuid.uuid4().hex[:8]}{ext}"
    storage.upload(config.RESUME_BUCKET, path, data, file.content_type)
    text = resumes.ai.extract_resume_text(f"x{ext}", data)
    row = {"id": uuid.uuid4().hex[:10], "label": label, "name": f"{base}_Resume{ext}", "path": path,
           "size": len(data), "updated_at": db.now_iso(), "keywords": " ".join(sorted(resumes.words(text)))[:4000]}
    meta = {"resume": row} if not user.get("resume") else {"resumes": [*(user.get("resumes") or []), row]}
    db.update_user(uid, meta)
    return {"resumes": _resume_list(uid)}


def _resume_list(uid: str) -> list[dict]:
    return [{k: r.get(k) for k in ("id", "label", "name", "size")} for r in resumes.all_resumes(db.get_user(uid))]


@router.get("/api/resumes")
def list_resumes(u: dict = Depends(current_user)):
    return {"resumes": _resume_list(u["uid"])}


@router.delete("/api/resumes/{rid}")
def remove_resume(rid: str, u: dict = Depends(current_user)):
    uid = u["uid"]
    _idle(uid)
    user = db.get_user(uid)
    row = next((r for r in user.get("resumes") or [] if r["id"] == rid), None)
    if not row:
        raise HTTPException(404, "No such resume (the main resume is removed in the section above)")
    try:
        storage.remove(config.RESUME_BUCKET, [row["path"]])
    except Exception:  # noqa: BLE001
        pass
    db.update_user(uid, {"resumes": [r for r in user["resumes"] if r["id"] != rid]})
    return {"resumes": _resume_list(uid)}


class TailorIn(BaseModel):
    resume_id: str = Field("", max_length=20)
    job_text: str = Field("", max_length=5000)


@router.post("/api/jobs/{job_id}/tailor")
def tailor(job_id: str, body: TailorIn, u: dict = Depends(current_user)):
    uid = u["uid"]
    job = _job(uid, job_id)
    try:
        draft = resumes.make(uid, db.get_user(uid), db.get_secrets(uid), job, body.resume_id, body.job_text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Resume making failed: {str(exc)[:200]}")
    old = job.get("tailored_draft")
    if old and old.get("path") != (job.get("tailored") or {}).get("path"):
        resumes.discard_tailored({"tailored_draft": old})
    db.update_job(uid, job_id, {"tailored_draft": draft})
    resumes.prune_made(uid)
    return draft


@router.post("/api/jobs/{job_id}/tailor/use")
def tailor_use(job_id: str, u: dict = Depends(current_user)):
    """The user checked the draft: attach it to this job's mail."""
    uid = u["uid"]
    with pipeline.send_lock(uid):
        draft = _job(uid, job_id).get("tailored_draft")
        if not draft:
            raise HTTPException(400, "Make a resume for this job first")
        db.update_job(uid, job_id, {"tailored": {"name": draft["name"], "path": draft["path"]}})
    return _view(uid, job_id)


@router.post("/api/jobs/{job_id}/tailor/remove")
def tailor_remove(job_id: str, u: dict = Depends(current_user)):
    uid = u["uid"]
    with pipeline.send_lock(uid):
        _job(uid, job_id)
        db.update_job(uid, job_id, {"tailored": None})
    return _view(uid, job_id)


@router.get("/api/jobs/{job_id}/tailor/download")
def tailor_download(job_id: str, u: dict = Depends(current_user)):
    uid = u["uid"]
    job = _job(uid, job_id)
    row = job.get("tailored_draft") or job.get("tailored")
    if not row or not row["path"].startswith(f"{uid}/"):
        raise HTTPException(404, "No resume made for this job yet")
    return _download(storage.download(config.RESUME_BUCKET, row["path"]), row["name"], DOCX_MIME)


@router.post("/api/resume/make")
def make_general(u: dict = Depends(current_user)):
    """A clean general resume from your main resume, as a Word file."""
    uid = u["uid"]
    try:
        draft = resumes.make(uid, db.get_user(uid), db.get_secrets(uid), None, "default")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Resume making failed: {str(exc)[:200]}")
    db.set_private(uid, "general_resume", {"name": draft["name"], "path": draft["path"]})
    return draft


@router.get("/api/resume/make/download")
def download_general(u: dict = Depends(current_user)):
    row = db.get_private(u["uid"], "general_resume")
    if not row:
        raise HTTPException(404, "Make the resume first")
    return _download(storage.download(config.RESUME_BUCKET, row["path"]), row["name"], DOCX_MIME)
