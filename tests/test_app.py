"""End to end through the real API with fake database, storage and Gmail."""
import io
import json
import zipfile
from datetime import date, timedelta

import docx

import config
import db
import maintenance
import notify
import pipeline
import resumes
import safety
import sources
from conftest import UID, add_job


def send(env, job_id):
    return env.client.post(f"/api/jobs/{job_id}/send")


def run_sync(steps, only=None):
    st = pipeline.state_for(UID)
    st.running = True
    pipeline._worker(UID, steps, "manual", st, only)
    return [l["msg"] for l in st.since(0)]


def make_due(job_id):
    db.update_job(UID, job_id, {"follow_up_date": date.today().isoformat()})


def docx_bytes(text):
    d = docx.Document()
    for line in text.splitlines():
        d.add_paragraph(line)
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def test_send_then_follow_up_in_same_thread(env):
    jid = add_job()
    r = send(env, jid)
    assert r.status_code == 200 and r.json()["stage"] == "sent"
    assert env.client.get("/api/followups").json() == []           # not due yet
    make_due(jid)
    due = env.client.get("/api/followups").json()
    assert len(due) == 1 and due[0]["subject"] == "Re: Application for Python Developer"
    assert due[0]["body"].startswith("Hi Asha,")
    r = env.client.post(f"/api/jobs/{jid}/followup", json={"subject": "changed", "body": due[0]["body"]})
    assert r.status_code == 200 and r.json()["stage"] == "followed_up"
    first, second = env.outbox
    assert second["thread_id"] == "t1" and second["reply_to_id"] == "<m1@example.com>"
    assert second["subject"] == "Re: Application for Python Developer"     # must match the thread
    again = env.client.post(f"/api/jobs/{jid}/followup", json={"subject": "x", "body": "second attempt here"})
    assert again.status_code == 409 and len(env.outbox) == 2


def test_reply_stops_follow_up(env):
    jid = add_job()
    send(env, jid)
    make_due(jid)
    r = env.client.post(f"/api/jobs/{jid}/stage", json={"stage": "replied"})
    assert r.json()["stage"] == "replied" and r.json()["replied_date"]
    assert env.client.get("/api/followups").json() == []
    r = env.client.post(f"/api/jobs/{jid}/followup", json={"subject": "x", "body": "please answer me"})
    assert r.status_code == 409 and len(env.outbox) == 1


def test_scheduled_send_never_sends_follow_ups(env):
    jid = add_job()
    send(env, jid)
    make_due(jid)
    run_sync(["send"])
    assert len(env.outbox) == 1


def test_no_repeat_mail_even_after_delete(env):
    jid = add_job()
    send(env, jid)
    env.client.delete(f"/api/jobs/{jid}")
    add_job()
    run_sync(["send"])
    assert len(env.outbox) == 1
    assert env.client.get("/api/jobs").json()[0]["hold"] == "you already mailed this address"


def test_blocklist_and_dead_domain_block_even_by_hand(env):
    env.client.put("/api/settings", json={"blocklist": ["acme.com"]})
    jid = add_job(reviewed=True)
    r = send(env, jid)
    assert r.status_code == 409 and "do-not-contact" in r.json()["detail"]
    dead = add_job(recruiter_email="hr@nomail.example")
    run_sync(["send"])
    assert env.outbox == []
    assert "cannot receive" in db.get_job(UID, dead)["needs_review"]


def test_daily_limit_and_warm_up(env):
    env.client.put("/api/settings", json={"daily_cap": 50, "warmup": True})
    for i in range(14):
        add_job(recruiter_email=f"hr{i}@acme.com", company=f"C{i}")
    pipeline_wait = pipeline.Ctx.wait
    pipeline.Ctx.wait = lambda self, s: False
    try:
        run_sync(["send"])
    finally:
        pipeline.Ctx.wait = pipeline_wait
    assert len(env.outbox) == config.WARMUP_START
    left = [j for j in db.list_jobs(UID) if j["email_status"] != "Sent"]
    r = send(env, left[0]["id"])
    assert r.status_code == 409 and "daily limit" in r.json()["detail"]
    # next sending day: yesterday counts, so the limit grows
    data = db.get_private(UID, "usage")
    db.set_private(UID, "usage", {**data, "day": (date.today() - timedelta(days=1)).isoformat()})
    u = safety.usage(UID, db.get_user(UID))
    assert u["sent_today"] == 0 and u["limit"] == config.WARMUP_START + config.WARMUP_STEP


