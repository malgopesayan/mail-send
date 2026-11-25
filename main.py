"""Job Mailer v2 — multi-user. Start:  uvicorn main:app --host 0.0.0.0 --port $PORT"""
import json
import re
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

import ai
import config
import crypto
import db
import gmail_service as gm
import pipeline
import scheduler
import storage
from auth import current_user

app = FastAPI(title="Job Mailer v2")
app.include_router(scheduler.router)
STATIC = Path(__file__).parent / "static"
MAX_BYTES = config.MAX_UPLOAD_MB * 1024 * 1024


# ------------------------------------------------------------ static / boot ----

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy():
    return FileResponse(STATIC / "privacy.html", media_type="text/html")


@app.get("/terms", response_class=HTMLResponse)
def terms_of_service():
    return FileResponse(STATIC / "terms.html", media_type="text/html")


@app.get("/config.js")
def config_js():
    """Public Firebase web config, injected from env so index.html never needs editing."""
    try:
        cfg = json.loads(config.FIREBASE_WEB_CONFIG_JSON or "{}")
    except json.JSONDecodeError:
        cfg = {}
    return PlainTextResponse(f"window.FIREBASE_CONFIG = {json.dumps(cfg)};",
                             media_type="application/javascript", headers={"Cache-Control": "no-store"})


# -------------------------------------------------------------------- me ----

def _public_state(uid: str, user: dict) -> dict:
    sec = db.get_secrets(uid)
    keys = sec.get("groq_keys", [])
    shared_gmail = bool(config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET)
    return {
        "email": user.get("email", ""), "name": user.get("name", ""), "photo": user.get("photo", ""),
        "candidate": user["candidate"], "settings": user["settings"], "keywords": user["keywords"],
        "profile_text": user["profile_text"], "resume": user["resume"],
        "keys": {
            "groq": [crypto.mask(k) for k in keys],
            "harvest": crypto.mask(sec.get("harvest_key", "")) if sec.get("harvest_key") else "",
        },
        "gmail": {
            "connected": bool(sec.get("gmail_refresh_token")),
            "email": user["gmail"].get("email", ""),
            "custom_client": bool(sec.get("gmail_client")),
            "client_available": bool(sec.get("gmail_client")) or shared_gmail,
            "redirect_uri": config.GMAIL_REDIRECT_URI,
        },
    }


@app.get("/api/me")
def me(u: dict = Depends(current_user)):
    return _public_state(u["uid"], db.ensure_user(u["uid"], u))


# -------------------------------------------------------------- settings ----

class CandidateIn(BaseModel):
    name: str = Field("", max_length=120)
    email: str = Field("", max_length=200)
    phone: str = Field("", max_length=60)
    linkedin: str = Field("", max_length=200)
    github: str = Field("", max_length=200)


class SettingsIn(BaseModel):
    candidate: CandidateIn | None = None
    min_score: int | None = Field(None, ge=0, le=100)
    max_age_days: int | None = Field(None, ge=1, le=30)
    require_contact: bool | None = None
    preferred_locations: list[str] | None = None
    strict_location: bool | None = None
    include_remote: bool | None = None
    max_send_per_run: int | None = Field(None, ge=1, le=200)
    timezone: str | None = None
    vision_model: str | None = Field(None, max_length=120)
    chat_model: str | None = Field(None, max_length=120)
    keywords: list[str] | None = None
    profile_text: str | None = Field(None, max_length=20000)


@app.put("/api/settings")
def save_settings(body: SettingsIn, u: dict = Depends(current_user)):
    d = body.model_dump(exclude_none=True)
    out: dict = {}
    if "candidate" in d:
        out["candidate"] = d.pop("candidate")
    if "keywords" in d:
        seen, kws = set(), []
        for k in d.pop("keywords"):
            k = k.strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                kws.append(k[:120])
        out["keywords"] = kws[:40]
    if "preferred_locations" in d:
        seen, locs = set(), []
        for loc in d.pop("preferred_locations"):
            loc = loc.strip()
            if loc and loc.lower() not in seen:
                seen.add(loc.lower())
                locs.append(loc[:80])
        d["preferred_locations"] = locs[:5]
    if "profile_text" in d:
        out["profile_text"] = d.pop("profile_text").strip()
    if "timezone" in d:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(d["timezone"])
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Unknown timezone (use e.g. Asia/Kolkata)")
    for k in ("vision_model", "chat_model"):
        if k in d and not d[k].strip():
            d.pop(k)
    if d:
        out["settings"] = d
    if out:
        db.update_user(u["uid"], out)
    return _public_state(u["uid"], db.get_user(u["uid"]))


