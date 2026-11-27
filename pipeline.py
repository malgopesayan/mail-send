"""The three jobs the app can do for ONE user, plus the run/log machinery.

   search  -> LinkedIn posts (HarvestAPI) -> LLM -> jobs
   process -> screenshots in Supabase     -> LLM -> jobs (screenshot deleted)
   send    -> every un-sent job gets mailed from the user's own Gmail
"""
import hashlib
import re
import threading
import traceback
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import ai
import config
import db
import matching
import notify
import resumes
import safety
import sources
import tracker
from llm import LLM
import storage
from gmail_service import GmailAuthRequired, GmailSender

EMAIL_RE = matching.EMAIL_RE
# "send" (used by schedules) = read new screenshots, then mail everything unsent.
# "send_only" is what the dashboard's "Send all unsent" button uses.
MODE_STEPS = {"search": ["search"], "process": ["process"], "send_only": ["send"],
              "send": ["process", "send"], "both": ["search", "process", "send"], "resume": ["resume"],
              "boards": ["boards"]}
MAX_ATTEMPTS = 4            # a post that breaks the AI this many times is dropped
OUTAGE_STREAK = 3           # this many failures in a row = the AI is down: pause, keep the rest
RETRY_WAIT_SECONDS = 8

# ---------------------------------------------------------------- run state ----

class RunState:
    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.lines: deque = deque(maxlen=600)
        self.running = False
        self.steps: list[str] = []
        self.started_at = ""
        self.mode = ""
        self.stop = threading.Event()

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


def start_user_run(uid: str, steps: list[str], trigger: str = "manual", mode: str = "",
                   only: dict[str, str] | None = None) -> bool:
    """`only` = {job_id: fingerprint}: send exactly these drafts, unchanged (assistant approvals)."""
    st = state_for(uid)
    with _STATES_LOCK:
        if st.running:
            return False
        st.running = True
        st.stop.clear()
        st.steps = steps
        st.mode = mode or trigger
        st.started_at = db.now_iso()
    st.add(f"▶ {trigger} run started: {' → '.join(steps)}", "head")
    threading.Thread(target=_worker, args=(uid, steps, trigger, st, only), daemon=True).start()
    return True


def stop_run(uid: str) -> bool:
    st = state_for(uid)
    if not st.running:
        return False
    if not st.stop.is_set():
        st.stop.set()
        st.add("⏹ Stop requested — finishing the current item…", "warn")
    return True


def _worker(uid, steps, trigger, st: RunState, only=None):
    stats, status = {}, "done"
    started = st.started_at
    ctx = None
    try:
        with _SEM:
            ctx = Ctx(uid, st, only)
            for step in steps:
                if st.stop.is_set():
                    break
                {"search": step_search, "resume": step_resume, "process": step_process,
                 "send": step_send, "boards": step_boards}[step](ctx)
            if st.stop.is_set():
                status = "stopped"
    except Exception:  # noqa: BLE001
        status = "error"
        st.add("❌ Run crashed: " + traceback.format_exc()[-600:], "fail")
    finally:
        stats = ctx.stats if ctx else stats
        summary = ", ".join(f"{k} {v}" for k, v in stats.items() if v) or "nothing to do"
        st.add(f"■ Finished ({status}): {summary}", "head")
        try:
            tail = [f"{l['t']} {l['msg']}" for l in list(st.lines)[-60:]]
            db.add_run(uid, {"trigger": trigger, "steps": steps, "status": status, "summary": summary,
                             "started_at": started, "finished_at": db.now_iso(), "log": tail})
        except Exception as exc:  # noqa: BLE001
            print(f"[pipeline] could not save run record: {exc}")
        st.running = False
        if trigger == "schedule":
            text = f"Job Mailer run {status}: {summary}"
            for problem in notify.notify(uid, text):
                st.add(f"⚠ Alert not sent: {problem}", "warn")


# ------------------------------------------------------------------ context ----