def test_opt_out_line(env):
    env.client.put("/api/settings", json={"opt_out_line": True})
    send(env, add_job())
    assert env.outbox[0]["body"].endswith(safety.OPT_OUT_LINE)


def test_editing_content_removes_reviewed_flag(env):
    jid = add_job(needs_review="draft contains a link that is not yours")
    assert env.client.patch(f"/api/jobs/{jid}", json={"reviewed": True}).json()["hold"] == ""
    r = env.client.patch(f"/api/jobs/{jid}", json={"email_body": "different text"})
    assert r.json()["hold"] and r.json()["reviewed"] is False


def test_mark_sent_and_back(env):
    jid = add_job()
    r = env.client.patch(f"/api/jobs/{jid}", json={"email_status": "Sent"}).json()
    assert r["stage"] == "sent" and r["sent_date"]
    r = env.client.post(f"/api/jobs/{jid}/stage", json={"stage": "new"}).json()
    assert r["email_status"] == "Not Sent" and r["hold"] == "you already mailed this address"
    assert env.client.post(f"/api/jobs/{jid}/stage", json={"stage": "bogus"}).status_code == 400


def test_old_jobs_without_stage_still_work(env):
    db.fs().collection("users").document(UID).collection("jobs").document("old").set(
        {"company": "Old", "recruiter_email": "a@old.com", "email_status": "Sent",
         "sent_date": "2026-01-01", "follow_up_date": "2026-01-08", "created_at": "2026-01-01T00:00:00"})
    job = env.client.get("/api/jobs").json()[0]
    assert job["stage"] == "sent" and job["follow_up_due"]
    env.client.post("/api/jobs/old/followup", json={"subject": "Hello again", "body": "Following up on my mail."})
    assert env.outbox[0]["thread_id"] is None and env.outbox[0]["subject"] == "Hello again"


def test_analytics_and_export(env):
    a, b = add_job(), add_job(recruiter_email="x@beta.com", company="Beta")
    send(env, a)
    send(env, b)
    env.client.post(f"/api/jobs/{a}/stage", json={"stage": "interview"})
    d = env.client.get("/api/analytics").json()
    assert (d["sent"], d["replied"], d["reply_rate"], d["interviews"]) == (2, 1, 50, 1)
    assert d["weeks"][-1]["sent"] == 2 and d["roles"][0]["name"] == "Python Developer"
    assert b"Acme" in env.client.get("/api/export/jobs.csv").content
    assert env.client.get("/api/export/jobs.xlsx").content[:2] == b"PK"


def test_resumes_pick_and_attach(env):
    for label, text in (("Python", "python fastapi developer"), ("Data Analyst", "sql tableau analyst")):
        r = env.client.post("/api/resumes", data={"label": label},
                            files={"file": ("cv.docx", docx_bytes(text), "application/octet-stream")})
        assert r.status_code == 200, r.text
    extra = env.client.get("/api/resumes").json()["resumes"][1]["id"]
    send(env, add_job(resume_id=extra))
    assert env.outbox[0]["attachment"] == env.storage.files[
        (config.RESUME_BUCKET, db.get_user(UID)["resumes"][0]["path"])]
    assert env.client.delete(f"/api/resumes/{extra}").json()["resumes"][0]["id"] == "default"


