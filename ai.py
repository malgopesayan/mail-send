"""LLM prompts + extraction (Groq), LinkedIn search (HarvestAPI), resume text."""
import base64
import json
import re
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from groq import APIStatusError, Groq, RateLimitError
from PIL import Image, ImageOps
from pypdf import PdfReader

import config

MAX_IMAGE_DIMENSION = 1568
RETRY_BACKOFF_SECONDS = 3


# ----------------------------------------------------------------- utils ----

def parse_json(text: str) -> dict:
    """Tolerant JSON parse: strips <think> blocks, code fences and prose around the object."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def is_quota_error(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429
    return "429" in str(exc) or "rate_limit" in str(exc).lower()


def safe_score(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------- resume text ----

def extract_resume_text(filename: str, content: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()
    try:
        if suffix == ".pdf":
            return "\n".join((p.extract_text() or "") for p in PdfReader(BytesIO(content)).pages)
        if suffix == ".docx":
            import docx
            return "\n".join(p.text for p in docx.Document(BytesIO(content)).paragraphs)
    except Exception as exc:  # noqa: BLE001
        print(f"[ai] resume text extraction failed: {exc}")
    return ""


def latex_to_text(tex: str) -> str:
    tex = re.sub(r"(?<!\\)%.*", "", tex)
    m = re.search(r"\\begin\{document\}(.*)\\end\{document\}", tex, flags=re.S)
    if m:
        tex = m.group(1)
    tex = re.sub(r"\\(?:section|subsection)\*?\{([^{}]*)\}", r"\n\n## \1\n", tex)
    tex = re.sub(r"\\(?:resumeItem|item)\b\s*", "\n- ", tex)
    tex = re.sub(r"\\(?:\\|newline)\b\s*(?:\[[^\]]*\])?", "\n", tex)
    tex = re.sub(r"\\href\{([^{}]*)\}\{([^{}]*)\}", r"\2 (\1)", tex)
    tex = re.sub(r"\}\s*\{", " | ", tex)
    cmd = re.compile(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}")
    for _ in range(6):
        new = cmd.sub(r"\1", tex)
        if new == tex:
            break
        tex = new
    tex = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", tex)
    tex = (tex.replace("\\&", "&").replace("\\%", "%").replace("\\$", "$")
              .replace("\\_", "_").replace("\\#", "#").replace("~", " "))
    tex = re.sub(r"[{}]", "", tex)
    tex = re.sub(r"[ \t]+", " ", tex)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", tex).strip()


PROFILE_BANK_MAX_CHARS = 5000

PROFILE_CONDENSE_PROMPT = """
Below is a candidate's resume. Turn it into an "EXPERIENCE BANK" that another
model will use to write tailored job application emails.

Rules:
- Use ONLY facts present in the resume. Never invent employers, numbers, tools or dates.
- Keep every metric exactly as written. Under 650 words, plain text.
- For every project / job bullet worth mentioning write one line:
  WHAT was built -> TECH used -> RESULT (if stated).
- Tag each line with 2-4 lowercase keywords in [brackets], e.g. [rag, llm, fastapi].

Layout:
SUMMARY: (2 sentences)
SKILLS: (comma separated, grouped)
EXPERIENCE:
- <Company> | <Role> | <dates>
  * [tags] what -> tech -> result
PROJECTS:
- <Project name> [tags]: what -> tech -> result
EDUCATION: (one line)

Resume:
\"\"\"{resume_text}\"\"\"
"""


def condense_profile(keys: list[str], model: str, resume_text: str) -> str:
    fallback = resume_text[:PROFILE_BANK_MAX_CHARS]
    if not keys or not resume_text.strip():
        return fallback
    prompt = PROFILE_CONDENSE_PROMPT.replace("{resume_text}", resume_text[:9000])
    for key in keys:
        try:
            resp = Groq(api_key=key).chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                temperature=0.2, max_completion_tokens=1400)
            out = re.sub(r"<think>.*?</think>", "", resp.choices[0].message.content or "", flags=re.S).strip()
            if len(out) > 200:
                return out[:PROFILE_BANK_MAX_CHARS]
        except Exception as exc:  # noqa: BLE001
            print(f"[ai] condense failed with one key: {exc}")
    return fallback


KEYWORD_PROMPT = """
Read the profile below and identify the 3-6 job titles/roles this candidate is a
strong fit for. For each role produce 2-3 FULL search strings someone would type
into LinkedIn post search to find recruiters actively hiring RIGHT NOW. Combine
the role with a hiring-intent phrase and vary the pattern, e.g.:
  "AI Engineer Hiring", "Hiring AI Engineer", "We're hiring a Python Developer",
  "Backend Engineer job opening", "Looking for a FastAPI Developer".
