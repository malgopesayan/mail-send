import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ENCRYPTION_KEY", "test-passphrase")
os.environ.setdefault("CRON_SECRET", "cron-test")

import pytest
from fastapi.testclient import TestClient

import auth
import db
import main
import pipeline
import safety
import storage
from fakes import FakeFirestore, FakeSender, FakeStorage

UID = "user1"


@pytest.fixture
def env(monkeypatch):
    fs, st = FakeFirestore(), FakeStorage()
    monkeypatch.setattr(db, "fs", lambda: fs)
    for name in ("upload", "download", "list_files", "remove", "signed_url", "ping"):
        monkeypatch.setattr(storage, name, getattr(st, name))
    FakeSender.outbox = []
    monkeypatch.setattr(pipeline, "GmailSender", FakeSender)
    monkeypatch.setattr(safety, "domain_accepts_mail", lambda d: d != "nomail.example")
    pipeline._STATES.clear()
    db.ensure_user(UID, {"email": "me@example.com", "name": "Test User"})
    db.update_user(UID, {"gmail": {"connected": True, "email": "me@example.com"},
                         "keywords": ["Python Developer Hiring"],
                         "profile_text": "- API [python, fastapi]: built a service in Python"})
    main.app.dependency_overrides[auth.current_user] = lambda: {"uid": UID, "email": "me@example.com"}
    yield type("Env", (), {"client": TestClient(main.app), "fs": fs, "storage": st, "uid": UID,
                           "outbox": property(lambda s: FakeSender.outbox)})()
    main.app.dependency_overrides.clear()


def add_job(**extra):
    row = pipeline.new_row({"recruiter_email": "hr@acme.com", "recruiter_name": "Asha Rao", "company": "Acme",
                            "job_title": "Python Developer", "location": "Pune", "job_match_score": 80,
                            "email_subject": "Application for Python Developer",
                            "email_body": "Hello, please find my resume attached."})
    row.update(extra)
    return db.add_job(UID, row)
