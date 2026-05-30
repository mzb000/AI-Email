from __future__ import annotations

import os
from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
    create_engine, inspect, text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

# Allow overriding the DB path via env var (useful for Railway volume mounts)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./users.db")
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
    contacts = relationship(
        "Contact", back_populates="user", cascade="all, delete-orphan"
    )
    scheduled_emails = relationship(
        "ScheduledEmail", back_populates="user", cascade="all, delete-orphan"
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


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    email = Column(String(200), nullable=False)
    company = Column(String(100), nullable=True, default="")
    phone = Column(String(50), nullable=True, default="")
    list_name = Column(String(100), nullable=True, default="General")
    notes = Column(Text, nullable=True, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="contacts")


class ScheduledEmail(Base):
    __tablename__ = "scheduled_emails"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    recipient = Column(String(200), nullable=False)
    subject = Column(String(300), nullable=False)
    body = Column(Text, nullable=False)
    send_at = Column(DateTime, nullable=False)
    sent = Column(Boolean, default=False)
    failed = Column(Boolean, default=False)
    error_msg = Column(String(500), nullable=True, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="scheduled_emails")


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

    # ── contacts table ────────────────────────────────────────────────────────
    if "contacts" in insp.get_table_names():
        existing = {c["name"] for c in insp.get_columns("contacts")}
        new_cols = {
            "company": "VARCHAR(100) DEFAULT ''",
            "phone": "VARCHAR(50) DEFAULT ''",
            "list_name": "VARCHAR(100) DEFAULT 'General'",
            "notes": "TEXT DEFAULT ''",
        }
        with engine.connect() as conn:
            for col, defn in new_cols.items():
                if col not in existing:
                    try:
                        conn.execute(text(f"ALTER TABLE contacts ADD COLUMN {col} {defn}"))
                    except Exception:
                        pass
            conn.commit()

    # ── scheduled_emails table ────────────────────────────────────────────────
    if "scheduled_emails" in insp.get_table_names():
        existing = {c["name"] for c in insp.get_columns("scheduled_emails")}
        new_cols = {
            "failed": "BOOLEAN DEFAULT 0",
            "error_msg": "VARCHAR(500) DEFAULT ''",
        }
        with engine.connect() as conn:
            for col, defn in new_cols.items():
                if col not in existing:
                    try:
                        conn.execute(text(f"ALTER TABLE scheduled_emails ADD COLUMN {col} {defn}"))
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
