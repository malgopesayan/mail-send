"""Sending safety: address check (MX), do-not-contact list, contact history,
daily limit with warm-up. All plain code - no AI decides any of this."""
import hashlib
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import dns.exception
import dns.resolver

import config
import db
import matching

TYPO_DOMAINS = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gmail.co": "gmail.com", "gmail.con": "gmail.com",
    "gamil.com": "gmail.com", "gnail.com": "gmail.com", "hotmial.com": "hotmail.com",
    "hotmail.con": "hotmail.com", "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com",
    "yahoo.con": "yahoo.com", "outlok.com": "outlook.com", "outloook.com": "outlook.com",
}
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
    "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com", "throwawaymail.com",
    "maildrop.cc", "dispostable.com", "fakeinbox.com",
}
OPT_OUT_LINE = "P.S. If you would rather not hear from me, just reply \"no\" and I will not email again."

_MX_CACHE: dict[str, bool] = {}


class SendBlocked(Exception):
    """This mail must not be sent. `permanent` = the job should be held for review."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


# ------------------------------------------------------------- address ----

def _resolve(domain: str, rtype: str):
    return dns.resolver.resolve(domain, rtype, lifetime=5)


def domain_accepts_mail(domain: str) -> bool:
    """False only when DNS says for certain that no mail can arrive.
    A timeout or DNS outage returns True, so a network blip never blocks everything."""
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]
    try:
        records = _resolve(domain, "MX")
        # "null MX" (RFC 7505): a single record "." means the domain refuses mail
        ok = any(str(r.exchange) != "." for r in records)
    except dns.resolver.NXDOMAIN:
        ok = False
    except dns.resolver.NoAnswer:          # no MX: mail may still go to the A / AAAA record
        ok = False
        for rtype in ("A", "AAAA"):
            try:
                _resolve(domain, rtype)
                ok = True
                break
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                continue
            except dns.exception.DNSException:
                return True
    except dns.exception.DNSException:
        return True                        # unknown: do not block, do not cache
    _MX_CACHE[domain] = ok
    return ok


def address_problem(email: str) -> str:
    """'' = the address looks deliverable. Only the DOMAIN can be checked, not the mailbox."""
    email = (email or "").strip().lower()
    if not matching.EMAIL_RE.match(email):
        return "not a valid email address"
    domain = email.rsplit("@", 1)[1]
    if domain in TYPO_DOMAINS:
        return f"looks like a typo: did you mean @{TYPO_DOMAINS[domain]}?"
    if domain in DISPOSABLE_DOMAINS:
        return "throw-away email domain"
    if not domain_accepts_mail(domain):
        return f"the domain {domain} cannot receive email"
    return ""


# ------------------------------------------- history and do-not-contact ----

def _hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]


class Mailed(set):
    """Set of addresses already contacted. Also remembers (as hashes) addresses whose
    job was deleted, so nobody is mailed twice by accident."""

    def __init__(self, addresses=(), hashes=()):
        super().__init__(addresses)
        self.hashes = set(hashes)

    def __contains__(self, email) -> bool:
        return super().__contains__(email) or (bool(email) and _hash(email) in self.hashes)


def mailed_for(uid: str, jobs: list[dict]) -> Mailed:
    return Mailed(matching.mailed_addresses(jobs), db.get_private(uid, "contacted").get("hashes", []))


def remember_contact(uid: str, email: str) -> None:
    data = db.get_private(uid, "contacted")
    hashes = data.get("hashes", [])
    h = _hash(email)
    if h not in hashes:
        db.set_private(uid, "contacted", {"hashes": (hashes + [h])[-config.SEEN_LIMIT:]})


def clean_blocklist(rows: list[str]) -> list[str]:
    """Accepts full addresses or whole domains ('company.com' / '@company.com')."""
    out = []
    for raw in rows or []:
        v = str(raw).strip().lower().lstrip("@")
        if v and re.fullmatch(r"(?:[a-z0-9._%+-]+@)?[a-z0-9-]+(?:\.[a-z0-9-]+)+", v) and v not in out:
            out.append(v)
    return out[:500]


def is_blocked(email: str, blocklist: list[str]) -> bool:
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1]
    for item in blocklist or []:
        if "@" in item:
            if item == email:
                return True
        elif domain == item or domain.endswith("." + item):
            return True
    return False


# ---------------------------------------------------------- daily limit ----

def _today(user: dict) -> str:
    try:
        tz = ZoneInfo(user["settings"]["timezone"])
    except Exception:  # noqa: BLE001
        tz = ZoneInfo(config.DEFAULT_TZ)
    return datetime.now(tz).date().isoformat()


def usage(uid: str, user: dict) -> dict:
    """{day, sent_today, active_days, limit, left}. active_days = earlier days with mail."""
    data = db.get_private(uid, "usage")
    today = _today(user)
    if "active_days" not in data:          # first time after upgrading: learn from past sends
        past = {j.get("sent_date") for j in db.list_jobs(uid) if j.get("sent_date")}
        past.discard(today)
        data = {"day": today, "sent": 0, "active_days": len(past)}
        db.set_private(uid, "usage", data)
    sent, active = int(data.get("sent", 0)), int(data.get("active_days", 0))
    if data.get("day") != today:
        active += 1 if sent else 0
        sent = 0
    s = user["settings"]
    limit = int(s["daily_cap"])
    if s.get("warmup", True):
        limit = min(limit, config.WARMUP_START + config.WARMUP_STEP * active)
    return {"day": today, "sent_today": sent, "active_days": active, "limit": limit,
            "left": max(0, limit - sent)}


def record_send(uid: str, user: dict) -> None:
    u = usage(uid, user)
    db.set_private(uid, "usage", {"day": u["day"], "sent": u["sent_today"] + 1,
                                  "active_days": u["active_days"]})


def check_can_send(uid: str, user: dict, email: str) -> None:
    """Raises SendBlocked. Called for EVERY outgoing mail, including follow-ups."""
    if is_blocked(email, user["settings"].get("blocklist")):
        raise SendBlocked("this address is on your do-not-contact list", permanent=True)
    u = usage(uid, user)
    if u["left"] <= 0:
        raise SendBlocked(f"daily limit reached ({u['sent_today']}/{u['limit']} today)")
    problem = address_problem(email)
    if problem:
        raise SendBlocked(f"address check failed: {problem}", permanent=True)
