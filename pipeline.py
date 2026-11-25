"""The three jobs the app can do for ONE user, plus the run/log machinery.

   search  -> LinkedIn posts (HarvestAPI) -> Groq -> jobs
   process -> screenshots in Supabase     -> Groq -> jobs (screenshot deleted)
   send    -> every un-sent job gets mailed from the user's own Gmail
"""
import re
import threading
import time
import traceback
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from groq import Groq

import ai
import config
import db
import storage
from gmail_service import GmailAuthRequired, GmailSender

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# "send" (used by schedules) = read new screenshots, then mail everything unsent.
# "send_only" is what the dashboard's "Send all unsent" button uses.
MODE_STEPS = {"search": ["search"], "process": ["process"], "send_only": ["send"],
              "send": ["process", "send"], "both": ["search", "process", "send"]}

# ---------------------------------------------------------------- run state ----

class RunState:
    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.lines: deque = deque(maxlen=600)
        self.running = False
        self.steps: list[str] = []
        self.started_at = ""

    def add(self, msg: str, kind: str = "info"):
        with self.lock:
            self.seq += 1
            self.lines.append({"seq": self.seq, "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                               "kind": kind, "msg": msg})

    def since(self, after: int) -> list[dict]:
        with self.lock:
            return [l for l in self.lines if l["seq"] > after]


_STATES: dict[str, RunState] = {}
_STATES_LOCK = threading.Lock()
_SEM = threading.Semaphore(config.MAX_CONCURRENT_RUNS)


def state_for(uid: str) -> RunState:
    with _STATES_LOCK:
        return _STATES.setdefault(uid, RunState())


def any_running() -> bool:
    return any(s.running for s in list(_STATES.values()))


def start_user_run(uid: str, steps: list[str], trigger: str = "manual") -> bool:
    st = state_for(uid)
    with _STATES_LOCK:
        if st.running:
            return False
        st.running = True
        st.steps = steps
        st.started_at = db.now_iso()
    st.add(f"▶ {trigger} run started: {' → '.join(steps)}", "head")
    threading.Thread(target=_worker, args=(uid, steps, trigger, st), daemon=True).start()
    return True


def _worker(uid, steps, trigger, st: RunState):
    stats, status = {}, "done"
    started = st.started_at
    try:
        with _SEM:
            ctx = Ctx(uid, st)
            for step in steps:
                {"search": step_search, "process": step_process, "send": step_send}[step](ctx)
            stats = ctx.stats
    except Exception:  # noqa: BLE001
        status = "error"
        st.add("❌ Run crashed: " + traceback.format_exc()[-600:], "fail")
    finally:
        summary = ", ".join(f"{k} {v}" for k, v in stats.items() if v) or "nothing to do"
        st.add(f"■ Finished ({status}): {summary}", "head")
        try:
            tail = [f"{l['t']} {l['msg']}" for l in list(st.lines)[-60:]]
            db.add_run(uid, {"trigger": trigger, "steps": steps, "status": status, "summary": summary,
                             "started_at": started, "finished_at": db.now_iso(), "log": tail})
        except Exception as exc:  # noqa: BLE001
            print(f"[pipeline] could not save run record: {exc}")
        st.running = False


# ------------------------------------------------------------------ context ----

class Ctx:
    def __init__(self, uid: str, st: RunState):
        self.uid, self.st = uid, st
        self.user = db.get_user(uid)
        self.secrets = db.get_secrets(uid)
        s = self.user["settings"]
        self.settings = s
        self.cand = self.user["candidate"]
        self.groq_keys = [k for k in self.secrets.get("groq_keys", []) if k]
        self.harvest_key = self.secrets.get("harvest_key", "")
        self.profile = (self.user.get("profile_text") or "").strip()
        self.stats = {"found": 0, "added": 0, "processed": 0, "failed": 0, "sent": 0, "send_failed": 0}
        self._resume = None

    def log(self, msg, kind="info"):
        self.st.add(msg, kind)

    @property
    def resume(self) -> tuple[str | None, bytes | None]:
        if self._resume is None:
            r = self.user.get("resume")
            try:
                self._resume = (r["name"], storage.download(config.RESUME_BUCKET, r["path"])) if r else (None, None)
            except Exception as exc:  # noqa: BLE001
                self.log(f"⚠ Couldn't load your resume ({exc}) — mails go out without attachment.", "warn")
                self._resume = (None, None)
        return self._resume

    def need_profile_warning(self):
        if not self.profile:
            self.log("⚠ No experience profile in Settings → emails will be generic.", "warn")


def new_row(f: dict, **extra) -> dict:
    return {
        "recruiter_email": (f.get("recruiter_email") or "").strip(),
        "recruiter_name": f.get("recruiter_name", ""), "company": f.get("company", ""),
        "job_title": f.get("job_title", ""), "location": f.get("location", ""),
        "job_match_score": ai.safe_score(f.get("job_match_score")), "priority": f.get("priority", ""),
        "email_subject": f.get("email_subject", ""), "email_body": f.get("email_body", ""),
        "email_status": "Not Sent", "sent_date": None, "follow_up_date": None,
        "source": "screenshot", "source_screenshot": "", "linkedin_url": "", "posted_date": None, **extra}


def signature(r: dict) -> tuple:
    return tuple((r.get(k) or "").strip().lower() for k in ("company", "job_title", "recruiter_email"))


# -------------------------------------------------------------- step: search ----

def step_search(ctx: Ctx):
    ctx.log("— LinkedIn search —", "head")
    if not ctx.groq_keys:
        return ctx.log("❌ Add a Groq API key in Settings first.", "fail")
    if not ctx.harvest_key:
        return ctx.log("❌ Add your HarvestAPI key in Settings first.", "fail")
    keywords = ctx.user.get("keywords") or []
    if not keywords:
        return ctx.log("❌ No search keywords — upload a resume / build your profile, or type them in Settings.", "fail")
    ctx.need_profile_warning()

    s = ctx.settings
    client = Groq(api_key=ctx.groq_keys[0])
    max_age, min_score = int(s["max_age_days"]), int(s["min_score"])
    locations = [l for l in (s.get("preferred_locations") or []) if l]
    strict_loc, inc_remote = bool(s.get("strict_location")), bool(s.get("include_remote", True))
    ctx.log(f"{len(keywords)} keyword(s) · posts ≤ {max_age} day(s) old · min score {min_score}")
    if locations:
        ctx.log(f"Preferred location(s): {', '.join(locations)}" + (" · strict filter on" if strict_loc else ""))
    queries = [f"{kw} {loc}" for kw in keywords for loc in locations] if locations else list(keywords)

    seen, posts = set(), []
    for kw in queries:
        try:
            got = ai.harvest_search(ctx.harvest_key, kw)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"  '{kw}': search failed ({exc})", "warn")
            continue
        fresh = [p for p in got if ai.is_recent(p, max_age)]
        ctx.log(f"  '{kw}': {len(got)} result(s), {len(fresh)} fresh", "muted")
        for p in fresh:
            pid = p.get("id") or p.get("linkedinUrl")
            if pid and pid not in seen:
                seen.add(pid)
                posts.append(p)

    ctx.stats["found"] += len(posts)
    if not posts:
        return ctx.log("No fresh posts found.")

    existing = db.list_jobs(ctx.uid)
    saved_urls = {r["linkedin_url"] for r in existing if r.get("linkedin_url")}
    sigs = {signature(r) for r in existing}
    skipped = {"not genuine": 0, "duplicate": 0, "low score": 0}

    for i, post in enumerate(posts, 1):
        url = post.get("linkedinUrl") or ""
        if url and url in saved_urls:
            continue
        author = (post.get("author") or {}).get("name") or ""
        tag = f"[{i}/{len(posts)}]"
        try:
            f = ai.extract_from_post(client, s["chat_model"], post.get("content") or "", ctx.profile or "(no profile)", ctx.cand)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"{tag} extraction failed: {exc}", "warn")
            if ai.is_quota_error(exc) and len(ctx.groq_keys) > 1:      # rotate key on quota
                ctx.groq_keys.append(ctx.groq_keys.pop(0))
                client = Groq(api_key=ctx.groq_keys[0])
            continue
        if not f.get("is_job_post"):
            continue
        has_email = bool((f.get("recruiter_email") or "").strip())
        if s["require_contact"] and (not has_email or not f.get("is_direct_listing", True)):
            skipped["not genuine"] += 1
            continue
        if locations and strict_loc and not ai.location_matches(
                locations, f.get("location", ""), post.get("content", "")[:1500], include_remote=inc_remote):
            skipped["other location"] = skipped.get("other location", 0) + 1
            continue
        score = ai.safe_score(f.get("job_match_score"))
        if score < min_score:
            skipped["low score"] += 1
            continue
        row = new_row(f, source="linkedin", linkedin_url=url,
                      recruiter_name=f.get("recruiter_name") or author,
                      posted_date=(ai.post_datetime(post).date().isoformat() if ai.post_datetime(post) else None))
        if signature(row) in sigs:
            skipped["duplicate"] += 1
            continue
        db.add_job(ctx.uid, row)
        sigs.add(signature(row))
        ctx.stats["added"] += 1
        ctx.log(f"{tag} ✅ {row['company'] or '?'} | {row['job_title'] or '?'} | score {score}", "success")

    ctx.log("Skipped: " + ", ".join(f"{v} {k}" for k, v in skipped.items()))


