"""Alerts: Telegram bot and/or an email to yourself. Alerts never break a run."""
import re

import requests

import db

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")
CHAT_RE = re.compile(r"^(-?\d{3,20}|@[A-Za-z0-9_]{5,32})$")


def send_telegram(token: str, chat_id: str, text: str) -> None:
    if not TOKEN_RE.match(token or "") or not CHAT_RE.match(chat_id or ""):
        raise ValueError("Telegram bot token or chat id looks wrong")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15,
                          json={"chat_id": chat_id, "text": text[:3500],
                                "link_preview_options": {"is_disabled": True}})
    except requests.RequestException as exc:       # the URL holds the token: never echo it
        raise RuntimeError(f"could not reach Telegram ({exc.__class__.__name__})") from None
    if not r.ok:
        try:
            why = r.json().get("description", "")
        except ValueError:
            why = ""
        raise RuntimeError(f"Telegram refused: {why or r.status_code}")


def notify(uid: str, text: str) -> list[str]:
    """Sends to every channel the user switched on. Returns problems (never raises)."""
    problems = []
    try:
        user, sec = db.get_user(uid), db.get_secrets(uid)
        chat_id = user["settings"].get("telegram_chat_id", "")
        token = sec.get("telegram_token", "")
        if token and chat_id:
            try:
                send_telegram(token, chat_id, text)
            except Exception as exc:  # noqa: BLE001
                problems.append(str(exc)[:160])
        if user["settings"].get("email_summary"):
            try:
                import pipeline
                me = (user.get("gmail") or {}).get("email")
                if me:
                    pipeline.make_sender(sec, user).send(me, "Job Mailer update", text)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"email summary: {str(exc)[:160]}")
    except Exception as exc:  # noqa: BLE001
        problems.append(str(exc)[:160])
    return problems