class SecretsIn(BaseModel):
    # None = leave unchanged, ""/[] = clear
    groq_keys: list[str] | None = None
    harvest_key: str | None = None
    gmail_client_json: str | None = None


@app.put("/api/secrets")
def save_secrets(body: SecretsIn, u: dict = Depends(current_user)):
    uid = u["uid"]
    sec = db.get_secrets(uid)
    if body.groq_keys is not None:
        sec["groq_keys"] = [k.strip() for k in body.groq_keys if k.strip()][:8]
    if body.harvest_key is not None:
        sec["harvest_key"] = body.harvest_key.strip()
    if body.gmail_client_json is not None:
        if body.gmail_client_json.strip():
            try:
                client = gm.parse_client_json(body.gmail_client_json)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"Not a valid Google OAuth client JSON: {exc}")
            sec["gmail_client"] = client
        else:
            sec.pop("gmail_client", None)
        # a token belongs to ONE client: switching clients invalidates it
        sec.pop("gmail_refresh_token", None)
        db.update_user(uid, {"gmail": {"connected": False, "email": ""}})
    db.save_secrets(uid, sec)
    return _public_state(uid, db.get_user(uid))


@app.post("/api/secrets/test-groq")
def test_groq(u: dict = Depends(current_user)):
    from groq import Groq
    keys = db.get_secrets(u["uid"]).get("groq_keys", [])
    if not keys:
        raise HTTPException(400, "No Groq key saved yet")
    results = []
    for i, k in enumerate(keys, 1):
        try:
            Groq(api_key=k).models.list()
            results.append({"key": i, "ok": True})
        except Exception as exc:  # noqa: BLE001
            results.append({"key": i, "ok": False, "error": str(exc)[:140]})
    return {"results": results}


# ----------------------------------------------------------------- gmail ----

@app.get("/api/gmail/auth-url")
def gmail_auth_url(u: dict = Depends(current_user)):
    client = gm.client_for(db.get_secrets(u["uid"]))
    if not client:
        raise HTTPException(400, "No Google OAuth client configured. Paste your own in Settings → Gmail → Advanced.")
    return {"url": gm.auth_url(client, crypto.sign_state(u["uid"]))}


@app.get("/api/gmail/callback")
def gmail_callback(code: str = "", state: str = "", error: str = ""):
    """Google redirects the browser here — no Authorization header, so identity comes from the signed `state`."""
    if error:
        return RedirectResponse(f"/?gmail=error&msg={error}")
    uid = crypto.verify_state(state)
    if not uid or not code:
        return RedirectResponse("/?gmail=error&msg=invalid_state")
    sec = db.get_secrets(uid)
    client = gm.client_for(sec)
    try:
        refresh, addr = gm.exchange_code(client, code)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/?gmail=error&msg=" + re.sub(r"[^A-Za-z0-9 _.,:-]", "", str(exc))[:160])
    sec["gmail_refresh_token"] = refresh
    db.save_secrets(uid, sec)
    db.update_user(uid, {"gmail": {"connected": True, "email": addr}})
    return RedirectResponse("/?gmail=connected")


@app.post("/api/gmail/disconnect")
def gmail_disconnect(u: dict = Depends(current_user)):
    sec = db.get_secrets(u["uid"])
    sec.pop("gmail_refresh_token", None)
    db.save_secrets(u["uid"], sec)
    db.update_user(u["uid"], {"gmail": {"connected": False, "email": ""}})
    return _public_state(u["uid"], db.get_user(u["uid"]))


# ---------------------------------------------------------------- resume ----

async def _read_upload(f: UploadFile) -> bytes:
    data = await f.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"{f.filename} is larger than {config.MAX_UPLOAD_MB} MB")
    return data


def _keys_and_model(uid: str, user: dict):
    return db.get_secrets(uid).get("groq_keys", []), user["settings"]["chat_model"]


