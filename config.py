from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Load .env file if present (local dev only — Railway uses env vars directly)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "tamplates"


def _get_bool(env_key: str, default: bool) -> bool:
    raw_value = os.getenv(env_key)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


EMAIL = os.getenv("EMAIL_ADDRESS", "mzoraofficial@gmail.com")
PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "dkff muxa shsn zpfa")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")

AUTO_REPLY = _get_bool("AUTO_REPLY", True)
AUTO_REPLY_POLL_INTERVAL = int(os.getenv("AUTO_REPLY_POLL_INTERVAL", "30"))
AUTO_REPLY_SEARCH_CRITERIA = os.getenv("AUTO_REPLY_SEARCH_CRITERIA", "UNSEEN")
AUTO_REPLY_FETCH_LIMIT = int(os.getenv("AUTO_REPLY_FETCH_LIMIT", "50"))
MAILBOX_FETCH_LIMIT = int(os.getenv("MAILBOX_FETCH_LIMIT", "20"))

CACHE_ROOT = Path(os.getenv("CACHE_DIR", tempfile.gettempdir()))
MAILBOX_CACHE = Path(os.getenv("MAILBOX_CACHE_PATH", str(CACHE_ROOT / "email_mailbox_cache.json")))

DELAY = float(os.getenv("SEND_DELAY", "0.5"))  # seconds
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10000"))

SEND_TEMPLATE = TEMPLATE_DIR / "send.txt"
REPLY_TEMPLATE = TEMPLATE_DIR / "reply.txt"

# REPLIED_LOG is configurable so it can point to a persistent volume on Railway
REPLIED_LOG = Path(os.getenv("REPLIED_LOG_PATH", str(BASE_DIR / "replied_ids.json")))
