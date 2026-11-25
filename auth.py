from fastapi import Header, HTTPException

import config
import db


def current_user(authorization: str = Header(default="")) -> dict:
    """FastAPI dependency: verifies the Firebase ID token sent by the browser."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not signed in")
    try:
        claims = db.verify_token(authorization.split(" ", 1)[1])
    except Exception:
        raise HTTPException(401, "Session expired — please sign in again")
    if claims.get("firebase", {}).get("sign_in_provider") != "google.com":
        raise HTTPException(403, "Only Google sign-in is allowed")
    email = (claims.get("email") or "").lower()
    if config.ALLOWED_EMAILS and email not in config.ALLOWED_EMAILS:
        raise HTTPException(403, "This account is not on the allow-list")
    claims["uid"] = claims["uid"]
    return claims