def test_tailored_resume_needs_acceptance(env, monkeypatch):
    source = ("Test User\nSoftware Engineer at Acme Labs, 2023 - 2025\nBuilt a billing API in Python and FastAPI.\n"
              "Skills: Python, FastAPI, PostgreSQL\nB.Tech Computer Science, 2023")
    env.client.post("/api/resumes", data={"label": "Main"}, files={"file": ("cv.docx", docx_bytes(source), "x/y")})
    reply = {"headline": "Software Engineer", "summary": "Software Engineer who builds APIs.",
             "skills": ["Python", "Rust"], "experience": [
                 {"company": "Acme Labs", "role": "Software Engineer", "dates": "2023 - 2025",
                  "bullets": ["Built a billing API in Python"]}],
             "missing_keywords": ["Docker"], "suggestions": ["Add links"]}
    used = {}

    def fake(self, kind, messages, **kw):
        used["model"] = self.model
        return "<think>x</think>" + json.dumps(reply)

    monkeypatch.setattr(resumes.ResumeAI, "complete", fake)
    monkeypatch.setattr(resumes.ResumeAI, "ready", lambda self: True)
    jid = add_job(post_text="Python and Docker")
    d = env.client.post(f"/api/jobs/{jid}/tailor", json={}).json()
    assert used["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert d["preview"]["skills"] == ["Python"] and d["missing_keywords"] == ["Docker"]
    assert db.get_job(UID, jid)["tailored"] is None
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(
        env.client.get(f"/api/jobs/{jid}/tailor/download").content)).paragraphs)
    assert "Acme Labs" in text and "Rust" not in text and "Docker" not in text
    before = pipeline.fingerprint(db.get_job(UID, jid))
    env.client.post(f"/api/jobs/{jid}/tailor/use")
    assert pipeline.fingerprint(db.get_job(UID, jid)) != before     # an old approval no longer matches
    send(env, jid)
    assert env.outbox[0]["filename"].endswith("_Acme.docx")