class Ctx:
    def __init__(self, uid: str, st: RunState, only: dict[str, str] | None = None):
        self.uid, self.st, self.only = uid, st, only
        self.user = db.get_user(uid)
        self.secrets = db.get_secrets(uid)
        s = self.user["settings"]
        self.settings = s
        self.cand = self.user["candidate"]
        self.llm = LLM(uid, s, self.secrets, log=self.log)
        self.harvest_key = self.secrets.get("harvest_key", "")
        self.profile = (self.user.get("profile_text") or "").strip()
        self.stats = {"found": 0, "checked": 0, "added": 0, "processed": 0, "failed": 0, "sent": 0, "send_failed": 0}
        self._resume = None

    def log(self, msg, kind="info"):
        self.st.add(msg, kind)

    def stopped(self) -> bool:
        return self.st.stop.is_set()

    def wait(self, seconds: float) -> bool:
        """Interruptible sleep. True = Stop was pressed."""
        return self.st.stop.wait(seconds)

    def resume_for(self, job: dict) -> tuple[str | None, bytes | None]:
        """(file name, bytes) to attach to this job. Cached per run."""
        if self._resume is None:
            self._resume = resumes.Loader(self.uid, self.user, self.log)
        return self._resume(job)

    def need_profile_warning(self):
        if not self.profile:
            self.log("⚠ No experience profile in Settings → emails will be generic.", "warn")


def new_row(f: dict, **extra) -> dict:
    """Only these keys can ever come from the AI; every one is cleaned and length-capped."""
    line = matching.one_line
    priority = line(f.get("priority"), 40)
    if priority not in ("High", "Medium", "Low") and not priority.upper().startswith("REVIEW"):
        priority = ""
    return {
        "recruiter_email": line(f.get("recruiter_email"), 200),
        "recruiter_name": line(f.get("recruiter_name"), 120), "company": line(f.get("company"), 160),
        "job_title": line(f.get("job_title"), 160), "location": line(f.get("location"), 120),
        "job_match_score": ai.safe_score(f.get("job_match_score")), "priority": priority,
        "email_subject": line(f.get("email_subject"), 200),
        "email_body": str(f.get("email_body") or "").replace("\x00", "")[:6000],
        "email_status": "Not Sent", "sent_date": None, "follow_up_date": None, "needs_review": "", "reviewed": False,
        "stage": "new", "follow_up_sent": None, "replied_date": None, "apply_url": "", "post_text": "",
        "resume_id": "", "tailored": None, "notes": "",
        "source": "screenshot", "source_screenshot": "", "linkedin_url": "", "posted_date": None, **extra}


def signature(r: dict) -> tuple:
    return tuple((r.get(k) or "").strip().lower() for k in ("company", "job_title", "recruiter_email"))


def fingerprint(job: dict) -> str:
    """Identifies exactly what would be sent, so an approval cannot be re-used for different content."""
    parts = [str(job.get(k) or "") for k in ("recruiter_email", "email_subject", "email_body", "resume_id")]
    parts.append(str((job.get("tailored") or {}).get("path") or ""))      # the attachment counts too
    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def post_key(post_id: str) -> str:
    return hashlib.sha1(post_id.encode()).hexdigest()[:24]


# -------------------------------------------------------------- step: search ----