# ------------------------------------------------------------- step: process ----

def step_process(ctx: Ctx):
    ctx.log("— Screenshots —", "head")
    files = [f for f in storage.list_files(config.SCREENSHOT_BUCKET, ctx.uid)
             if Path(f["name"]).suffix.lower() in config.IMAGE_EXTENSIONS]
    if not files:
        return ctx.log("No screenshots waiting.")
    if not ctx.groq_keys:
        return ctx.log("❌ Add a Groq API key in Settings first.", "fail")
    ctx.need_profile_warning()
    clients = [Groq(api_key=k) for k in ctx.groq_keys]
    model = ctx.settings["vision_model"]

    for i, fi in enumerate(files, 1):
        name, path, tag = fi["name"], f"{ctx.uid}/{fi['name']}", f"[{i}/{len(files)}]"
        shown = re.sub(r"^[0-9a-f]{8}_", "", name)
        ctx.log(f"{tag} {shown} …", "muted")
        try:
            raw = storage.download(config.SCREENSHOT_BUCKET, path)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"{tag} ❌ download failed ({exc})", "fail")
            ctx.stats["failed"] += 1
            continue
        try:
            fields, flagged = ai.extract_with_retries(clients, model, raw, ctx.log, ctx.profile or "(no profile)", ctx.cand)
        except Exception as exc:  # noqa: BLE001  (e.g. a corrupt image)
            ctx.log(f"{tag} ❌ unreadable image ({exc}) — left in storage", "fail")
            ctx.stats["failed"] += 1
            continue
        if fields is None:
            ctx.log(f"{tag} ❌ failed on every attempt — left in storage, retry later", "fail")
            ctx.stats["failed"] += 1
            continue
        row = new_row(fields, source="screenshot", source_screenshot=shown)
        try:
            db.add_job(ctx.uid, row)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"{tag} ❌ couldn't save ({exc}) — left in storage", "fail")
            ctx.stats["failed"] += 1
            continue
        try:
            storage.remove(config.SCREENSHOT_BUCKET, [path])
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"{tag} ⚠ saved, but couldn't delete the image ({exc})", "warn")
        ctx.stats["processed"] += 1
        ctx.log(f"{tag} {'⚠' if flagged else '✅'} {row['company'] or '?'} | {row['job_title'] or '?'} | "
                f"score {row['job_match_score']} | {row['priority']}", "warn" if flagged else "success")