Also add 2-4 skill-based strings paired with "hiring"/"job" (never a bare skill word).
Rules: 10-16 strings total, 2-6 words each, no duplicates.
Return ONLY JSON: {{"keywords": ["...", "..."]}}

Profile:
\"\"\"{text}\"\"\"
"""


def extract_keywords(keys: list[str], model: str, text: str) -> list[str]:
    if not keys or not text.strip():
        return []
    try:
        resp = Groq(api_key=keys[0]).chat.completions.create(
            model=model, temperature=0.2, max_completion_tokens=600,
            messages=[{"role": "user", "content": KEYWORD_PROMPT.format(text=text[:6000])}],
            response_format={"type": "json_object"})
        data = parse_json(resp.choices[0].message.content)
        return [k.strip() for k in data.get("keywords", []) if isinstance(k, str) and k.strip()]
    except Exception as exc:  # noqa: BLE001
        print(f"[ai] keyword extraction failed: {exc}")
        return []


# --------------------------------------------------------------- prompts ----

EMAIL_RULES = """\"email_subject\": \"short, specific subject, e.g. 'Application for <Job Title> - {name}'\",
  \"email_body\": \"plain-text application email, roughly 150-200 words. Structure: (1) greeting
      (recruiter's name if known); (2) one sentence naming the exact role/company; (3) a short
      PERSONAL paragraph telling ONE concrete story from the candidate's real experience below -
      pick the single project/job bullet that best matches THIS posting and say what was built,
      the tech and the result, in first person; (4) one line connecting 1-2 other relevant
      skills; (5) polite call to action mentioning the attached resume; (6) sign-off with the
      name and contact details below. Use ONLY facts in the candidate profile - never invent
      employers, tools or metrics. Sound like a person, not a template: no 'I am writing to
      express my interest', no buzzword lists. No markdown.\""""


def _contact_block(c: dict) -> str:
    rows = [("Name", c.get("name")), ("Email", c.get("email")), ("Phone", c.get("phone")),
            ("LinkedIn", c.get("linkedin")), ("GitHub", c.get("github"))]
    return "\n".join(f"{k}: {v}" for k, v in rows if v)


def build_image_prompt(profile: str, cand: dict) -> str:
    return f"""
You are looking at a screenshot of a recruiter message, job posting, or LinkedIn DM.
1. Extract job/recruiter details. 2. Draft a short personalised application email for the candidate below.

Return ONLY a JSON object (no markdown fences, no commentary):
{{
  "recruiter_email": "email if visible, else empty string",
  "recruiter_name": "name if visible, else empty string",
  "company": "company if identifiable, else empty string",
  "job_title": "role if identifiable, else empty string",
  "location": "location if mentioned, else empty string",
  "job_match_score": integer 0-100 (fit with the candidate profile),
  "priority": "High" | "Medium" | "Low",
  {EMAIL_RULES.format(name=cand.get('name') or 'Candidate')}
}}

Candidate profile:
\"\"\"{profile}\"\"\"

Candidate contact details to sign with:
{_contact_block(cand)}

If a field cannot be determined use "" (0 for the score). Still draft the email even if
some details are missing - keep it a bit more generic then.
"""


def build_post_prompt(profile: str, cand: dict, post_text: str) -> str:
    return f"""
You are looking at the text of a LinkedIn post, which may or may not be a genuine job posting.
1. Decide if it is a job posting / recruiter outreach (not general hiring chatter or news).
2. Decide if it is a DIRECT listing - by the hiring company, an employee, or the recruiter
   working the role - as opposed to an aggregator/bot account (e.g. "RemoteYeah", "JobKash",
   "XYZ Jobs" pages reposting scraped listings).
3. If genuine and direct, extract details and draft a personalised email for the candidate.

Return ONLY a JSON object:
{{
  "is_job_post": true or false,
  "is_direct_listing": true or false,
  "recruiter_email": "ONLY an email visible verbatim in the post - never guess. Else empty string.",
  "recruiter_name": "post author's name, else empty string",
  "company": "...", "job_title": "...", "location": "...",
  "job_match_score": integer 0-100,
  "priority": "High" | "Medium" | "Low",
  {EMAIL_RULES.format(name=cand.get('name') or 'Candidate')}
}}

Candidate profile:
\"\"\"{profile}\"\"\"

Candidate contact details to sign with:
{_contact_block(cand)}

If this is not a genuine direct job post set is_job_post / is_direct_listing to false and leave
the rest empty. Never invent contact details.

LinkedIn post text:
\"\"\"{post_text}\"\"\"
"""


# ----------------------------------------------------- screenshot -> JSON ----

def preprocess_image(raw: bytes) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(BytesIO(raw)))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    gray = img.convert("L")
    diff = ImageOps.invert(gray) if gray.getpixel((0, 0)) > 200 else gray
    bbox = diff.getbbox()
    if bbox:
        pad = 8
        img = img.crop((max(bbox[0] - pad, 0), max(bbox[1] - pad, 0),
                        min(bbox[2] + pad, img.width), min(bbox[3] + pad, img.height)))
    longest = max(img.size)
    if longest > MAX_IMAGE_DIMENSION:
        s = MAX_IMAGE_DIMENSION / longest
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    return img


