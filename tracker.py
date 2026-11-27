"""What happens after a mail is sent: stages, follow-ups, analytics, export."""
import csv
import io
from collections import Counter
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font

import matching

STAGES = ("new", "sent", "followed_up", "replied", "interview", "offer", "rejected", "no_answer")
STAGE_LABELS = {"new": "Not applied", "sent": "Applied / Sent", "followed_up": "Followed up",
                "replied": "Got reply", "interview": "Interview", "offer": "Offer",
                "rejected": "Rejected", "no_answer": "No answer"}
ANSWERED = ("replied", "interview", "offer", "rejected")


def stage_of(job: dict) -> str:
    """Jobs saved before this feature have no stage: work it out from the status."""
    s = job.get("stage")
    if s in STAGES and s != "new":
        return s
    return "sent" if job.get("email_status") == "Sent" else "new"


def stage_fields(job: dict, stage: str) -> dict:
    """Database changes for moving a job to `stage`. 'new' makes it unsent again."""
    if stage not in STAGES:
        raise ValueError("unknown stage")
    out: dict = {"stage": stage}
    today = date.today().isoformat()
    if stage == "new":
        out.update(email_status="Not Sent", sent_date=None, follow_up_date=None, replied_date=None)
    elif job.get("email_status") != "Sent":
        out.update(email_status="Sent", sent_date=today)
    if stage in ANSWERED:
        out["replied_date"] = job.get("replied_date") or today
    elif stage != "new":
        out["replied_date"] = None
    if stage in ANSWERED or stage == "no_answer":
        out["follow_up_date"] = None
    return out


# ---------------------------------------------------------- follow-ups ----

def can_follow_up(job: dict) -> str:
    """'' = one follow-up may be sent now; otherwise the reason it may not."""
    if job.get("email_status") != "Sent":
        return "the first mail has not been sent"
    if job.get("follow_up_sent"):
        return "a follow-up was already sent (maximum one)"
    if stage_of(job) != "sent":
        return f"stage is '{STAGE_LABELS[stage_of(job)]}'"
    if not matching.EMAIL_RE.match((job.get("recruiter_email") or "").strip()):
        return "no recruiter email"
    return ""


def due_follow_ups(jobs: list[dict], today: date | None = None) -> list[dict]:
    today_s = (today or date.today()).isoformat()
    return [j for j in jobs if not can_follow_up(j) and j.get("follow_up_date")
            and j["follow_up_date"] <= today_s]


def follow_up_draft(job: dict, cand: dict) -> dict:
    """A fixed, polite template. No AI, so nothing invented or injected can get in."""
    first = (job.get("recruiter_name") or "").split()
    hello = f"Hi {first[0]}," if first else "Hello,"
    role = job.get("job_title") or "the role"
    at = f" at {job['company']}" if job.get("company") else ""
    when = f" on {job['sent_date']}" if job.get("sent_date") else " recently"
    sign = "\n".join(x for x in (cand.get("name"), cand.get("phone"), cand.get("email")) if x)
    body = (f"{hello}\n\nI wanted to follow up on my application for {role}{at}, which I sent{when}. "
            f"I am still very interested and would be glad to share anything else that helps.\n\n"
            f"Thank you for your time.\n\n{sign}").strip()
    original = job.get("sent_subject") or job.get("email_subject") or f"Application for {role}"
    subject = original if original.lower().startswith("re:") else f"Re: {original}"
    return {"subject": matching.one_line(subject), "body": body}


# ----------------------------------------------------------- analytics ----

def _rate(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def analytics(jobs: list[dict], min_score: int, mailed: set, today: date | None = None) -> dict:
    today = today or date.today()
    sent = [j for j in jobs if j.get("email_status") == "Sent"]
    stages = Counter(stage_of(j) for j in jobs)
    replied = sum(stage_of(j) in ANSWERED for j in sent)
    interviews = sum(stage_of(j) in ("interview", "offer") for j in sent)

    monday = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(7, -1, -1):
        start = monday - timedelta(weeks=i)
        end = start + timedelta(days=7)
        n = sum(1 for j in sent if j.get("sent_date") and start.isoformat() <= j["sent_date"] < end.isoformat())
        weeks.append({"week": start.isoformat(), "sent": n})

    def top(key: str) -> list[dict]:
        total, answered = Counter(), Counter()
        for j in sent:
            name = (j.get(key) or "").strip()
            if name:
                total[name] += 1
                answered[name] += stage_of(j) in ANSWERED
        return [{"name": n, "sent": c, "replies": answered[n], "reply_rate": _rate(answered[n], c)}
                for n, c in total.most_common(8)]

    held = sum(1 for j in jobs if j.get("email_status") != "Sent" and j.get("recruiter_email")
               and matching.hold_reason(j, min_score, mailed))
    return {"total": len(jobs), "sent": len(sent), "replied": replied, "interviews": interviews,
            "offers": stages["offer"], "held": held, "reply_rate": _rate(replied, len(sent)),
            "interview_rate": _rate(interviews, len(sent)), "stages": {s: stages[s] for s in STAGES},
            "weeks": weeks, "roles": top("job_title"), "locations": top("location"),
            "sources": top("source")}


# -------------------------------------------------------------- export ----

COLUMNS = [("company", "Company"), ("job_title", "Job title"), ("location", "Location"),
           ("recruiter_name", "Recruiter"), ("recruiter_email", "Recruiter email"), ("stage", "Stage"),
           ("email_status", "Email status"), ("sent_date", "Sent date"), ("follow_up_date", "Follow-up date"),
           ("replied_date", "Reply date"), ("job_match_score", "Match score"), ("source", "Source"),
           ("linkedin_url", "Post link"), ("apply_url", "Apply link"), ("email_subject", "Subject"),
           ("email_body", "Email body"), ("notes", "Notes"), ("created_at", "Saved at")]


def _cell(job: dict, key: str) -> str:
    value = STAGE_LABELS[stage_of(job)] if key == "stage" else job.get(key)
    text = "" if value is None else str(value)
    # job text is written by strangers: stop it running as a spreadsheet formula
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return ILLEGAL_CHARACTERS_RE.sub("", text)[:32000]


def to_csv(jobs: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in COLUMNS])
    for j in jobs:
        w.writerow([_cell(j, k) for k, _ in COLUMNS])
    return buf.getvalue().encode("utf-8-sig")       # BOM so Excel reads UTF-8 correctly


def to_xlsx(jobs: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Jobs"
    ws.append([label for _, label in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for j in jobs:
        ws.append([_cell(j, k) for k, _ in COLUMNS])
    for i, (key, _) in enumerate(COLUMNS):
        ws.column_dimensions[chr(ord("A") + i)].width = 50 if key == "email_body" else 20
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