# ----------------------------------------------------------------- step: send ----

def make_sender(ctx_secrets: dict, user: dict) -> GmailSender:
    g = user.get("gmail") or {}
    return GmailSender(ctx_secrets, from_name=(user["candidate"].get("name") or ""), from_email=g.get("email", ""))


def send_job(uid: str, job: dict, sender: GmailSender, resume: tuple, user: dict) -> str:
    """Mails one job and marks it Sent. Raises on failure."""
    cand = user["candidate"]
    name, data = resume
    mid = sender.send(job["recruiter_email"],
                      job.get("email_subject") or f"Application - {cand.get('name', '')}",
                      job.get("email_body") or "", data, name)
    today = date.today()
    db.update_job(uid, job["id"], {"email_status": "Sent", "sent_date": today.isoformat(),
                                   "follow_up_date": (today + timedelta(days=7)).isoformat(),
                                   "gmail_message_id": mid, "send_error": ""})
    return mid


def step_send(ctx: Ctx):
    ctx.log("— Sending unsent mails —", "head")
    pending = [j for j in db.list_jobs(ctx.uid)
               if j.get("email_status") != "Sent" and EMAIL_RE.match((j.get("recruiter_email") or "").strip())]
    if not pending:
        return ctx.log("Nothing to send — every job with an email address is already Sent.")
    cap = int(ctx.settings["max_send_per_run"])
    pending.sort(key=lambda j: -ai.safe_score(j.get("job_match_score")))
    if len(pending) > cap:
        ctx.log(f"{len(pending)} waiting — sending the best {cap} this run (limit in Settings).", "warn")
        pending = pending[:cap]
    try:
        sender = make_sender(ctx.secrets, ctx.user)
    except GmailAuthRequired as exc:
        db.update_user(ctx.uid, {"gmail": {"connected": False}})
        return ctx.log(f"❌ {exc}. Connect Gmail in Settings.", "fail")
    resume = ctx.resume
    if not resume[0]:
        ctx.log("⚠ No resume uploaded — mails go out without an attachment.", "warn")

    for i, job in enumerate(pending, 1):
        tag = f"[{i}/{len(pending)}]"
        try:
            send_job(ctx.uid, job, sender, resume, ctx.user)
            ctx.stats["sent"] += 1
            ctx.log(f"{tag} ✉ sent → {job['recruiter_email']} ({job.get('company') or '?'})", "success")
        except Exception as exc:  # noqa: BLE001
            ctx.stats["send_failed"] += 1
            msg = str(exc)[:200]
            ctx.log(f"{tag} ❌ {job['recruiter_email']}: {msg}", "fail")
            try:
                db.update_job(ctx.uid, job["id"], {"send_error": msg})
            except Exception:  # noqa: BLE001
                pass
            if "rateLimit" in msg or "quota" in msg.lower():
                ctx.log("Gmail's daily limit looks reached — stopping.", "warn")
                break
        time.sleep(1.5)       # gentle pacing; Gmail flags bursts
