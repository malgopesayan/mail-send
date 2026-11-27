"""Per-user schedules. Render Free sleeps when idle, so an in-process timer can't
work. GitHub Actions calls POST /api/cron/tick every ~20 min: it wakes the
service, which looks at EVERY user's enabled schedules in Firestore and starts
the ones that are due (in each user's own timezone)."""
import secrets as pysecrets
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, field_validator

import config
import db
import pipeline
import storage
from auth import current_user

router = APIRouter()
GRACE = timedelta(minutes=config.SCHEDULE_GRACE_MIN)
MAX_SCHEDULES_PER_USER = 8


class ScheduleIn(BaseModel):
    label: str = "Run"
    run_at: str
    enabled: bool = True
    mode: Literal["search", "send", "both"] = "both"

    @field_validator("run_at")
    @classmethod
    def _check(cls, v):
        try:
            h, m = (int(x) for x in str(v).split(":")[:2])
        except ValueError:
            raise ValueError("run_at must look like HH:MM")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("run_at out of range")
        return f"{h:02d}:{m:02d}"


def _own(uid: str, sid: str):
    ref = db.schedules().document(sid)
    snap = ref.get()
    if not snap.exists or snap.to_dict().get("uid") != uid:
        raise HTTPException(404, "No such schedule")
    return ref


@router.get("/api/schedules")
def list_schedules(u: dict = Depends(current_user)):
    rows = [{**d.to_dict(), "id": d.id} for d in db.schedules().where("uid", "==", u["uid"]).stream()]
    rows.sort(key=lambda r: r.get("run_at", ""))
    return {"timezone": db.get_user(u["uid"])["settings"]["timezone"], "schedules": rows}


@router.post("/api/schedules")
def create_schedule(body: ScheduleIn, u: dict = Depends(current_user)):
    mine = list(db.schedules().where("uid", "==", u["uid"]).stream())
    if len(mine) >= MAX_SCHEDULES_PER_USER:
        raise HTTPException(400, f"Max {MAX_SCHEDULES_PER_USER} schedules")
    ref = db.schedules().document()
    ref.set({**body.model_dump(), "uid": u["uid"], "last_run_at": None, "created_at": db.now_iso()})
    return {"id": ref.id}


@router.patch("/api/schedules/{sid}")
def update_schedule(sid: str, body: ScheduleIn, u: dict = Depends(current_user)):
    _own(u["uid"], sid).update(body.model_dump())
    return {"ok": True}


@router.delete("/api/schedules/{sid}")
def delete_schedule(sid: str, u: dict = Depends(current_user)):
    _own(u["uid"], sid).delete()
    return {"ok": True}


@router.get("/api/healthz")
def healthz():
    return {"ok": True, "running": pipeline.any_running()}


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo(config.DEFAULT_TZ)


@router.post("/api/cron/tick")
def cron_tick(x_cron_secret: str = Header(default="")):
    if not config.CRON_SECRET or not pysecrets.compare_digest(x_cron_secret, config.CRON_SECRET):
        raise HTTPException(401, "bad cron secret")
    storage.ping()                      # keeps the free Supabase project from pausing

    tz_cache: dict[str, ZoneInfo] = {}
    due: dict[str, list] = {}           # uid -> [schedule docs]
    for d in db.schedules().where("enabled", "==", True).stream():
        row = {**d.to_dict(), "id": d.id}
        uid = row["uid"]
        if uid not in tz_cache:
            tz_cache[uid] = _tz(db.get_user(uid)["settings"]["timezone"])
        tz = tz_cache[uid]
        now = datetime.now(tz)
        h, m = (int(x) for x in row["run_at"].split(":")[:2])
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < slot or now - slot > GRACE:
            continue
        last = row.get("last_run_at")
        if last and datetime.fromisoformat(last).astimezone(tz) >= slot:
            continue
        due.setdefault(uid, []).append(row)

    fired = []
    for uid, rows in due.items():
        steps = []
        for r in rows:
            for s in pipeline.MODE_STEPS[r.get("mode", "both")]:
                if s not in steps:
                    steps.append(s)
        steps.sort(key=["search", "process", "send"].index)       # always search → process → send
        if pipeline.start_user_run(uid, steps, trigger="schedule", mode="schedule"):
            stamp = datetime.now(tz_cache[uid]).isoformat()
            for r in rows:
                db.schedules().document(r["id"]).update({"last_run_at": stamp})
            fired.append(uid[:6])
    return {"woken": True, "users_started": len(fired), "running": pipeline.any_running()}
