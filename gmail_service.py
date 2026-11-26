"""Per-user Gmail. Each user authorises THEIR OWN Google account (any account —
it does not have to be the one they log in with) and mail is sent from it.

Plain OAuth2 web flow, no google-auth-oauthlib (avoids its PKCE state problems
when the code exchange happens in a different request than the auth URL)."""
import base64
import json
import mimetypes
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import urlencode

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config

SCOPES = ["https://www.googleapis.com/auth/gmail.send", "openid",
          "https://www.googleapis.com/auth/userinfo.email"]
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailAuthRequired(Exception):
    """No Gmail connected for this user, or Google revoked the token."""


def parse_client_json(text: str) -> dict:
    """Accepts a downloaded credentials.json ({"web":{...}} / {"installed":{...}})."""
    data = json.loads(text)
    inner = data.get("web") or data.get("installed") or data
    if not inner.get("client_id") or not inner.get("client_secret"):
        raise ValueError("That JSON has no client_id / client_secret")
    return {"client_id": inner["client_id"], "client_secret": inner["client_secret"],
            "type": "web" if "web" in data else ("installed" if "installed" in data else "web")}


def client_for(secrets: dict) -> dict | None:
    """The user's own OAuth client if they pasted one, else the shared server one."""
    if secrets.get("gmail_client"):
        return secrets["gmail_client"]
    if config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET:
        return {"client_id": config.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET, "type": "web"}
    return None


def auth_url(client: dict, state: str) -> str:
    return AUTH_URI + "?" + urlencode({
        "client_id": client["client_id"],
        "redirect_uri": config.GMAIL_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        # select_account => the user can pick ANY Google account to send from.
        # consent        => forces Google to return a refresh_token every time.
        "prompt": "select_account consent",
        "state": state,
    })


def exchange_code(client: dict, code: str) -> tuple[str, str]:
    """Returns (refresh_token, gmail_address)."""
    r = requests.post(TOKEN_URI, timeout=30, data={
        "code": code, "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": config.GMAIL_REDIRECT_URI, "grant_type": "authorization_code"})
    if not r.ok:
        raise RuntimeError(f"Google rejected the code: {r.text[:200]}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise RuntimeError("Google returned no refresh token — remove the app at "
                           "myaccount.google.com/permissions and connect again.")
    if "https://www.googleapis.com/auth/gmail.send" not in tok.get("scope", ""):
        raise RuntimeError("The 'send email' permission was not ticked on the consent screen.")
    info = requests.get("https://www.googleapis.com/oauth2/v3/userinfo", timeout=20,
                        headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
    return tok["refresh_token"], info.get("email", "")


class GmailSender:
    """Build once per run: refreshes the access token once, then sends many mails."""

    def __init__(self, secrets: dict, from_name: str = "", from_email: str = ""):
        client, refresh = client_for(secrets), secrets.get("gmail_refresh_token")
        if not client or not refresh:
            raise GmailAuthRequired("Gmail is not connected")
        creds = Credentials(token=None, refresh_token=refresh, token_uri=TOKEN_URI,
                            client_id=client["client_id"], client_secret=client["client_secret"],
                            scopes=SCOPES)
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GmailAuthRequired(f"Gmail authorisation expired or was revoked ({exc})")
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.from_name, self.from_email = from_name, from_email

    def send(self, to_email: str, subject: str, body: str,
             attachment: bytes | None = None, filename: str | None = None) -> str:
        msg = EmailMessage()
        msg["To"] = to_email
        msg["Subject"] = subject
        if self.from_email:
            msg["From"] = formataddr((self.from_name, self.from_email))
        msg.set_content(body)
        if attachment and filename:
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            main, sub = mime.split("/", 1)
            msg.add_attachment(attachment, maintype=main, subtype=sub, filename=filename)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return self.service.users().messages().send(userId="me", body={"raw": raw}).execute()["id"]
