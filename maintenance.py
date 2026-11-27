"""Once a day per user (triggered by the cron tick): follow-up reminder and auto-delete."""
from datetime import date, datetime, timedelta, timezone

import config
import db
import notify
import resumes
import safety
import tracker


def purge_old_jobs(uid: str, days: int) -> int:
    """Deletes jobs saved more than `days` ago. Active conversations are kept."""
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    removed = 0
    for job in db.list_all_jobs(uid):
        if (job.get("created_at") or "9999") >= cutoff or tracker.stage_of(job) in ("interview", "offer"):
            continue
        if job.get("email_status") == "Sent" and job.get("recruiter_email"):
            safety.remember_contact(uid, job["recruiter_email"])     # still never mail twice
        resumes.discard_tailored(job)
        db.delete_job(uid, job["id"])
        removed += 1
    return removed


def run_for(uid: str) -> None:
    today = date.today().isoformat()
    if db.get_private(uid, "maintenance").get("day") == today:
        return
    db.set_private(uid, "maintenance", {"day": today})
    user = db.get_user(uid)
    purge_old_jobs(uid, int(user["settings"].get("retention_days") or 0))
    due = tracker.due_follow_ups(db.list_jobs(uid))
    if due:
        names = ", ".join((j.get("company") or "?") for j in due[:5])
        notify.notify(uid, f"{len(due)} follow-up(s) due: {names}. "
                           f"Open Job Mailer to check and approve them: {config.APP_BASE_URL}")


def daily() -> None:
    try:
        ids = db.user_ids()
    except Exception as exc:  # noqa: BLE001
        print(f"[maintenance] could not list users: {exc}")
        return
    for uid in ids:
        try:
            run_for(uid)
        except Exception as exc:  # noqa: BLE001
            print(f"[maintenance] {uid[:6]}: {exc}")
