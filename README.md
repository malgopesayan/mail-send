# Job Mailer v2 — multi-user (Google login · Firebase · Supabase Storage)

Every person signs in with **Google only**. Each user has their own Groq keys, an optional NVIDIA key (auto-fallback), HarvestAPI key,
Gmail account, resume, profile, jobs and schedules — all editable from **Settings** in the UI.

| Thing | Where it lives |
|---|---|
| Login | Firebase Auth (Google provider only) |
| Jobs, profile, schedules, run history | Firestore (only the backend can touch it) |
| Per-user API keys + Gmail token | Firestore, **encrypted** with `ENCRYPTION_KEY`; browser only sees masked values |
| Screenshots + resume files | Supabase Storage (free plan), path `<bucket>/<uid>/…` |
| Backend | FastAPI on Render Free |
| Timer | GitHub Actions → `/api/cron/tick` every 20 min (runs every user's due schedules) |

Removed vs v1 (as you asked): the chatbot and RAG/pgvector/Gemini. Replaced by the
**Send all unsent** button on the dashboard.

## Gmail: "can each user send from a different mail?" — yes
Press **Connect Gmail** → Google shows an account chooser → pick **any** account (it can differ from
the login account). Mail is sent from that account (shows in its Sent folder). Two ways to get an OAuth client:
1. **Shared** (default): you create one OAuth client, set `GOOGLE_OAUTH_CLIENT_ID/SECRET`; every user just clicks Connect.
2. **Own**: a user pastes/uploads their own `credentials.json` in Settings → Gmail → Advanced.

⚠ `gmail.send` is a *sensitive* scope. While your OAuth consent screen is in **Testing** mode only the
test users you list (max 100) can connect, and their tokens expire after 7 days. For real multi-user
use, publish the app (Google verification) — or have each user use option 2 with their own client.

## Setup (≈20 min)

### 1. Firebase
1. console.firebase.google.com → create project (Spark/free).
2. **Authentication → Sign-in method → enable Google.**
3. **Authentication → Settings → Authorized domains → add** your Render domain (`xxx.onrender.com`).
4. **Firestore Database → Create** (production mode). Paste `firestore.rules` in the Rules tab → Publish.
5. **Project settings → General → Your apps → Web (`</>`)** → copy the `firebaseConfig` object as one-line JSON → `FIREBASE_WEB_CONFIG_JSON`.
6. **Project settings → Service accounts → Generate new private key** → paste the JSON (one line) into `FIREBASE_SERVICE_ACCOUNT_JSON`.

### 2. Supabase (free)
Create a project → **Storage → New bucket** twice: `screenshots` and `resumes` (both **private**).
**Project settings → API** → `SUPABASE_URL` and the `service_role` (or new "secret") key → `SUPABASE_KEY`.
No SQL, no tables — Supabase is used for files only. (Free projects pause after 7 idle days; the cron tick pings storage so yours won't.)

### 3. Google Cloud (Gmail)
console.cloud.google.com → (same project as Firebase is fine) → enable **Gmail API** →
OAuth consent screen (External; add yourself/users as test users) → Credentials → **OAuth client ID → Web application**
→ Authorized redirect URI: `https://YOUR-APP.onrender.com/api/gmail/callback` (and `http://localhost:8000/api/gmail/callback` for local).
Put the id/secret in `GOOGLE_OAUTH_CLIENT_ID/SECRET`.

### 4. Secrets
```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # ENCRYPTION_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"                                  # CRON_SECRET
```
**Never change `ENCRYPTION_KEY` later** — saved user keys could no longer be decrypted.
Optionally set `ALLOWED_EMAILS=you@gmail.com,friend@gmail.com` to restrict who can log in (protects your free quotas).

### 5. Deploy on Render
Push this folder to GitHub → Render → New → Blueprint (uses `render.yaml`) → fill the env vars from `.env.example`
(`APP_BASE_URL` = your Render URL). Open the URL, sign in with Google, finish the checklist on the dashboard.

### 6. GitHub Actions timer
Repo → Settings → Secrets → `APP_URL` (e.g. `https://job-mailer.onrender.com`) and `CRON_SECRET`.
`.github/workflows/cron.yml` wakes Render, starts due schedules and stays pinging until runs finish
(so Render doesn't sleep mid-run).

### Run locally
```
pip install -r requirements.txt
cp .env.example .env   # fill it; APP_BASE_URL=http://localhost:8000
uvicorn main:app --reload
```

## Using it
- **Settings**: add Groq key(s), HarvestAPI key, connect Gmail, upload resume → *Build profile & keywords*, save.
- **Screenshots**: drop images → *Read screenshots* → jobs + draft emails appear, images are deleted.
- **Dashboard**: click a job to edit the draft; **Send** per row; **Send all unsent** mails everything (cap per run in Settings, default 40, 1.5 s apart).
- **Schedule**: daily times in *your* timezone: Find jobs / Read screenshots + send / Everything.

## Free-tier notes
- Firestore Spark: 50k reads / 20k writes per day — plenty for a few users (the dashboard loads ≤600 jobs per open).
- Supabase 1 GB: screenshots are deleted after processing; per-user cap of 60 waiting images, 10 MB each.
- Render Free: 512 MB RAM → at most 2 simultaneous runs (`MAX_CONCURRENT_RUNS`); first request after sleep takes ~30 s.
- **AI provider** (Settings → Preferences): *Auto* uses Groq first and switches to NVIDIA (`integrate.api.nvidia.com`) when Groq errors / rate-limits; or force *Groq only* / *NVIDIA only*. Default NVIDIA model is `nvidia/nemotron-3.5-super-vl-120b-a12b` (editable in Settings). Optional server-wide fallback: set `NVIDIA_API_KEY` env var.
- Groq / NVIDIA model names change — edit them in Settings, no redeploy.

## Files
`main.py` routes · `pipeline.py` search/process/send · `scheduler.py` per-user cron · `llm.py` Groq/NVIDIA router · `ai.py` prompts+HarvestAPI ·
`gmail_service.py` per-user OAuth+send · `db.py` Firestore · `storage.py` Supabase · `crypto.py` · `auth.py` · `static/index.html` UI.
"# mail-send" 
