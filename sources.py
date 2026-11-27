"""Job sources with public APIs: Greenhouse and Lever company boards, Adzuna search.
They give an apply link, almost never a recruiter email, so no mail is drafted."""
import hashlib
import html
import re

import requests

import ai
import config
import db
import matching

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
ADZUNA_COUNTRIES = ("in", "gb", "us", "au", "at", "be", "br", "ca", "ch", "de", "es", "fr", "it",
                    "mx", "nl", "nz", "pl", "sg", "za")
TIMEOUT = 25


def clean_slugs(rows: list[str]) -> list[str]:
    """Accepts a company token or its careers URL. Anything else is dropped, so a user
    can never make the server call an address of their choosing."""
    out = []
    for raw in rows or []:
        v = str(raw).strip().split("?")[0].rstrip("/")
        m = re.search(r"(?:greenhouse\.io|lever\.co)/(?:v1/boards/|v0/postings/)?([A-Za-z0-9_-]+)", v)
        v = m.group(1) if m else v
        if SLUG_RE.match(v) and v.lower() not in [x.lower() for x in out]:
            out.append(v)
    return out[:config.MAX_BOARDS]


def strip_html(text: str) -> str:
    t = html.unescape(str(text or ""))
    t = re.sub(r"<\s*(?:br|/p|/li|/h\d|/div)\s*/?>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t\xa0]+", " ", t)).strip()


def _get(url: str, **params):
    r = requests.get(url, params=params or None, timeout=TIMEOUT, headers={"Accept": "application/json"})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()


def greenhouse(token: str) -> list[dict]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", content="true")
    return [{"title": j.get("title"), "company": j.get("company_name") or token,
             "location": (j.get("location") or {}).get("name"), "url": j.get("absolute_url"),
             "text": strip_html(j.get("content")), "source": "greenhouse"}
            for j in data.get("jobs", [])]


def lever(company: str) -> list[dict]:
    data = _get(f"https://api.lever.co/v0/postings/{company}", mode="json")
    return [{"title": j.get("text"), "company": company,
             "location": (j.get("categories") or {}).get("location"),
             "url": j.get("hostedUrl") or j.get("applyUrl"),
             "text": j.get("descriptionPlain") or strip_html(j.get("description")), "source": "lever"}
            for j in data]


def adzuna(app_id: str, app_key: str, country: str, what: str, where: str = "") -> list[dict]:
    country = country if country in ADZUNA_COUNTRIES else "in"
    params = {"app_id": app_id, "app_key": app_key, "what": what, "results_per_page": 20, "max_days_old": 14}
    if where:
        params["where"] = where
    try:
        data = _get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", **params)
    except requests.RequestException as exc:        # the URL holds the key: never echo it
        raise RuntimeError(f"could not reach Adzuna ({exc.__class__.__name__})") from None
    return [{"title": strip_html(j.get("title")), "company": (j.get("company") or {}).get("display_name"),
             "location": (j.get("location") or {}).get("display_name"), "url": j.get("redirect_url"),
             "text": strip_html(j.get("description")), "source": "adzuna"}
            for j in data.get("results", [])]


def match_score(title: str, text: str, profile: str, keywords: list[str]) -> int:
    """Rough score without an AI call: title match plus how many of your skills the post names."""
    base = 45 if matching.title_matches(title, keywords) else 20
    skills = set()
    for group in re.findall(r"\[([^\]]{2,80})\]", profile or ""):
        skills.update(s.strip().lower() for s in group.split(",") if s.strip())
    if not skills:
        return base
    low = (text or "").lower()
    hits = sum(1 for s in skills if re.search(rf"(?<![a-z0-9]){re.escape(s)}(?![a-z0-9])", low))
    return min(95, base + min(50, hits * 10))


def _tasks(user: dict, secrets: dict):
    s = user["settings"]
    for token in clean_slugs(s.get("greenhouse_boards")):
        yield f"Greenhouse · {token}", lambda t=token: greenhouse(t)
    for company in clean_slugs(s.get("lever_companies")):
        yield f"Lever · {company}", lambda c=company: lever(c)
    app_id, app_key = secrets.get("adzuna_app_id"), secrets.get("adzuna_app_key")
    if app_id and app_key:
        where = next((l for l in s.get("preferred_locations") or [] if l.lower() != "remote"), "")
        for kw in (user.get("keywords") or [])[:6]:
            what = re.sub(r"\b(?:we are|hiring|jobs?|openings?)\b", " ", kw, flags=re.I).strip()
            if what:
                yield f"Adzuna · {what}", lambda w=what: adzuna(app_id, app_key, s.get("adzuna_country", "in"), w, where)


def collect(uid: str, user: dict, secrets: dict, log, stopped, new_row) -> int:
    """Fetch, filter, de-duplicate and save. Returns how many jobs were added."""
    keywords, s = user.get("keywords") or [], user["settings"]
    if not keywords:
        log("❌ No search keywords — build your profile or type them in Settings.", "fail")
        return 0
    tasks = list(_tasks(user, secrets))
    if not tasks:
        log("No job boards set up. Add companies or Adzuna keys in Settings → Job sources.", "warn")
        return 0
    saved = {j.get("apply_url") for j in db.list_jobs(uid) if j.get("apply_url")}
    locations = [l for l in s.get("preferred_locations") or [] if l]
    # remembered links, so a job you deleted does not come back on the next run
    seen = db.get_private(uid, "board_seen").get("ids", [])
    known = set(seen)
    added = 0
    for label, fetch in tasks:
        if stopped() or added >= config.MAX_BOARD_JOBS_PER_RUN:
            break
        try:
            rows = fetch()
        except Exception as exc:  # noqa: BLE001
            log(f"  {label}: failed ({str(exc)[:120]})", "warn")
            continue
        kept = 0
        for r in rows:
            if added >= config.MAX_BOARD_JOBS_PER_RUN:
                break
            title, url = matching.one_line(r.get("title"), 160), str(r.get("url") or "")
            location = matching.one_line(r.get("location"), 120)
            key = hashlib.sha1(url.encode()).hexdigest()[:20]
            if not title or not re.match(r"^https?://", url) or url in saved or key in known:
                continue
            if not matching.title_matches(title, keywords):
                continue
            if matching.excluded_word(s.get("exclude_words"), title):
                continue
            if locations and s.get("strict_location") and not ai.location_matches(
                    locations, location, include_remote=bool(s.get("include_remote", True))):
                continue
            text = (r.get("text") or "")[:6000]
            row = new_row({"company": r.get("company"), "job_title": title, "location": location,
                           "job_match_score": match_score(title, text, user.get("profile_text"), keywords)},
                          source=r["source"], apply_url=url[:600], post_text=text)
            db.add_job(uid, row)
            saved.add(url)
            known.add(key)
            seen.append(key)
            added += 1
            kept += 1
        log(f"  {label}: {len(rows)} job(s), {kept} new match(es)", "muted")
        if kept:
            db.set_private(uid, "board_seen", {"ids": seen[-config.SEEN_LIMIT:]})
    return added
