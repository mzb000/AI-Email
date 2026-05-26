from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
    create_engine, inspect, text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DATABASE_URL = "sqlite:///./users.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)

    # Gmail credentials
    gmail_address = Column(String, nullable=True, default="")
    gmail_app_password = Column(String, nullable=True, default="")
    smtp_server = Column(String, nullable=True, default="smtp.gmail.com")
    smtp_port = Column(Integer, nullable=True, default=587)
    imap_server = Column(String, nullable=True, default="imap.gmail.com")

    # Account status
    is_active = Column(Boolean, default=True)
    role = Column(String, default="user")  # "user" | "admin"

    # Security / rate-limiting
    last_login = Column(DateTime, nullable=True)
    login_count = Column(Integer, default=0)
    failed_login_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    email_templates = relationship(
        "UserEmailTemplate", back_populates="user", cascade="all, delete-orphan"
    )
    campaign_logs = relationship(
        "CampaignLog", back_populates="user", cascade="all, delete-orphan"
    )


class UserEmailTemplate(Base):
    __tablename__ = "user_email_templates"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    subject = Column(String, nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="email_templates")


class CampaignLog(Base):
    __tablename__ = "campaign_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    subject = Column(String, nullable=False, default="")
    recipient_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    dry_run = Column(Boolean, default=False)
    sent_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="campaign_logs")


def _run_migrations() -> None:
    """Safely add missing columns to existing tables (SQLite ALTER TABLE)."""
    insp = inspect(engine)

    # ── users table ──────────────────────────────────────────────────────────
    if "users" in insp.get_table_names():
        existing = {c["name"] for c in insp.get_columns("users")}
        new_cols = {
            "smtp_server": "VARCHAR DEFAULT 'smtp.gmail.com'",
            "smtp_port": "INTEGER DEFAULT 587",
            "imap_server": "VARCHAR DEFAULT 'imap.gmail.com'",
            "is_active": "BOOLEAN DEFAULT 1",
            "role": "VARCHAR DEFAULT 'user'",
            "last_login": "DATETIME",
            "login_count": "INTEGER DEFAULT 0",
            "failed_login_attempts": "INTEGER DEFAULT 0",
            "locked_until": "DATETIME",
        }
        with engine.connect() as conn:
            for col, defn in new_cols.items():
                if col not in existing:
                    try:
                        conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {defn}"))
                    except Exception:
                        pass
            conn.commit()


Base.metadata.create_all(bind=engine)
_run_migrations()


# ── Owner account seeding ─────────────────────────────────────────────────────

OWNER_EMAIL          = "mzoraofficial@gmail.com"
OWNER_PASSWORD       = "zabi12345"
OWNER_NAME           = "Zabiullah"
OWNER_GMAIL_ADDRESS  = "mzoraofficial@gmail.com"
OWNER_GMAIL_APP_PW   = "sirq iaem echn ynid"


def seed_owner_account() -> None:
    """Create the owner account if it does not exist yet, and keep Gmail credentials up to date."""
    from passlib.context import CryptContext
    _ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == OWNER_EMAIL).first()
        if not existing:
            owner = User(
                name=OWNER_NAME,
                email=OWNER_EMAIL,
                password_hash=_ctx.hash(OWNER_PASSWORD),
                gmail_address=OWNER_GMAIL_ADDRESS,
                gmail_app_password=OWNER_GMAIL_APP_PW,
                is_active=True,
                role="admin",
            )
            db.add(owner)
            db.commit()
        else:
            # Always keep Gmail credentials in sync with this file
            existing.gmail_address    = OWNER_GMAIL_ADDRESS
            existing.gmail_app_password = OWNER_GMAIL_APP_PW
            db.commit()
    finally:
        db.close()