@app.post("/api/resume")
async def upload_resume(file: UploadFile = File(...), u: dict = Depends(current_user)):
    uid = u["uid"]
    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.RESUME_EXTENSIONS:
        raise HTTPException(400, "Resume must be .pdf, .doc or .docx")
    data = await _read_upload(file)
    user = db.get_user(uid)
    old = user.get("resume")
    base = re.sub(r"[^A-Za-z0-9]+", "_", user["candidate"].get("name") or "My").strip("_") or "My"
    name = f"{base}_Resume{ext}"                        # the name recruiters will see
    path = f"{uid}/resume_{uuid.uuid4().hex[:6]}{ext}"
    storage.upload(config.RESUME_BUCKET, path, data, file.content_type)
    if old:
        try:
            storage.remove(config.RESUME_BUCKET, [old["path"]])
        except Exception:  # noqa: BLE001
            pass
    meta = {"name": name, "path": path, "size": len(data), "updated_at": db.now_iso()}
    db.update_user(uid, {"resume": meta})

    # Best-effort: build profile + keywords from the resume if the user has none yet.
    notes = []
    text = ai.extract_resume_text(name, data)
    keys, model = _keys_and_model(uid, user)
    if text.strip() and keys:
        extra = {}
        if not user["profile_text"].strip():
            extra["profile_text"] = ai.condense_profile(keys, model, text)
            notes.append("profile built")
        if not user["keywords"]:
            kws = ai.extract_keywords(keys, model, text)
            if kws:
                extra["keywords"] = kws
                notes.append(f"{len(kws)} keywords generated")
        if extra:
            db.update_user(uid, extra)
    elif not keys:
        notes.append("add a Groq key, then press “Build from resume”")
    return {"notes": notes, "state": _public_state(uid, db.get_user(uid))}


@app.delete("/api/resume")
def delete_resume(u: dict = Depends(current_user)):
    user = db.get_user(u["uid"])
    if user.get("resume"):
        try:
            storage.remove(config.RESUME_BUCKET, [user["resume"]["path"]])
        except Exception:  # noqa: BLE001
            pass
        db.clear_user_field(u["uid"], "resume", None)
    return _public_state(u["uid"], db.get_user(u["uid"]))


@app.post("/api/profile/build")
def build_profile(u: dict = Depends(current_user)):
    """(Re)builds experience bank + keywords from the stored resume."""
    uid = u["uid"]
    user = db.get_user(uid)
    if not user.get("resume"):
        raise HTTPException(400, "Upload a resume first")
    keys, model = _keys_and_model(uid, user)
    if not keys:
        raise HTTPException(400, "Add a Groq API key first")
    r = user["resume"]
    text = ai.extract_resume_text(r["name"], storage.download(config.RESUME_BUCKET, r["path"]))
    if not text.strip():
        raise HTTPException(400, "Couldn't read text from that resume (.doc files aren't supported — use PDF or DOCX)")
    update = {"profile_text": ai.condense_profile(keys, model, text)}
    kws = ai.extract_keywords(keys, model, text)
    if kws:
        update["keywords"] = kws
    db.update_user(uid, update)
    return _public_state(uid, db.get_user(uid))


@app.post("/api/profile/tex")
async def upload_tex(file: UploadFile = File(...), u: dict = Depends(current_user)):
    uid = u["uid"]
    if Path(file.filename or "").suffix.lower() != ".tex":
        raise HTTPException(400, "Please upload a .tex file")
    raw = await file.read()
    if len(raw) > 500_000:
        raise HTTPException(400, ".tex file too large")
    clean = ai.latex_to_text(raw.decode("utf-8", errors="ignore"))
    if len(clean) < 150:
        raise HTTPException(400, "No readable resume text found in that .tex")
    user = db.get_user(uid)
    keys, model = _keys_and_model(uid, user)
    update = {"profile_text": ai.condense_profile(keys, model, clean)}
    if not user["keywords"]:
        kws = ai.extract_keywords(keys, model, clean)
        if kws:
            update["keywords"] = kws
    db.update_user(uid, update)
    return _public_state(uid, db.get_user(uid))


# ----------------------------------------------------------- screenshots ----

@app.post("/api/screenshots")
async def upload_screenshots(files: list[UploadFile] = File(...), u: dict = Depends(current_user)):
    uid = u["uid"]
    waiting = len(storage.list_files(config.SCREENSHOT_BUCKET, uid))
    uploaded, skipped = [], []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in config.IMAGE_EXTENSIONS:
            skipped.append(f"{f.filename} (not an image)")
            continue
        if waiting >= config.MAX_PENDING_SCREENSHOTS:
            skipped.append(f"{f.filename} (limit of {config.MAX_PENDING_SCREENSHOTS} waiting screenshots — process some first)")
            continue
        try:
            data = await _read_upload(f)
        except HTTPException as exc:
            skipped.append(f"{f.filename} ({exc.detail})")
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(f.filename).stem)[:60] or "shot"
        storage.upload(config.SCREENSHOT_BUCKET, f"{uid}/{uuid.uuid4().hex[:8]}_{safe}{ext}", data, f.content_type)
        waiting += 1
        uploaded.append(f.filename)
    return {"uploaded": uploaded, "skipped": skipped}