def image_data_url(raw: bytes) -> str:
    buf = BytesIO()
    preprocess_image(raw).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


EMPTY = {"recruiter_email": "", "recruiter_name": "", "company": "", "job_title": "", "location": "",
         "job_match_score": 0, "priority": "", "email_subject": "", "email_body": ""}


def mostly_empty(f: dict) -> bool:
    return all(not str(f.get(k, "")).strip() for k in ("company", "job_title", "recruiter_name", "recruiter_email"))


def extract_from_image(client: Groq, model: str, raw: bytes, profile: str, cand: dict) -> dict:
    resp = client.chat.completions.create(
        model=model, temperature=0.3, max_completion_tokens=2048,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": build_image_prompt(profile, cand)},
            {"type": "image_url", "image_url": {"url": image_data_url(raw)}}]}])
    return parse_json(resp.choices[0].message.content) or dict(EMPTY)


def extract_with_retries(clients: list[Groq], model: str, raw: bytes, log, profile: str, cand: dict):
    """Returns (fields | None, flagged_low_confidence)."""
    attempts = max(len(clients), 2)
    fallback = None
    for i in range(attempts):
        try:
            got = extract_from_image(clients[i % len(clients)], model, raw, profile, cand)
        except Exception as exc:  # noqa: BLE001
            log(f"  attempt {i + 1}/{attempts}: " + ("quota hit, trying another key…" if is_quota_error(exc)
                else f"{exc.__class__.__name__}: {exc}"), "warn")
            time.sleep(RETRY_BACKOFF_SECONDS)
            continue
        if mostly_empty(got):
            fallback = got
            log(f"  attempt {i + 1}/{attempts}: extraction came back empty, retrying…", "warn")
            time.sleep(RETRY_BACKOFF_SECONDS)
            continue
        return got, False
    if fallback is not None:
        fallback["priority"] = "REVIEW - low OCR confidence"
        return fallback, True
    return None, False


def extract_from_post(client: Groq, model: str, post_text: str, profile: str, cand: dict) -> dict:
    resp = client.chat.completions.create(
        model=model, temperature=0.3, max_completion_tokens=1024,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": build_post_prompt(profile, cand, post_text[:4000])}])
    return parse_json(resp.choices[0].message.content) or {"is_job_post": False}


# --------------------------------------------------------------- HarvestAPI ----

def harvest_search(api_key: str, query: str, posted_limit: str = "week") -> list[dict]:
    r = requests.get(f"{config.HARVESTAPI_BASE}/linkedin/post-search", timeout=30,
                     params={"search": query, "postedLimit": posted_limit, "sortBy": "date", "page": 1},
                     headers={"X-API-Key": api_key})
    r.raise_for_status()
    return r.json().get("elements", [])


def post_datetime(post: dict) -> datetime | None:
    ts = (post.get("postedAt") or {}).get("timestamp")
    if not ts:
        return None
    if ts > 10_000_000_000:
        ts /= 1000
    try:
        return datetime.fromtimestamp(ts)
    except (ValueError, OSError):
        return None


def is_recent(post: dict, max_age_days: int) -> bool:
    dt = post_datetime(post)
    return dt is not None and (datetime.now() - dt).days <= max_age_days