def step_search(ctx: Ctx):
    ctx.log("— LinkedIn search —", "head")
    if not ctx.llm.ready():
        return ctx.log("❌ " + ctx.llm.missing_message(), "fail")

    # 1) whatever the last run could not check comes FIRST
    pending = db.queue_list(ctx.uid)
    if pending:
        ctx.log(f"↻ {len(pending)} post(s) left over from an earlier run — checking these first.", "head")
        if not process_queue(ctx, pending):
            return                      # stopped or AI down: don't spend HarvestAPI credits now

    if not ctx.harvest_key:
        return ctx.log("❌ Add your HarvestAPI key in Settings first.", "fail")
    keywords = ctx.user.get("keywords") or []
    if not keywords:
        return ctx.log("❌ No search keywords — upload a resume / build your profile, or type them in Settings.", "fail")

    # 2) fetch new posts and park them in the queue before the AI sees any of them
    s = ctx.settings
    max_age = int(s["max_age_days"])
    locations = [l for l in (s.get("preferred_locations") or []) if l]
    ctx.log(f"{len(keywords)} keyword(s) · posts ≤ {max_age} day(s) old · min score {s['min_score']}"
            + (" · strict matching" if s.get("strict_match", True) else ""))
    if locations:
        ctx.log(f"Preferred location(s): {', '.join(locations)}"
                + (" · strict filter on" if s.get("strict_location") else ""))
    queries = [f"{kw} {loc}" for kw in keywords for loc in locations] if locations else list(keywords)

    seen = set(db.get_seen(ctx.uid))
    saved_urls = {r["linkedin_url"] for r in db.list_jobs(ctx.uid) if r.get("linkedin_url")}
    queued = {q["id"] for q in db.queue_list(ctx.uid)}
    for kw in queries:
        if ctx.stopped():
            return
        try:
            got = ai.harvest_search(ctx.harvest_key, kw)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"  '{kw}': search failed ({exc})", "warn")
            continue
        fresh = [p for p in got if ai.is_recent(p, max_age)]
        batch = {}
        for p in fresh:
            pid = p.get("id") or p.get("linkedinUrl")
            if not pid:
                continue
            qid, url = post_key(str(pid)), p.get("linkedinUrl") or ""
            if qid in seen or qid in queued or (url and url in saved_urls):
                continue
            queued.add(qid)
            dt = ai.post_datetime(p)
            batch[qid] = {"content": (p.get("content") or "")[:4000], "url": url,
                          "author": matching.one_line((p.get("author") or {}).get("name"), 120),
                          "posted": dt.date().isoformat() if dt else None,
                          "attempts": 0, "queued_at": db.now_iso()}
        if batch:
            db.queue_add(ctx.uid, batch)
            ctx.stats["found"] += len(batch)
        ctx.log(f"  '{kw}': {len(got)} result(s), {len(fresh)} fresh, {len(batch)} new", "muted")

    # 3) judge them
    todo = db.queue_list(ctx.uid)
    if not todo:
        return ctx.log("No new posts to check.")
    process_queue(ctx, todo)


def step_resume(ctx: Ctx):
    """Check only the leftover posts (no HarvestAPI calls)."""
    ctx.log("— Continuing unchecked posts —", "head")
    if not ctx.llm.ready():
        return ctx.log("❌ " + ctx.llm.missing_message(), "fail")
    pending = db.queue_list(ctx.uid)
    if not pending:
        return ctx.log("Nothing waiting to be checked.")
    process_queue(ctx, pending)


def process_queue(ctx: Ctx, items: list[dict]) -> bool:
    """AI-checks queued posts. A post leaves the queue only after it has really been judged.
    Returns False if it had to stop early (Stop button or AI unavailable)."""
    ctx.need_profile_warning()
    ctx.log(f"AI provider: {ctx.llm.provider.upper()} · {len(items)} post(s) to check", "muted")
    s, keywords = ctx.settings, ctx.user.get("keywords") or []
    strict = bool(s.get("strict_match", True))
    exclude = s.get("exclude_words") or []
    locations = [l for l in (s.get("preferred_locations") or []) if l]
    min_score = int(s["min_score"])
    sigs = {signature(r) for r in db.list_jobs(ctx.uid)}
    seen = db.get_seen(ctx.uid)
    skipped: dict[str, int] = {}
    streak: list[dict] = []         # consecutive provider failures
    finished = True
    expired_before = (date.today() - timedelta(days=config.QUEUE_EXPIRE_DAYS)).isoformat()

    def done(item, reason=""):
        db.queue_remove(ctx.uid, item["id"])
        seen.append(item["id"])
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
        if len(seen) % 20 == 0:
            db.save_seen(ctx.uid, seen)

    def bump(item):
        attempts = item.get("attempts", 0) + 1
        if attempts >= MAX_ATTEMPTS:
            done(item, "gave up after repeated errors")
        else:
            db.queue_update(ctx.uid, item["id"], {"attempts": attempts})

    try:
        for i, item in enumerate(items, 1):
            if ctx.stopped():
                finished = False
                break
            tag, content = f"[{i}/{len(items)}]", item.get("content") or ""
            if (item.get("queued_at") or "9999")[:10] < expired_before:
                done(item, "expired")
                continue
            reason = matching.prefilter_post(content, keywords, exclude, strict)
            if reason:
                done(item, reason)
                continue
            try:
                f = ai.extract_from_post(ctx.llm, content, ctx.profile or "(no profile)", ctx.cand, keywords)
            except ai.BadOutput as exc:
                ctx.log(f"{tag} {exc} — will retry later", "warn")
                bump(item)
                continue
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"{tag} AI error: {str(exc)[:160]}", "warn")
                streak.append(item)
                if len(streak) >= OUTAGE_STREAK:
                    finished = False
                    break
                if ctx.wait(RETRY_WAIT_SECONDS * len(streak)):
                    finished = False
                    break
                continue
            for failed in streak:       # the AI works, so those posts themselves were the problem
                bump(failed)
            streak = []
            ctx.stats["checked"] += 1
            done(item, judge_post(ctx, tag, item, f, content, keywords, strict, exclude, locations, min_score, sigs))
    finally:
        db.save_seen(ctx.uid, seen)

    if skipped:
        ctx.log("Skipped: " + ", ".join(f"{v} {k}" for k, v in skipped.items()), "muted")
    left = db.queue_count(ctx.uid)
    if not finished:
        why = "Stopped" if ctx.stopped() else "AI is unavailable (rate limit or API error) — pausing"
        ctx.log(f"⏸ {why}. {left} unchecked post(s) saved; the next run checks them first.", "warn")
    return finished


