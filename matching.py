"""Deterministic guard rails. These run in plain Python, so nothing written inside a
LinkedIn post or screenshot can talk its way past them.

  * relevance  - is this post really a hiring post for one of MY roles?
  * draft safety - did the AI write something it should not have (prompt injection)?
  * hold_reason  - may this job be mailed automatically?
"""
import re

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
ANY_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>()\"']+|\b[a-z0-9-]+\.(?:com|net|org|io|co|in|me|ly|xyz|app|ai|dev|info|link|top)/[^\s<>()\"']*",
    re.I)

HIRING_RE = re.compile(
    r"\bhiring\b|\bwe(?:'|’)?re looking\b|\bwe are looking\b|\blooking (?:for|to hire)\b|\bjob (?:opening|opportunity|alert)\b|"
    r"\bopenings?\b|\bvacanc(?:y|ies)\b|\bopen (?:position|role)s?\b|\bapply\b|\b(?:send|share|email|mail|drop) (?:me |us )?(?:your |an? |the )?(?:updated )?(?:cv|resume|profile)\b|"
    r"\bjoin (?:our|my|the) team\b|\brecruit(?:ing|ment)\b|\brequirement\b|\bwalk[- ]?in\b|\bimmediate joiners?\b|#hiring", re.I)
SEEKER_RE = re.compile(
    r"#opentowork|\bi(?:'|’)?(?: a)?m (?:currently |actively )?(?:open to work|(?:looking|seeking)(?: for)? (?:a |an |my )?(?:new |next )?(?:job|role|opportunit|position|internship))|"
    r"\bi am (?:currently |actively )?(?:looking|seeking)(?: for)? (?:a |an |my )?(?:new |next )?(?:job|role|opportunit|position|internship)", re.I)
JUNK_RE = re.compile(
    r"\bregistration fee\b|\bpay (?:a |the )?(?:registration|processing|training) (?:fee|amount|charge)|\bsecurity deposit\b|"
    r"\btraining (?:cum|and|\+|&) placement\b|\benrol?l now\b|\bbootcamp\b|\bwebinar\b|\bcourse fee\b|\bpaid (?:internship program|training)\b|"
    r"\bearn .{0,25}per day\b|\bcomment .{0,6}interested.{0,6}(?:below|to get)", re.I)
INJECTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\b.{0,40}\b(?:previous|prior|above|earlier|all|your)\b.{0,20}\b(?:instruction|prompt|rule|message)|"
    r"\bsystem prompt\b|\byou are now\b|\bnew instructions?\s*:|"
    r"\bif you(?:'| a)?re an? (?:ai|llm|language model|bot|assistant)\b|\b(?:ai|llm) (?:assistant|agent|model)s?\s*[:,]|"
    r"<\s*/?\s*(?:system|assistant|tool|instructions?)\s*>|\[/?INST\]|<\|", re.I)
SENSITIVE_RE = re.compile(
    r"\bpassword\b|\bone[- ]time (?:password|code)\b|\botp\b|\bbank account\b|\baadhaar\b|\bsocial security\b|"
    r"\bapi[_ -]?key\b|\brefresh token\b|\bcredit card (?:number|details)\b|\bwire transfer\b|\bgift cards?\b|\bupi (?:id|pin)\b", re.I)

# ------------------------------------------------------------- role terms ----

_NORMALISE = [
    (r"\bback[\s-]end\b", "backend"), (r"\bfront[\s-]end\b", "frontend"), (r"\bfull[\s-]stack\b", "fullstack"),
    (r"\bartificial intelligence\b", "ai"), (r"\bmachine learning\b", "ml"), (r"\bgen(?:erative)?[\s-]?ai\b", "genai"),
    (r"\bdev[\s-]?ops\b", "devops"), (r"\b(?:sde|swe)(?:[\s-]?(?:[123]|i{1,3}))?\b", "software engineer"),
    (r"\breact\.?js\b", "react"), (r"\bnode\.?js\b", "node"), (r"\b(engineer|developer|analyst|scientist|designer|manager)s\b", r"\1"),
]
_FILLER = re.compile(
    r"\b(?:we(?:'|’)?re|we are|is|are|now|hiring|hire|jobs?|openings?|vacanc(?:y|ies)|opportunit(?:y|ies)|looking|for|"
    r"an?|the|urgent(?:ly)?|immediate|needed|wanted|role|position)\b")
_FAMILIES = [{"engineer", "developer", "programmer", "scientist", "architect"}, {"analyst"}, {"designer"},
             {"manager"}, {"tester"}, {"consultant"}, {"recruiter"}, {"writer"}, {"accountant"}]
_GENERIC = set().union(*_FAMILIES) | {"lead", "senior", "junior", "associate", "intern", "specialist",
                                      "executive", "officer", "sr", "jr", "staff", "principal", "fresher"}
_SYNONYMS = [{"ai", "ml", "genai", "llm"}, {"devops", "sre"}, {"qa", "sdet", "test", "testing"},
             {"frontend", "react", "angular", "vue"}]


def _tokens(text: str) -> list[str]:
    t = (text or "").lower()
    for pat, rep in _NORMALISE:
        t = re.sub(pat, rep, t)
    return re.findall(r"[a-z0-9+#]+", t)


