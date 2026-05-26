from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Request

from database import SessionLocal, User

# ── JWT config ────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "NexusMail-Ultra-Secure-JWT-Key-2025-Please-Change-In-Production!"
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

# ── Password hashing ──────────────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── Rate limiting (in-memory per email) ───────────────────────────────────────
_login_attempts: dict = defaultdict(lambda: {"count": 0, "locked_until": None})
MAX_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


# ── Core auth helpers ─────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(request: Request) -> User | None:
    token = request.cookies.get("token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        return None
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id, User.is_active == True).first()
    finally:
        db.close()


# ── Rate limiting ─────────────────────────────────────────────────────────────

def check_rate_limit(email: str) -> tuple[bool, int]:
    """Returns (is_locked, seconds_remaining)."""
    state = _login_attempts[email]
    if state["locked_until"] and datetime.utcnow() < state["locked_until"]:
        remaining = int((state["locked_until"] - datetime.utcnow()).total_seconds())
        return True, remaining
    if state["locked_until"] and datetime.utcnow() >= state["locked_until"]:
        state["count"] = 0
        state["locked_until"] = None
    return False, 0


def record_failed_attempt(email: str) -> tuple[int, bool]:
    """Returns (new_count, just_locked)."""
    state = _login_attempts[email]
    if state["locked_until"] and datetime.utcnow() >= state["locked_until"]:
        state["count"] = 0
        state["locked_until"] = None
    state["count"] += 1
    just_locked = False
    if state["count"] >= MAX_ATTEMPTS:
        state["locked_until"] = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
        just_locked = True
    return state["count"], just_locked


def clear_rate_limit(email: str) -> None:
    _login_attempts[email] = {"count": 0, "locked_until": None}


# ── Password strength ─────────────────────────────────────────────────────────

def validate_password_strength(password: str) -> list[str]:
    """Returns list of unmet requirements (empty = strong enough)."""
    errors: list[str] = []
    if len(password) < 8:
        errors.append("At least 8 characters long")
    if not re.search(r"[A-Z]", password):
        errors.append("At least one uppercase letter (A-Z)")
    if not re.search(r"[a-z]", password):
        errors.append("At least one lowercase letter (a-z)")
    if not re.search(r"\d", password):
        errors.append("At least one digit (0-9)")
    if not re.search(r"[!@#$%^&*()\-_=+\[\]{}|;:',.<>?/`~\\\"]+", password):
        errors.append("At least one special character (!@#$% etc.)")
    return errors


def password_strength_score(password: str) -> int:
    """Returns 0-5 score (5 = strongest)."""
    return max(0, 5 - len(validate_password_strength(password)))