def judge_post(ctx, tag, item, f, content, keywords, strict, exclude, locations, min_score, sigs) -> str:
    """Saves the job if it passes every gate. Returns the skip reason, or ''."""
    s = ctx.settings
    if f.get("is_job_post") is not True:
        return "not a job post"
    email = str(f.get("recruiter_email") or "").strip()
    if email and not matching.email_in_text(email, content):
        ctx.log(f"{tag} ⚠ AI returned an email that is not in the post — discarded", "warn")
        email = ""
    f["recruiter_email"] = email
    if s["require_contact"] and (not email or f.get("is_direct_listing") is False):
        return "no direct recruiter email"
    title = matching.one_line(f.get("job_title"), 160)
    if strict and (f.get("role_match") is not True or not matching.title_matches(title, keywords)):
        return "off-target role"
    if matching.excluded_word(exclude, title):
        return "blocked word"
    if locations and s.get("strict_location") and not ai.location_matches(
            locations, str(f.get("location", "")), content[:1500], include_remote=bool(s.get("include_remote", True))):
        return "other location"
    score = ai.safe_score(f.get("job_match_score"))
    if score < min_score:
        return "low score"
    row = new_row(f, source="linkedin", linkedin_url=item.get("url") or "", posted_date=item.get("posted"),
                  post_text=content[:4000])
    row["recruiter_name"] = row["recruiter_name"] or item.get("author") or ""
    if signature(row) in sigs:
        return "duplicate"
    problems = matching.draft_problems(row, ctx.cand, content, ctx.profile)
    row["needs_review"] = "; ".join(problems)
    db.add_job(ctx.uid, row)
    sigs.add(signature(row))
    ctx.stats["added"] += 1
    ctx.log(f"{tag} {'🛡 held for review:' if problems else '✅'} {row['company'] or '?'} | "
            f"{row['job_title'] or '?'} | score {score}", "warn" if problems else "success")
    return ""