def test_resume_maker_without_key_says_so(env, monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")
    r = env.client.post(f"/api/jobs/{add_job()}/tailor", json={})
    assert r.status_code == 400 and "NVIDIA" in r.json()["detail"]


def test_company_boards(env, monkeypatch):
    def fake_get(url, **params):
        if "greenhouse" in url:
            return {"jobs": [{"title": "Python Developer", "absolute_url": "https://x.test/1",
                              "location": {"name": "Pune"}, "content": "&lt;p&gt;Use FastAPI&lt;/p&gt;"},
                             {"title": "Sales Manager", "absolute_url": "https://x.test/2"},
                             {"title": "Python Developer", "absolute_url": "javascript:alert(1)"}]}
        return [{"text": "Senior Python Developer", "hostedUrl": "https://y.test/1",
                 "categories": {"location": "Remote"}, "descriptionPlain": "python"}]

    monkeypatch.setattr(sources, "_get", fake_get)
    env.client.put("/api/settings", json={"greenhouse_boards": ["acme"], "lever_companies": ["beta"]})
    run_sync(["boards"])
    jobs = env.client.get("/api/jobs").json()
    assert sorted(j["apply_url"] for j in jobs) == ["https://x.test/1", "https://y.test/1"]
    assert all(j["hold"] == "" and j["stage"] == "new" and "post_text" not in j for j in jobs)
    assert db.get_job(UID, jobs[0]["id"])["post_text"] in ("Use FastAPI", "python")
    env.client.delete(f"/api/jobs/{jobs[0]['id']}")
    run_sync(["boards"])
    assert len(env.client.get("/api/jobs").json()) == 1            # deleted one stays gone
    run_sync(["send"])
    assert env.outbox == []


def test_cron_tick_runs_boards_schedule_and_reminds(env, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "notify", lambda uid, text: sent.append(text) or [])
    monkeypatch.setattr(pipeline, "start_user_run", lambda *a, **k: sent.append(a[1]) or True)
    jid = add_job()
    send(env, jid)
    make_due(jid)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%H:%M")
    assert env.client.post("/api/schedules", json={"run_at": now, "mode": "boards"}).status_code == 200
    r = env.client.post("/api/cron/tick", headers={"X-Cron-Secret": "cron-test"})
    assert r.status_code == 200, r.text
    assert ["boards"] in sent and any("follow-up" in str(s) for s in sent)


def test_secrets_are_masked_and_bad_input_refused(env):
    token = "123456789:" + "A" * 35
    r = env.client.put("/api/secrets", json={"telegram_token": token, "adzuna_app_id": "id", "adzuna_app_key": "k" * 20})
    assert token not in r.text and "k" * 20 not in r.text
    assert env.client.put("/api/secrets", json={"telegram_token": "nope"}).status_code == 400
    assert env.client.put("/api/settings", json={"telegram_chat_id": "abc def"}).status_code == 400
    assert env.client.put("/api/settings", json={"daily_cap": 5000}).status_code == 422


def test_auto_delete_keeps_interviews(env):
    old = "2020-01-01T00:00:00+00:00"
    gone, kept = add_job(), add_job(recruiter_email="b@b.com", stage="interview", email_status="Sent")
    for jid in (gone, kept):
        db.update_job(UID, jid, {"created_at": old})
    assert maintenance.purge_old_jobs(UID, 30) == 1
    assert db.get_job(UID, gone) is None and db.get_job(UID, kept)


def test_export_all_then_delete_all(env):
    env.client.put("/api/secrets", json={"harvest_key": "SECRET-HARVEST-KEY"})
    env.storage.upload(config.RESUME_BUCKET, f"{UID}/resume_x.pdf", b"pdf")
    env.storage.upload(config.SCREENSHOT_BUCKET, f"{UID}/s.png", b"png")
    env.storage.upload(config.RESUME_BUCKET, "someone-else/r.pdf", b"keep")
    send(env, add_job())
    z = zipfile.ZipFile(io.BytesIO(env.client.get("/api/export/all.zip").content))
    assert {"data.json", "jobs.csv", "resumes/resume_x.pdf", "screenshots/s.png"} <= set(z.namelist())
    assert b"SECRET-HARVEST-KEY" not in z.read("data.json")
    assert env.client.post("/api/account/delete", json={"confirm": "no"}).status_code == 400
    assert env.client.post("/api/account/delete", json={"confirm": "DELETE"}).status_code == 200
    assert not [p for p in env.fs.store if UID in p]
    assert list(env.storage.files) == [(config.RESUME_BUCKET, "someone-else/r.pdf")]


def test_assistant_stage_and_send_cards(env):
    import assistant
    jid = add_job()
    card = assistant.propose(UID, "send_job", {"job_id": jid}, False)
    assert ["attachment", "(no resume)"] in card["details"]
    assert assistant.propose(UID, "set_stage", {"job_id": jid, "stage": "new"}, False)["risk"] == "high"
    assistant.x_stage(UID, {"job_id": jid, "stage": "offer"})
    assert db.get_job(UID, jid)["stage"] == "offer"
    assert assistant.r_overview(UID, {})["daily_sending"]["limit"] > 0


def test_gmail_sender_builds_threaded_message():
    import base64
    import email

    import gmail_service
    captured = {}

    class Api:
        def users(self): return self
        def messages(self): return self
        def send(self, userId, body):
            captured.update(body)
            return self
        def execute(self): return {"id": "1", "threadId": "T"}

    s = gmail_service.GmailSender.__new__(gmail_service.GmailSender)
    s.service, s.from_name, s.from_email = Api(), "Me", "me@example.com"
    out = s.send("a@b.com", "Re: Hi", "body", thread_id="T", reply_to_id="<x@example.com>")
    msg = email.message_from_bytes(base64.urlsafe_b64decode(captured["raw"]))
    assert captured["threadId"] == "T" and msg["In-Reply-To"] == msg["References"] == "<x@example.com>"
    assert out == {"id": "1", "thread_id": "T", "message_id": msg["Message-ID"]}


def test_assistant_approved_send_and_send_all(env, monkeypatch):
    import assistant
    jid = add_job()
    card = assistant.propose(UID, "send_job", {"job_id": jid}, False)
    action = assistant._ACTIONS[card["id"]]
    assert assistant.x_send_job(UID, action["args"]) == "Sent to hr@acme.com."
    add_job(recruiter_email="z@zed.com", company="Zed")
    card = assistant.propose(UID, "send_all_ready", {}, False)
    args = assistant._ACTIONS[card["id"]]["args"]
    monkeypatch.setattr(pipeline.Ctx, "wait", lambda self, s: False)
    run_sync(["send"], args["only"])
    assert [m["to"] for m in env.outbox] == ["hr@acme.com", "z@zed.com"]
