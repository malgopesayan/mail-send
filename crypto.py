"""Encryption for per-user secrets + signed OAuth `state` tokens."""
import base64
import hashlib
import hmac
import json
import time

from cryptography.fernet import Fernet, InvalidToken

import config


def _key_bytes() -> bytes:
    if not config.ENCRYPTION_KEY:
        raise RuntimeError("ENCRYPTION_KEY is not set")
    # Accept either a real Fernet key or any passphrase.
    try:
        raw = base64.urlsafe_b64decode(config.ENCRYPTION_KEY.encode())
        if len(raw) == 32:
            return config.ENCRYPTION_KEY.encode()
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(config.ENCRYPTION_KEY.encode()).digest())


def _fernet() -> Fernet:
    return Fernet(_key_bytes())


def encrypt_json(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_json(blob: str) -> dict:
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob.encode()).decode())
    except (InvalidToken, ValueError):
        return {}


def sign_state(uid: str, ttl: int = 900) -> str:
    payload = f"{uid}|{int(time.time()) + ttl}"
    sig = hmac.new(_key_bytes(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def verify_state(state: str) -> str | None:
    try:
        uid, exp, sig = base64.urlsafe_b64decode(state.encode()).decode().rsplit("|", 2)
        good = hmac.new(_key_bytes(), f"{uid}|{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) >= time.time():
            return uid
    except Exception:
        pass
    return None


def mask(value: str) -> str:
    value = value or ""
    return f"{value[:4]}…{value[-4:]}" if len(value) > 10 else ("•" * len(value))