# ------------------------------------------------------------- step: process ----
def step_process(ctx: Ctx):
    ctx.log("— Screenshots —", "head")
    files = [f for f in storage.list_files(config.SCREENSHOT_BUCKET, ctx.uid)
             if Path(f["name"]).suffix.lower() in config.IMAGE_EXTENSIONS]
    if not files:
        return ctx.log("No screenshots waiting.")
    if not ctx.llm.ready():
        return ctx.log("❌ " + ctx.llm.missing_message(), "fail")
    ctx.need_profile_warning()
    ctx.log(f"AI provider: {ctx.llm.provider.upper()}", "muted")

    for i, fi in enumerate(files, 1):
        if ctx.stopped():
            return ctx.log(f"⏹ Stopped — {len(files) - i + 1} screenshot(s) still waiting.", "warn")
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
            fields, flagged = ai.extract_with_retries(ctx.llm, raw, ctx.log, ctx.profile or "(no profile)", ctx.cand,
                                                      wait=ctx.wait)
        except Exception as exc:  # noqa: BLE001  (e.g. a corrupt image)
            ctx.log(f"{tag} ❌ unreadable image ({exc}) — left in storage", "fail")
            ctx.stats["failed"] += 1
            continue
        if fields is None:
            ctx.log(f"{tag} ❌ failed on every attempt — left in storage, retry later", "fail")
            ctx.stats["failed"] += 1
            continue
        row = new_row(fields, source="screenshot", source_screenshot=shown)
        problems = matching.draft_problems(row, ctx.cand, "", ctx.profile)
        if row["recruiter_email"] and not EMAIL_RE.match(row["recruiter_email"]):
            problems.append("recruiter email looks malformed")
        row["needs_review"] = "; ".join(problems)
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


_SEND_LOCKS: dict[str, threading.Lock] = {}


def send_lock(uid: str) -> threading.Lock:
    with _STATES_LOCK:
        return _SEND_LOCKS.setdefault(uid, threading.Lock())


def send_job(uid: str, job: dict, sender: GmailSender, resume: tuple, user: dict) -> str:
    """Mails one job and marks it Sent. Raises on failure. Call while holding send_lock(uid)."""
    cand = user["candidate"]
    name, data = resume
    to = (job.get("recruiter_email") or "").strip()
    if not EMAIL_RE.match(to):
        raise ValueError("invalid recruiter email")
    safety.check_can_send(uid, user, to)
    subject = matching.one_line(job.get("email_subject") or f"Application - {cand.get('name', '')}")
    body = job.get("email_body") or ""
    if user["settings"].get("opt_out_line") and safety.OPT_OUT_LINE not in body:
        body = f"{body.rstrip()}\n\n{safety.OPT_OUT_LINE}"
    sent = sender.send(to, subject, body, data, name)
    today = date.today()
    db.update_job(uid, job["id"], {"email_status": "Sent", "sent_date": today.isoformat(),
                                   "follow_up_date": (today + timedelta(days=7)).isoformat(),
                                   "stage": "sent", "follow_up_sent": None, "replied_date": None,
                                   "gmail_message_id": sent["id"], "gmail_thread_id": sent["thread_id"],
                                   "rfc_message_id": sent["message_id"], "sent_subject": subject,
                                   "sent_to": to, "sent_from": sender.from_email, "send_error": ""})
    safety.remember_contact(uid, to)
    safety.record_send(uid, user)
    return sent["id"]


def send_follow_up(uid: str, job: dict, sender: GmailSender, user: dict, subject: str, body: str) -> None:
    """One follow-up, in the same Gmail thread when possible. Call while holding send_lock(uid)."""
    why = tracker.can_follow_up(job)
    if why:
        raise safety.SendBlocked(f"follow-up not allowed: {why}")
    to = job["recruiter_email"].strip()
    safety.check_can_send(uid, user, to)
    # threading only works from the same mailbox to the same person as the first mail
    same = (job.get("sent_from") == sender.from_email and (job.get("sent_to") or "").lower() == to.lower()
            and job.get("gmail_thread_id") and job.get("rfc_message_id"))
    if same:
        subject = tracker.follow_up_draft(job, user["candidate"])["subject"]     # must match the thread
    sender.send(to, matching.one_line(subject), body,
                thread_id=job["gmail_thread_id"] if same else None,
                reply_to_id=job["rfc_message_id"] if same else None)
    db.update_job(uid, job["id"], {"stage": "followed_up", "follow_up_sent": date.today().isoformat(),
                                   "follow_up_date": None, "send_error": ""})
    safety.record_send(uid, user)