def role_terms(keywords: list[str]) -> list[tuple[set, set | None]]:
    """'Hiring AI Engineer' -> ({'ai','ml','genai','llm'}, {engineer family})."""
    out = []
    for kw in keywords or []:
        t = (kw or "").lower()
        for pat, rep in _NORMALISE:
            t = re.sub(pat, rep, t)
        toks = re.findall(r"[a-z0-9+#]+", _FILLER.sub(" ", t))
        specific = {x for x in toks if x not in _GENERIC}
        for group in _SYNONYMS:
            if specific & group:
                specific |= group
        family = next((f for f in _FAMILIES if f & set(toks)), None)
        if specific or family:
            out.append((specific, family))
    return out


def title_matches(title: str, keywords: list[str]) -> bool:
    have = set(_tokens(title))
    terms = role_terms(keywords)
    if not terms:
        return True
    return any((not spec or spec & have) and (not fam or fam & have) for spec, fam in terms)


def _mentions_any_term(text: str, keywords: list[str]) -> bool:
    have = set(_tokens(text))
    terms = role_terms(keywords)
    return not terms or any((spec or fam or set()) & have for spec, fam in terms)


def excluded_word(words: list[str], *texts: str) -> str:
    hay = " ".join(t or "" for t in texts).lower()
    for w in words or []:
        w = w.strip().lower()
        if w and re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", hay):
            return w
    return ""


def prefilter_post(content: str, keywords: list[str], exclude: list[str], strict: bool) -> str:
    """Cheap check BEFORE spending an AI call. Returns a reason to skip, or ''."""
    if excluded_word(exclude, content):
        return "blocked word"
    if not strict:
        return ""
    if SEEKER_RE.search(content):
        return "job seeker, not a job"
    if JUNK_RE.search(content):
        return "course / scam / spam"
    if not HIRING_RE.search(content):
        return "not a hiring post"
    if not _mentions_any_term(content, keywords):
        return "off-target role"
    return ""


# ------------------------------------------------------------ draft safety ----

def _deobfuscate(text: str) -> str:
    t = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text or "", flags=re.I)
    return re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", t, flags=re.I)


def email_in_text(email: str, text: str) -> bool:
    """The recipient must literally be written in the source - the AI may not invent or swap it."""
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return False
    return email in {m.lower().rstrip(".") for m in ANY_EMAIL_RE.findall(_deobfuscate(text))}


def one_line(text: str, limit: int = 200) -> str:
    return re.sub(r"[\x00-\x1f\x7f  ‪-‮⁦-⁩]+", " ", str(text or "")).strip()[:limit]


def _own_link(url: str, cand: dict) -> bool:
    def norm(u):
        return re.sub(r"^(?:https?://)?(?:www\.)?", "", (u or "").strip().lower()).rstrip("/.,;")
    mine = [norm(cand.get(k)) for k in ("linkedin", "github")]
    u = norm(url)
    return any(m and (u == m or u.startswith(m + "/")) for m in mine)


def draft_problems(row: dict, cand: dict, source_text: str = "", profile: str = "") -> list[str]:
    """Reasons a human must look at this draft before it can ever be sent."""
    body = f"{row.get('email_subject', '')}\n{row.get('email_body', '')}"
    out = []
    if source_text and INJECTION_RE.search(source_text):
        out.append("source text contains instructions aimed at an AI")
    if INJECTION_RE.search(body):
        out.append("draft contains instruction-like text")
    known = _deobfuscate(profile).lower()
    bad_links = [u for u in URL_RE.findall(body)
                 if not _own_link(u, cand) and u.lower().rstrip("/.,;") not in known]
    if bad_links:
        out.append("draft contains a link that is not yours")
    allowed = {(cand.get("email") or "").strip().lower(), (row.get("recruiter_email") or "").strip().lower()}
    if any(m.lower().rstrip(".") not in allowed and m.lower().rstrip(".") not in known
           for m in ANY_EMAIL_RE.findall(body)):
        out.append("draft mentions an unknown email address")
    if SENSITIVE_RE.search(body):
        out.append("draft mentions sensitive data (password / OTP / bank / key)")
    if len(row.get("email_body") or "") > 4000:
        out.append("draft is unusually long")
    return out


def score_of(v) -> int:
    try:
        return max(0, min(100, int(float(v))))
    except (TypeError, ValueError, OverflowError):
        return 0


def hold_reason(job: dict, min_score: int, mailed: set[str]) -> str:
    """'' = may be mailed automatically. `mailed` = lower-case addresses already contacted."""
    if job.get("reviewed"):
        return ""
    if job.get("needs_review"):
        return job["needs_review"]
    if not (job.get("email_body") or "").strip():
        return "no email draft"
    if str(job.get("priority", "")).upper().startswith("REVIEW"):
        return "low-confidence screenshot reading"
    if score_of(job.get("job_match_score")) < min_score:
        return f"match score below {min_score}"
    if (job.get("recruiter_email") or "").strip().lower() in mailed:
        return "you already mailed this address"
    return ""


def mailed_addresses(jobs: list[dict]) -> set[str]:
    return {(j.get("recruiter_email") or "").strip().lower() for j in jobs if j.get("email_status") == "Sent"} - {""}
