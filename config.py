"""All environment configuration in one place."""
import os
from dotenv import load_dotenv

load_dotenv()


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# Public URL of this backend, no trailing slash. Used for the Gmail OAuth
# redirect URI:  <APP_BASE_URL>/api/gmail/callback
APP_BASE_URL = env("APP_BASE_URL", "http://localhost:8000").rstrip("/")
GMAIL_REDIRECT_URI = f"{APP_BASE_URL}/api/gmail/callback"

# --- Firebase (Auth + Firestore) ---
FIREBASE_SERVICE_ACCOUNT_JSON = env("FIREBASE_SERVICE_ACCOUNT_JSON")  # raw JSON or base64 of it
FIREBASE_WEB_CONFIG_JSON = env("FIREBASE_WEB_CONFIG_JSON")            # the web-app config object, as JSON

# --- Supabase (Storage only: screenshots + resumes) ---
SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = env("SUPABASE_KEY")                      # service_role / secret key (server only)
SCREENSHOT_BUCKET = env("SUPABASE_BUCKET", "screenshots")
RESUME_BUCKET = env("SUPABASE_RESUME_BUCKET", "resumes")

# --- Security ---
ENCRYPTION_KEY = env("ENCRYPTION_KEY")                  # encrypts every user's API keys / Gmail token
CRON_SECRET = env("CRON_SECRET")
# Optional comma separated allow-list. Empty = any Google account may sign in.
ALLOWED_EMAILS = {e.strip().lower() for e in env("ALLOWED_EMAILS").split(",") if e.strip()}

# --- Shared Gmail OAuth client (used unless a user pastes their own) ---
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET")

# --- NVIDIA (optional server-wide fallback key; users can also save their own in Settings) ---
NVIDIA_API_KEY = env("NVIDIA_API_KEY")
NVIDIA_BASE_URL = env("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

# --- Defaults (every one of these can be changed per user in Settings) ---
DEFAULT_VISION_MODEL = env("VISION_MODEL", "qwen/qwen3.8-27b")
DEFAULT_CHAT_MODEL = env("CHAT_MODEL", "qwen/qwen3.8-27b")
DEFAULT_NVIDIA_VISION_MODEL = env("NVIDIA_VISION_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")
DEFAULT_NVIDIA_CHAT_MODEL = env("NVIDIA_CHAT_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")
DEFAULT_TZ = env("APP_TZ", "Asia/Kolkata")
SCHEDULE_GRACE_MIN = int(env("SCHEDULE_GRACE_MIN", "90"))
HARVESTAPI_BASE = "https://api.harvestapi.io"

# --- Free-tier guard rails ---
MAX_UPLOAD_MB = 10                   # Supabase free allows 50 MB; screenshots are small
MAX_PENDING_SCREENSHOTS = int(env("MAX_PENDING_SCREENSHOTS", "60"))
MAX_CONCURRENT_RUNS = int(env("MAX_CONCURRENT_RUNS", "2"))   # Render free = 512 MB RAM

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
RESUME_EXTENSIONS = {".pdf", ".doc", ".docx"}