def sendable(uid: str, user: dict) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(jobs that may be mailed now, [(held job, why)]), best match first."""
    jobs = db.list_jobs(uid)
    mailed = safety.mailed_for(uid, jobs)
    min_score = int(user["settings"]["min_score"])
    blocklist = user["settings"].get("blocklist")
    ready, held = [], []
    for j in sorted(jobs, key=lambda j: -ai.safe_score(j.get("job_match_score"))):
        addr = (j.get("recruiter_email") or "").strip()
        if j.get("email_status") == "Sent" or not EMAIL_RE.match(addr):
            continue
        why = ("on your do-not-contact list" if safety.is_blocked(addr, blocklist)
               else matching.hold_reason(j, min_score, mailed))
        if why:
            held.append((j, why))
        else:
            ready.append(j)
            mailed.add(addr.lower())          # one mail per address per run
    return ready, held


def step_send(ctx: Ctx):
    ctx.log("— Sending unsent mails —", "head")
    pending, held = sendable(ctx.uid, ctx.user)
    if ctx.only is not None:
        pending = [j for j in pending if ctx.only.get(j["id"]) == fingerprint(j)]
        if len(pending) < len(ctx.only):
            ctx.log(f"⚠ {len(ctx.only) - len(pending)} job(s) changed after you approved — not sent.", "warn")
    elif held:
        ctx.log(f"🛡 {len(held)} job(s) held back (open them on the Dashboard to see why).", "warn")
    if not pending:
        return ctx.log("Nothing to send.")
    left = safety.usage(ctx.uid, ctx.user)["left"]
    if left <= 0:
        return ctx.log("Today's sending limit is reached — the rest wait for tomorrow.", "warn")
    cap = int(ctx.settings["max_send_per_run"])
    if left < min(cap, len(pending)):
        ctx.log(f"Daily limit: only {left} more mail(s) today.", "warn")
        cap = left
    if len(pending) > cap:
        ctx.log(f"{len(pending)} waiting — sending the best {cap} this run (limit in Settings).", "warn")
        pending = pending[:cap]
    try:
        sender = make_sender(ctx.secrets, ctx.user)
    except GmailAuthRequired as exc:
        db.update_user(ctx.uid, {"gmail": {"connected": False}})
        return ctx.log(f"❌ {exc}. Connect Gmail in Settings.", "fail")
    if not resumes.all_resumes(ctx.user):
        ctx.log("⚠ No resume uploaded — mails go out without an attachment.", "warn")

    for i, job in enumerate(pending, 1):
        if ctx.stopped():
            return ctx.log(f"⏹ Stopped — {len(pending) - i + 1} mail(s) not sent.", "warn")
        tag = f"[{i}/{len(pending)}]"
        try:
            with send_lock(ctx.uid):
                now = db.get_job(ctx.uid, job["id"])          # it may have been sent/edited meanwhile
                if not now or now.get("email_status") == "Sent" or fingerprint(now) != fingerprint(job):
                    ctx.log(f"{tag} skipped — the job changed while the run was going", "muted")
                    continue
                send_job(ctx.uid, now, sender, ctx.resume_for(now), ctx.user)
            ctx.stats["sent"] += 1
            ctx.log(f"{tag} ✉ sent → {job['recruiter_email']} ({job.get('company') or '?'})", "success")
        except safety.SendBlocked as exc:
            ctx.log(f"{tag} 🛡 {job['recruiter_email']}: {exc}", "warn")
            if not exc.permanent:
                break                                       # daily limit
            ctx.stats["send_failed"] += 1
            db.update_job(ctx.uid, job["id"], {"send_error": str(exc), "needs_review": str(exc),
                                               "reviewed": False})
            continue
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
        ctx.wait(1.5)         # gentle pacing; Gmail flags bursts


# ---------------------------------------------------------------- step: boards ----

def step_boards(ctx: Ctx):
    """Company career pages (Greenhouse, Lever) and Adzuna: 'apply link' cards, no email."""
    ctx.log("— Company job boards —", "head")
    added = sources.collect(ctx.uid, ctx.user, ctx.secrets, ctx.log, ctx.stopped, new_row)
    ctx.stats["added"] += added
    if added:
        ctx.log(f"{added} new job(s) saved. Open each one and use its Apply link.", "success")