@app.get("/api/screenshots")
def list_screenshots(u: dict = Depends(current_user)):
    uid = u["uid"]
    out = []
    for f in storage.list_files(config.SCREENSHOT_BUCKET, uid):
        if Path(f["name"]).suffix.lower() in config.IMAGE_EXTENSIONS:
            out.append({"name": f["name"], "label": re.sub(r"^[0-9a-f]{8}_", "", f["name"]),
                        "size": (f.get("metadata") or {}).get("size", 0),
                        "url": storage.signed_url(config.SCREENSHOT_BUCKET, f"{uid}/{f['name']}")})
    return {"files": out, "limit": config.MAX_PENDING_SCREENSHOTS}


@app.delete("/api/screenshots/{name}")
def delete_screenshot(name: str, u: dict = Depends(current_user)):
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    storage.remove(config.SCREENSHOT_BUCKET, [f"{u['uid']}/{name}"])
    return {"ok": True}


# ------------------------------------------------------------------ jobs ----

@app.get("/api/jobs")
def jobs(u: dict = Depends(current_user)):
    return db.list_jobs(u["uid"])


class JobPatch(BaseModel):
    recruiter_email: str | None = None
    recruiter_name: str | None = None
    company: str | None = None
    job_title: str | None = None
    location: str | None = None
    priority: str | None = None
    email_subject: str | None = None
    email_body: str | None = None
    email_status: str | None = None
    job_match_score: int | None = Field(None, ge=0, le=100)


@app.patch("/api/jobs/{job_id}")
def patch_job(job_id: str, body: JobPatch, u: dict = Depends(current_user)):
    if not db.get_job(u["uid"], job_id):
        raise HTTPException(404, "No such job")
    fields = body.model_dump(exclude_none=True)
    if fields.get("email_status") not in (None, "Sent", "Not Sent"):
        raise HTTPException(400, "email_status must be 'Sent' or 'Not Sent'")
    if fields:
        db.update_job(u["uid"], job_id, fields)
    return db.get_job(u["uid"], job_id)


@app.delete("/api/jobs/{job_id}")
def remove_job(job_id: str, u: dict = Depends(current_user)):
    db.delete_job(u["uid"], job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/send")
def send_one(job_id: str, u: dict = Depends(current_user)):
    uid = u["uid"]
    job = db.get_job(uid, job_id)
    if not job:
        raise HTTPException(404, "No such job")
    if not pipeline.EMAIL_RE.match((job.get("recruiter_email") or "").strip()):
        raise HTTPException(400, "This job has no valid recruiter email — edit it first.")
    user, sec = db.get_user(uid), db.get_secrets(uid)
    try:
        sender = pipeline.make_sender(sec, user)
    except gm.GmailAuthRequired as exc:
        raise HTTPException(401, {"needs_gmail": True, "message": str(exc)})
    ctx = pipeline.Ctx(uid, pipeline.state_for(uid))
    try:
        pipeline.send_job(uid, job, sender, ctx.resume, user)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Gmail refused the mail: {str(exc)[:200]}")
    return db.get_job(uid, job_id)


# ------------------------------------------------------------------ runs ----

class RunIn(BaseModel):
    mode: str = "both"      # search | process | send_only | send | both


@app.post("/api/run")
def run(body: RunIn, u: dict = Depends(current_user)):
    if body.mode not in pipeline.MODE_STEPS:
        raise HTTPException(400, "bad mode")
    if not pipeline.start_user_run(u["uid"], pipeline.MODE_STEPS[body.mode], "manual"):
        raise HTTPException(409, "A run is already in progress")
    return {"started": True}


@app.get("/api/run/status")
def run_status(after: int = 0, u: dict = Depends(current_user)):
    st = pipeline.state_for(u["uid"])
    return {"running": st.running, "steps": st.steps, "seq": st.seq, "lines": st.since(after)}


@app.get("/api/runs")
def runs(u: dict = Depends(current_user)):
    return db.list_runs(u["uid"])
