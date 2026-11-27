"""Firestore access. The browser NEVER talks to Firestore directly — only this
backend does (firebase-admin bypasses security rules, and firestore.rules
denies every client). That is what keeps each user's API keys private."""
import base64
import json
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import auth as fb_auth
from firebase_admin import credentials, firestore

import config
import crypto

_client = None

USER_DEFAULTS = {
    "candidate": {"name": "", "email": "", "phone": "", "linkedin": "", "github": ""},
    "profile_text": "",
    "keywords": [],
    "resume": None,
    "gmail": {"connected": False, "email": ""},
    "settings": {
        "min_score": 60,
        "strict_match": True,                         # only posts that match your search phrases
        "exclude_words": [],                          # e.g. ["intern", "unpaid"]
        "assistant_model": config.DEFAULT_ASSISTANT_MODEL,
        "max_age_days": 5,
        "require_contact": True,
        "preferred_locations": [],
        "strict_location": False,
        "include_remote": True,
        "max_send_per_run": 40,
        "timezone": config.DEFAULT_TZ,
        "vision_model": config.DEFAULT_VISION_MODEL,
        "chat_model": config.DEFAULT_CHAT_MODEL,
        "llm_provider": "auto",                       # auto | groq | nvidia
        "nvidia_vision_model": config.DEFAULT_NVIDIA_VISION_MODEL,
        "nvidia_chat_model": config.DEFAULT_NVIDIA_CHAT_MODEL,
        "nvidia_thinking": False,                     # Nemotron "enable_thinking" (slower, rarely needed here)
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _service_account() -> dict:
    raw = config.FIREBASE_SERVICE_ACCOUNT_JSON
    if not raw:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON is not set")
    if not raw.lstrip().startswith("{"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


def fs():
    global _client
    if _client is None:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(_service_account()))
        _client = firestore.client()
    return _client


def verify_token(token: str) -> dict:
    fs()  # make sure the app is initialised
    return fb_auth.verify_id_token(token)


# ---------------------------------------------------------------- users ----

def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def get_user(uid: str) -> dict:
    snap = fs().collection("users").document(uid).get()
    return _deep_merge(USER_DEFAULTS, snap.to_dict() if snap.exists else {})


def ensure_user(uid: str, claims: dict) -> dict:
    ref = fs().collection("users").document(uid)
    snap = ref.get()
    if not snap.exists:
        ref.set({
            "email": claims.get("email", ""),
            "name": claims.get("name", ""),
            "photo": claims.get("picture", ""),
            "created_at": now_iso(),
            "candidate": {"name": claims.get("name", ""), "email": claims.get("email", "")},
        })
    return get_user(uid)


def update_user(uid: str, fields: dict) -> None:
    fs().collection("users").document(uid).set(fields, merge=True)


def clear_user_field(uid: str, field: str, value=None) -> None:
    fs().collection("users").document(uid).set({field: value}, merge=True)


# -------------------------------------------------------------- secrets ----

def _secret_ref(uid: str):
    return fs().collection("users").document(uid).collection("private").document("secrets")


def get_secrets(uid: str) -> dict:
    snap = _secret_ref(uid).get()
    return crypto.decrypt_json((snap.to_dict() or {}).get("blob", "")) if snap.exists else {}


def save_secrets(uid: str, data: dict) -> None:
    _secret_ref(uid).set({"blob": crypto.encrypt_json(data), "updated_at": now_iso()})


# ----------------------------------------------------------------- jobs ----

def _jobs(uid: str):
    return fs().collection("users").document(uid).collection("jobs")


def list_jobs(uid: str, limit: int = 600) -> list[dict]:
    q = _jobs(uid).order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
    return [{**d.to_dict(), "id": d.id} for d in q.stream()]


def get_job(uid: str, job_id: str) -> dict | None:
    d = _jobs(uid).document(job_id).get()
    return {**d.to_dict(), "id": d.id} if d.exists else None


def add_job(uid: str, row: dict) -> str:
    ref = _jobs(uid).document()
    ref.set({**row, "created_at": now_iso()})
    return ref.id


def update_job(uid: str, job_id: str, fields: dict) -> None:
    _jobs(uid).document(job_id).update(fields)


def delete_job(uid: str, job_id: str) -> None:
    _jobs(uid).document(job_id).delete()


# ----------------------------------------------------------------- runs ----

def add_run(uid: str, data: dict) -> None:
    fs().collection("users").document(uid).collection("runs").add(data)


def list_runs(uid: str, limit: int = 15) -> list[dict]:
    q = (fs().collection("users").document(uid).collection("runs")
         .order_by("started_at", direction=firestore.Query.DESCENDING).limit(limit))
    return [{**d.to_dict(), "id": d.id} for d in q.stream()]


# ------------------------------------------------------------ schedules ----
# Top-level collection (not a sub-collection) so the cron tick can fetch every
# enabled schedule of every user with ONE simple query — no composite index.

def schedules():
    return fs().collection("schedules")


# ---------------------------------------------------------------- queue ----
# Posts fetched from HarvestAPI but not yet judged by the AI. They survive crashes,
# API errors, Stop and server restarts, and are checked first on the next run.

def _queue(uid: str):
    return fs().collection("users").document(uid).collection("queue")


def queue_add(uid: str, items: dict[str, dict]) -> None:
    pairs = list(items.items())
    for start in range(0, len(pairs), 400):          # Firestore batch limit is 500
        batch = fs().batch()
        for qid, data in pairs[start:start + 400]:
            batch.set(_queue(uid).document(qid), data)
        batch.commit()


def queue_list(uid: str) -> list[dict]:
    rows = [{**d.to_dict(), "id": d.id} for d in _queue(uid).stream()]
    rows.sort(key=lambda r: (r.get("attempts", 0), r.get("queued_at", "")))
    return rows


def queue_count(uid: str) -> int:
    return int(_queue(uid).count().get()[0][0].value)


def queue_update(uid: str, qid: str, fields: dict) -> None:
    _queue(uid).document(qid).update(fields)


def queue_remove(uid: str, qid: str) -> None:
    _queue(uid).document(qid).delete()


def queue_clear(uid: str) -> int:
    docs = list(_queue(uid).stream())
    for start in range(0, len(docs), 400):
        batch = fs().batch()
        for d in docs[start:start + 400]:
            batch.delete(d.reference)
        batch.commit()
    return len(docs)


def _seen_ref(uid: str):
    return fs().collection("users").document(uid).collection("private").document("seen")


def get_seen(uid: str) -> list[str]:
    snap = _seen_ref(uid).get()
    return list((snap.to_dict() or {}).get("ids", [])) if snap.exists else []


def save_seen(uid: str, ids: list[str]) -> None:
    _seen_ref(uid).set({"ids": ids[-config.SEEN_LIMIT:]})


# ---------------------------------------------------------------- audit ----

def add_audit(uid: str, data: dict) -> None:
    """What the chat assistant did after you approved it."""
    fs().collection("users").document(uid).collection("audit").add({**data, "at": now_iso()})
