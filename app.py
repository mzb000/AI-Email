from __future__ import annotations

import csv
import imaplib
import io
import json as _json
import smtplib
import threading
import time as _time
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path
import re
from threading import Lock
from typing import List

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from starlette import status
from starlette.responses import Response

from auto_reply import AutoReplyService
from auth import (
    check_rate_limit,
    clear_rate_limit,
    create_token,
    get_current_user,
    hash_password,
    record_failed_attempt,
    validate_password_strength,
    verify_password,
    password_strength_score,
    LOCKOUT_MINUTES,
    MAX_ATTEMPTS,
)
from database import CampaignLog, Contact, ScheduledEmail, SessionLocal, User, UserEmailTemplate, seed_owner_account
from config import (
    AUTO_REPLY_POLL_INTERVAL,
    AUTO_REPLY_SEARCH_CRITERIA,
    BATCH_SIZE,
    DELAY,
)
from mailbox import fetch_recent_emails, get_mailbox_snapshot
from send_emails import EmailTemplate, send_bulk_emails

# ── App setup ─────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates_web"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Nexus Mail", docs_url=None, redoc_url=None, openapi_url=None)

# Seed the owner account on every startup
seed_owner_account()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Security headers middleware ───────────────────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response

app.add_middleware(SecurityHeadersMiddleware)

_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    auto_reload=True,
    cache_size=0,
)
_jinja_env.filters["tojson"] = lambda v: _json.dumps(v, ensure_ascii=False).replace('</', '<\\/')


def _render(name: str, context: dict) -> HTMLResponse:
    tmpl = _jinja_env.get_template(name)
    return HTMLResponse(tmpl.render(**context))


# ── Per-user state ────────────────────────────────────────────────────────────

app.state.user_data: dict[int, dict] = {}
app.state.user_services: dict[int, AutoReplyService] = {}


def _get_user_data(user_id: int) -> dict:
    if user_id not in app.state.user_data:
        app.state.user_data[user_id] = {
            "send_log": [],
            "auto_reply_log": [],
            "flash": None,
            "saved_drafts": [],
            "draft_lock": Lock(),
            "auto_reply_count": 0,
        }
    return app.state.user_data[user_id]


def _get_user_service(user: User) -> AutoReplyService:
    uid = user.id
    if uid not in app.state.user_services:
        app.state.user_services[uid] = AutoReplyService(
            email=user.gmail_address or None,
            password=user.gmail_app_password or None,
        )
    return app.state.user_services[uid]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _push_log(target: List[str], message: str, prefix: str | None = None) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {prefix + ' ' if prefix else ''}{message}"
    target.append(line)
    if len(target) > 200:
        del target[: len(target) - 200]


def _set_flash(user_data: dict, text: str, level: str = "info") -> None:
    user_data["flash"] = {"text": text, "level": level}


def _consume_flash(user_data: dict) -> dict | None:
    msg = user_data.get("flash")
    user_data["flash"] = None
    return msg


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


_EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)


def _extract_recipients(raw: str) -> tuple[list[str], list[str]]:
    if not raw:
        return [], []
    normalized = raw.replace(";", ",").replace("\t", ",").replace("\r", "\n")
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for line in normalized.splitlines():
        for token in line.split(","):
            token = token.strip()
            if not token:
                continue
            _, addr = parseaddr(token)
            addr = (addr or "").strip()
            if addr and _EMAIL_RE.fullmatch(addr) and addr not in seen:
                valid.append(addr)
                seen.add(addr)
            elif token:
                invalid.append(token)
    return valid, invalid


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _draft_signature(subject: str, body: str) -> str:
    return _normalize_text(subject)[:120] + "|" + _normalize_text(body)[:400]


def _save_draft(ud: dict, subject: str, body: str) -> None:
    s, b = (subject or "").strip(), (body or "").strip()
    if not s and not b:
        return
    sig = _draft_signature(s, b)
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with ud["draft_lock"]:
        for item in ud["saved_drafts"]:
            if item.get("signature") == sig:
                item.update({"subject": s, "body": b, "updated_at": now})
                return
        ud["saved_drafts"].insert(0, {"signature": sig, "subject": s, "body": b, "updated_at": now})
        if len(ud["saved_drafts"]) > 30:
            del ud["saved_drafts"][30:]


def _require_auth(request: Request):
    """Returns (user, None) or (None, redirect_response)."""
    user = get_current_user(request)
    if not user:
        return None, _redirect("/login")
    return user, None


def _require_gmail(user: User, ud: dict):
    """Returns error redirect if Gmail not configured."""
    if not user.gmail_address or not user.gmail_app_password:
        _set_flash(ud, "Please configure your Gmail credentials in Settings first.", "warning")
        return _redirect("/settings")
    return None


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page(request: Request, error: str = ""):
    if get_current_user(request):
        return _redirect("/")
    return _render("login.html", {"request": request, "error": error})


@app.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    email = email.strip().lower()

    # Rate limit check
    locked, secs = check_rate_limit(email)
    if locked:
        mins = (secs + 59) // 60
        return _render("login.html", {
            "request": request,
            "error": f"Account temporarily locked. Try again in {mins} minute(s).",
        })

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
    finally:
        db.close()

    if not user or not verify_password(password, user.password_hash):
        count, just_locked = record_failed_attempt(email)
        remaining = MAX_ATTEMPTS - count
        if just_locked:
            msg = f"Too many failed attempts. Account locked for {LOCKOUT_MINUTES} minutes."
        elif remaining <= 2:
            msg = f"Invalid email or password. {remaining} attempt(s) remaining before lockout."
        else:
            msg = "Invalid email or password."
        return _render("login.html", {"request": request, "error": msg})

    if not user.is_active:
        return _render("login.html", {"request": request, "error": "This account has been deactivated."})

    clear_rate_limit(email)

    # Update last_login & count
    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.id == user.id).first()
        if db_user:
            db_user.last_login = datetime.utcnow()
            db_user.login_count = (db_user.login_count or 0) + 1
            db_user.failed_login_attempts = 0
            db.commit()
    finally:
        db.close()

    token = create_token(user.id)
    resp = _redirect("/")
    resp.set_cookie("token", token, httponly=True, max_age=60 * 60 * 24 * 30, samesite="lax", secure=False)
    return resp


@app.get("/signup")
async def signup_page(request: Request):
    # Signup is disabled — only the owner account can log in
    return _redirect("/login")


@app.post("/signup")
async def signup_submit(request: Request):
    return _redirect("/login")


@app.post("/logout")
async def logout():
    resp = _redirect("/login")
    resp.delete_cookie("token")
    return resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/")
async def dashboard(request: Request):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    svc = _get_user_service(user)
    flash = _consume_flash(ud)

    if not user.gmail_address and not flash:
        flash = {
            "text": "Welcome! Go to Settings to connect your Gmail account.",
            "level": "warning",
        }

    # DB stats
    db = SessionLocal()
    try:
        campaigns_total = db.query(CampaignLog).filter(
            CampaignLog.user_id == user.id, CampaignLog.dry_run == False
        ).count()
        emails_sent = db.query(CampaignLog).filter(
            CampaignLog.user_id == user.id, CampaignLog.dry_run == False
        ).with_entities(CampaignLog.recipient_count).all()
        total_sent = sum(r[0] or 0 for r in emails_sent)
        templates_count = db.query(UserEmailTemplate).filter(
            UserEmailTemplate.user_id == user.id
        ).count()
        recent_campaigns = db.query(CampaignLog).filter(
            CampaignLog.user_id == user.id
        ).order_by(CampaignLog.sent_at.desc()).limit(5).all()
    finally:
        db.close()

    mailbox = []
    if user.gmail_address and user.gmail_app_password:
        mailbox = get_mailbox_snapshot(
            email_addr=user.gmail_address or None,
            password=user.gmail_app_password or None,
        )

    return _render("dashboard.html", {
        "request": request,
        "user": user,
        "active": "dashboard",
        "flash": flash,
        "auto_reply_running": svc.is_running,
        "auto_reply_poll_interval": AUTO_REPLY_POLL_INTERVAL,
        "auto_reply_search": AUTO_REPLY_SEARCH_CRITERIA,
        "auto_reply_count": ud.get("auto_reply_count", 0),
        "send_log": list(ud["send_log"]),
        "auto_reply_log": list(ud["auto_reply_log"]),
        "default_delay": DELAY,
        "default_batch_size": BATCH_SIZE,
        "mailbox": mailbox[:5],
        "campaigns_total": campaigns_total,
        "total_sent": total_sent,
        "templates_count": templates_count,
        "recent_campaigns": recent_campaigns,
        "saved_drafts": list(ud["saved_drafts"]),
    })


# ── Inbox ─────────────────────────────────────────────────────────────────────

@app.get("/inbox")
async def inbox_page(request: Request, q: str = ""):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    flash = _consume_flash(ud)

    mailbox: list = []
    error_msg = ""
    if user.gmail_address and user.gmail_app_password:
        try:
            mailbox = fetch_recent_emails(
                email_addr=user.gmail_address,
                password=user.gmail_app_password,
            )
        except Exception as e:
            import traceback, logging
            logging.error("IMAP fetch failed for %s: %s\n%s", user.gmail_address, e, traceback.format_exc())
            error_msg = f"IMAP Error: {e}"
    else:
        flash = {"text": "Connect your Gmail account in Settings to see your inbox.", "level": "warning"}

    if q:
        q_lower = q.lower()
        mailbox = [
            m for m in mailbox
            if q_lower in (m.get("subject") or "").lower()
            or q_lower in (m.get("from") or "").lower()
            or q_lower in (m.get("body") or "").lower()
        ]

    return _render("inbox.html", {
        "request": request,
        "user": user,
        "active": "inbox",
        "flash": flash,
        "mailbox": mailbox,
        "query": q,
        "error_msg": error_msg,
    })


@app.get("/inbox/data")
async def inbox_data(request: Request):
    """Return cached emails as JSON — no IMAP call, used for auto-refresh polling."""
    user, _ = _require_auth(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    try:
        from mailbox import _load_cached_messages
        msgs = _load_cached_messages()
        return JSONResponse({"ok": True, "emails": msgs, "count": len(msgs)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/inbox/refresh")
async def inbox_refresh(request: Request):
    user, _ = _require_auth(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not authenticated"})
    if not user.gmail_address or not user.gmail_app_password:
        return JSONResponse({"ok": False, "error": "No Gmail credentials"})
    try:
        mailbox = fetch_recent_emails(
            email_addr=user.gmail_address,
            password=user.gmail_app_password,
        )
        return JSONResponse({"ok": True, "count": len(mailbox)})
    except Exception as e:
        import logging
        logging.error("IMAP refresh failed for %s: %s", user.gmail_address, e)
        return JSONResponse({"ok": False, "error": str(e)})


# ── Compose (single email) ────────────────────────────────────────────────────

@app.get("/compose")
async def compose_page(request: Request, to: str = "", subject: str = "", template_id: str = ""):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    flash = _consume_flash(ud)

    # Pre-fill from template if requested
    prefill_subject = subject
    prefill_body = ""
    if template_id:
        db = SessionLocal()
        try:
            tmpl = db.query(UserEmailTemplate).filter(
                UserEmailTemplate.id == int(template_id),
                UserEmailTemplate.user_id == user.id,
            ).first()
            if tmpl:
                prefill_subject = tmpl.subject
                prefill_body = tmpl.body
        except Exception:
            pass
        finally:
            db.close()

    return _render("compose.html", {
        "request": request,
        "user": user,
        "active": "compose",
        "flash": flash,
        "prefill_to": to,
        "prefill_subject": prefill_subject,
        "prefill_body": prefill_body,
    })


@app.post("/compose")
async def compose_send(
    request: Request,
    to: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    attachments: List[UploadFile] | None = File(None),
):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    err = _require_gmail(user, ud)
    if err:
        return err

    to = to.strip()
    if not _EMAIL_RE.fullmatch(to):
        _set_flash(ud, f"Invalid email address: {to}", "error")
        return _redirect("/compose")

    try:
        template = EmailTemplate(subject=subject.strip(), body=body.strip())
        attachment_payload: list[tuple[str, bytes, str | None]] = []
        if attachments:
            for upload in attachments:
                if upload and upload.filename:
                    data = await upload.read()
                    attachment_payload.append((upload.filename, data, upload.content_type))

        send_log: list[str] = []
        def capture(msg: str) -> None:
            _push_log(send_log, msg, prefix="Send")

        send_bulk_emails(
            [to],
            template=template,
            attachments=attachment_payload,
            delay=0,
            status_callback=capture,
            email=user.gmail_address,
            password=user.gmail_app_password,
        )
        ud["send_log"] = send_log

        # Log campaign
        db = SessionLocal()
        try:
            log = CampaignLog(
                user_id=user.id,
                subject=subject.strip(),
                recipient_count=1,
                success_count=1,
                fail_count=0,
                dry_run=False,
            )
            db.add(log)
            db.commit()
        finally:
            db.close()

        _set_flash(ud, f"Email sent to {to} successfully!", "success")
    except Exception as exc:
        _set_flash(ud, f"Failed to send: {exc}", "error")

    return _redirect("/compose")


# ── Email Templates ───────────────────────────────────────────────────────────

@app.get("/templates")
async def templates_page(request: Request):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    flash = _consume_flash(ud)

    db = SessionLocal()
    try:
        templates = db.query(UserEmailTemplate).filter(
            UserEmailTemplate.user_id == user.id
        ).order_by(UserEmailTemplate.updated_at.desc()).all()
        templates_data = [
            {
                "id": t.id,
                "name": t.name,
                "subject": t.subject,
                "body": t.body,
                "created_at": t.created_at.strftime("%b %d, %Y") if t.created_at else "",
                "updated_at": t.updated_at.strftime("%b %d, %Y") if t.updated_at else "",
            }
            for t in templates
        ]
    finally:
        db.close()

    return _render("templates_page.html", {
        "request": request,
        "user": user,
        "active": "templates",
        "flash": flash,
        "templates": templates_data,
    })


@app.post("/templates/save")
async def template_save(
    request: Request,
    template_id: str = Form(""),
    name: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    name, subject, body = name.strip(), subject.strip(), body.strip()

    if not name:
        _set_flash(ud, "Template name is required.", "error")
        return _redirect("/templates")

    db = SessionLocal()
    try:
        if template_id:
            tmpl = db.query(UserEmailTemplate).filter(
                UserEmailTemplate.id == int(template_id),
                UserEmailTemplate.user_id == user.id,
            ).first()
            if tmpl:
                tmpl.name = name
                tmpl.subject = subject
                tmpl.body = body
                tmpl.updated_at = datetime.utcnow()
                db.commit()
                _set_flash(ud, f"Template '{name}' updated.", "success")
            else:
                _set_flash(ud, "Template not found.", "error")
        else:
            count = db.query(UserEmailTemplate).filter(
                UserEmailTemplate.user_id == user.id
            ).count()
            if count >= 20:
                _set_flash(ud, "Template limit reached (max 20).", "error")
            else:
                new_tmpl = UserEmailTemplate(
                    user_id=user.id,
                    name=name,
                    subject=subject,
                    body=body,
                )
                db.add(new_tmpl)
                db.commit()
                _set_flash(ud, f"Template '{name}' saved!", "success")
    finally:
        db.close()

    return _redirect("/templates")


@app.post("/templates/{tid}/delete")
async def template_delete(request: Request, tid: int):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    db = SessionLocal()
    try:
        tmpl = db.query(UserEmailTemplate).filter(
            UserEmailTemplate.id == tid,
            UserEmailTemplate.user_id == user.id,
        ).first()
        if tmpl:
            db.delete(tmpl)
            db.commit()
            _set_flash(ud, "Template deleted.", "success")
        else:
            _set_flash(ud, "Template not found.", "error")
    finally:
        db.close()

    return _redirect("/templates")


# ── Bulk send campaign ────────────────────────────────────────────────────────

@app.post("/send")
async def send_campaign(
    request: Request,
    recipients_text: str | None = Form(None),
    attachments: List[UploadFile] | None = File(None),
    subject: str = Form(...),
    body: str = Form(...),
    delay: str | None = Form(None),
    batch_size: str | None = Form(None),
    dry_run: bool = Form(False),
):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    err = _require_gmail(user, ud)
    if err:
        return err

    valid, invalid = _extract_recipients(recipients_text or "")
    if not valid:
        sample = ", ".join(invalid[:4])
        _set_flash(ud, f"No valid recipients found. Invalid: {sample}", "error")
        return _redirect("/")

    if invalid:
        _set_flash(ud, f"Skipped {len(invalid)} invalid address(es).", "info")

    try:
        template = EmailTemplate(subject=subject.strip(), body=body.strip())
        delay_val = float(delay) if delay not in (None, "") else None
        batch_val = int(batch_size) if batch_size not in (None, "") else None

        attachment_payload: list[tuple[str, bytes, str | None]] = []
        if attachments:
            for upload in attachments:
                if upload and upload.filename:
                    data = await upload.read()
                    attachment_payload.append((upload.filename, data, upload.content_type))

        send_log: list[str] = []
        def capture(msg: str) -> None:
            _push_log(send_log, msg, prefix="Campaign")

        send_bulk_emails(
            valid,
            template=template,
            attachments=attachment_payload,
            delay=delay_val,
            batch_size=batch_val,
            dry_run=dry_run,
            status_callback=capture,
            email=user.gmail_address,
            password=user.gmail_app_password,
        )
        ud["send_log"] = send_log

        if not dry_run:
            _save_draft(ud, subject.strip(), body.strip())
            db = SessionLocal()
            try:
                log = CampaignLog(
                    user_id=user.id,
                    subject=subject.strip(),
                    recipient_count=len(valid),
                    success_count=len(valid),
                    fail_count=0,
                    dry_run=False,
                )
                db.add(log)
                db.commit()
            finally:
                db.close()
            _set_flash(ud, f"Campaign sent to {len(valid)} recipients!", "success")
        else:
            _set_flash(ud, f"Dry-run complete — {len(valid)} recipients previewed.", "success")

    except Exception as exc:
        _set_flash(ud, f"Campaign failed: {exc}", "error")

    return _redirect("/")


# ── Auto-reply ────────────────────────────────────────────────────────────────

@app.post("/auto-reply/start")
async def start_auto_reply(request: Request):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    err = _require_gmail(user, ud)
    if err:
        return err

    svc = _get_user_service(user)
    if svc.is_running:
        _set_flash(ud, "Auto-reply is already running.", "info")
        return _redirect("/")

    def capture(msg: str) -> None:
        _push_log(ud["auto_reply_log"], msg, prefix="AutoReply")
        if "Auto-replied" in msg:
            ud["auto_reply_count"] = ud.get("auto_reply_count", 0) + 1

    svc.start(status_callback=capture)
    _set_flash(ud, "Auto-reply service started successfully.", "success")
    return _redirect("/")


@app.post("/auto-reply/stop")
async def stop_auto_reply(request: Request):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    svc = _get_user_service(user)

    if not svc.is_running:
        _set_flash(ud, "Auto-reply is not running.", "info")
        return _redirect("/")

    svc.stop()
    _set_flash(ud, "Auto-reply service stopped.", "success")
    return _redirect("/")


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/settings")
async def settings_page(request: Request, welcome: str = "", tab: str = "gmail"):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    flash = _consume_flash(ud)

    return _render("settings.html", {
        "request": request,
        "user": user,
        "active": "settings",
        "flash": flash,
        "welcome": welcome == "1",
        "active_tab": tab,
    })


@app.post("/settings/gmail")
async def settings_gmail(
    request: Request,
    gmail_address: str = Form(""),
    gmail_app_password: str = Form(""),
    smtp_server: str = Form("smtp.gmail.com"),
    smtp_port: str = Form("587"),
    imap_server: str = Form("imap.gmail.com"),
):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    gmail_address = gmail_address.strip()
    gmail_app_password = gmail_app_password.strip()

    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.id == user.id).first()
        if db_user:
            db_user.gmail_address = gmail_address
            if gmail_app_password:
                db_user.gmail_app_password = gmail_app_password
            db_user.smtp_server = smtp_server.strip() or "smtp.gmail.com"
            db_user.smtp_port = int(smtp_port) if smtp_port.isdigit() else 587
            db_user.imap_server = imap_server.strip() or "imap.gmail.com"
            db.commit()
    finally:
        db.close()

    # Restart service if running
    uid = user.id
    if uid in app.state.user_services:
        svc = app.state.user_services[uid]
        was_running = svc.is_running
        if was_running:
            svc.stop()
        app.state.user_services[uid] = AutoReplyService(
            email=gmail_address or None,
            password=gmail_app_password or None,
        )
        if was_running and gmail_address and gmail_app_password:
            def capture(msg: str) -> None:
                _push_log(ud["auto_reply_log"], msg, prefix="AutoReply")
            app.state.user_services[uid].start(status_callback=capture)

    _set_flash(ud, "Gmail credentials saved successfully.", "success")
    return _redirect("/settings?tab=gmail")


@app.post("/settings/profile")
async def settings_profile(
    request: Request,
    name: str = Form(...),
):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    name = name.strip()

    if not name:
        _set_flash(ud, "Name cannot be empty.", "error")
        return _redirect("/settings?tab=profile")

    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.id == user.id).first()
        if db_user:
            db_user.name = name
            db.commit()
    finally:
        db.close()

    _set_flash(ud, "Profile updated successfully.", "success")
    return _redirect("/settings?tab=profile")


@app.post("/settings/password")
async def settings_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)

    if not verify_password(current_password, user.password_hash):
        _set_flash(ud, "Current password is incorrect.", "error")
        return _redirect("/settings?tab=security")

    if new_password != confirm_password:
        _set_flash(ud, "New passwords do not match.", "error")
        return _redirect("/settings?tab=security")

    errors = validate_password_strength(new_password)
    if errors:
        _set_flash(ud, f"Password too weak: {errors[0]}", "error")
        return _redirect("/settings?tab=security")

    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.id == user.id).first()
        if db_user:
            db_user.password_hash = hash_password(new_password)
            db.commit()
    finally:
        db.close()

    _set_flash(ud, "Password changed successfully. Please log in again.", "success")
    resp = _redirect("/login")
    resp.delete_cookie("token")
    return resp


@app.post("/settings/test")
async def settings_test_connection(request: Request):
    user, _ = _require_auth(request)
    if not user:
        return JSONResponse({"success": False, "message": "Not authenticated"})

    gmail = user.gmail_address
    password = user.gmail_app_password

    if not gmail or not password:
        return JSONResponse({"success": False, "message": "No credentials configured yet."})

    # Test IMAP
    try:
        with imaplib.IMAP4_SSL(user.imap_server or "imap.gmail.com") as imap:
            imap.login(gmail, password)
    except Exception as e:
        return JSONResponse({"success": False, "message": f"IMAP failed: {e}"})

    # Test SMTP
    try:
        smtp = smtplib.SMTP(user.smtp_server or "smtp.gmail.com", user.smtp_port or 587, timeout=10)
        smtp.starttls()
        smtp.login(gmail, password)
        smtp.quit()
    except Exception as e:
        return JSONResponse({"success": False, "message": f"SMTP failed: {e}"})

    return JSONResponse({"success": True, "message": "Gmail credentials verified successfully!"})


@app.post("/settings/delete")
async def settings_delete_account(
    request: Request,
    confirm_password: str = Form(...),
):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)

    if not verify_password(confirm_password, user.password_hash):
        _set_flash(ud, "Incorrect password. Account not deleted.", "error")
        return _redirect("/settings?tab=danger")

    # Stop auto-reply service
    uid = user.id
    if uid in app.state.user_services:
        svc = app.state.user_services[uid]
        if svc.is_running:
            svc.stop()
        del app.state.user_services[uid]
    if uid in app.state.user_data:
        del app.state.user_data[uid]

    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.id == uid).first()
        if db_user:
            db_user.is_active = False
            db.commit()
    finally:
        db.close()

    resp = _redirect("/login")
    resp.delete_cookie("token")
    return resp


# ── Stats API ─────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats(request: Request):
    user, _ = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    ud = _get_user_data(user.id)
    svc = _get_user_service(user)

    db = SessionLocal()
    try:
        campaigns = db.query(CampaignLog).filter(
            CampaignLog.user_id == user.id, CampaignLog.dry_run == False
        ).all()
        templates_count = db.query(UserEmailTemplate).filter(
            UserEmailTemplate.user_id == user.id
        ).count()
    finally:
        db.close()

    return JSONResponse({
        "campaigns_sent": len(campaigns),
        "total_emails_sent": sum(c.recipient_count or 0 for c in campaigns),
        "templates_count": templates_count,
        "auto_reply_running": svc.is_running,
        "auto_reply_count": ud.get("auto_reply_count", 0),
    })


# ── Draft helpers ─────────────────────────────────────────────────────────────

@app.post("/draft/save")
async def save_draft(request: Request, subject: str = Form(""), body: str = Form("")):
    user, _ = _require_auth(request)
    if not user:
        return JSONResponse({"ok": False})
    ud = _get_user_data(user.id)
    _save_draft(ud, subject, body)
    return JSONResponse({"ok": True})


@app.get("/api/templates")
async def api_templates(request: Request):
    user, _ = _require_auth(request)
    if not user:
        return JSONResponse({"templates": []})
    db = SessionLocal()
    try:
        templates = db.query(UserEmailTemplate).filter(
            UserEmailTemplate.user_id == user.id
        ).order_by(UserEmailTemplate.updated_at.desc()).all()
        return JSONResponse({
            "templates": [
                {"id": t.id, "name": t.name, "subject": t.subject, "body": t.body}
                for t in templates
            ]
        })
    finally:
        db.close()


@app.get("/draft/suggest")
async def suggest_draft(request: Request, q: str = ""):
    user, _ = _require_auth(request)
    if not user:
        return JSONResponse({"items": []})

    ud = _get_user_data(user.id)
    query = _normalize_text(q)
    if not query:
        return JSONResponse({"items": []})

    def score(item: dict) -> float:
        hay = _normalize_text(f"{item.get('subject', '')} {item.get('body', '')}")
        if not hay:
            return 0.0
        s = 2.0 if query in hay else 0.0
        qt = set(query.split())
        ht = set(hay.split())
        if qt and ht:
            s += len(qt & ht) / max(1, len(qt))
        return s

    with ud["draft_lock"]:
        scored = sorted([(score(d), d) for d in ud["saved_drafts"]], key=lambda x: x[0], reverse=True)

    results = [
        {"subject": d.get("subject", ""), "body": d.get("body", ""), "updated_at": d.get("updated_at")}
        for sc, d in scored if sc > 0
    ][:5]
    return JSONResponse({"items": results})


# ── Scheduled email background worker ────────────────────────────────────────

def _scheduled_email_worker():
    while True:
        try:
            db = SessionLocal()
            now = datetime.utcnow()
            pending = db.query(ScheduledEmail).filter(
                ScheduledEmail.sent == False,
                ScheduledEmail.failed == False,
                ScheduledEmail.send_at <= now,
            ).all()
            for sched in pending:
                user = db.query(User).filter(User.id == sched.user_id).first()
                if not user or not user.gmail_address or not user.gmail_app_password:
                    continue
                try:
                    msg = MIMEText(sched.body)
                    msg["Subject"] = sched.subject
                    msg["From"] = user.gmail_address
                    msg["To"] = sched.recipient
                    with smtplib.SMTP(user.smtp_server or "smtp.gmail.com", user.smtp_port or 587) as s:
                        s.starttls()
                        s.login(user.gmail_address, user.gmail_app_password)
                        s.send_message(msg)
                    sched.sent = True
                except Exception as e:
                    sched.failed = True
                    sched.error_msg = str(e)[:500]
            db.commit()
        except Exception:
            pass
        finally:
            try:
                db.close()
            except Exception:
                pass
        _time.sleep(60)


@app.on_event("startup")
async def start_scheduler():
    import logging
    logging.getLogger("uvicorn").info("NexusMail v2 startup — SMTP fixed build [port-465-ssl]")
    t = threading.Thread(target=_scheduled_email_worker, daemon=True)
    t.start()


# ── Contacts routes ───────────────────────────────────────────────────────────

@app.get("/contacts")
async def contacts_page(request: Request):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    flash = _consume_flash(ud)

    db = SessionLocal()
    try:
        contacts = db.query(Contact).filter(
            Contact.user_id == user.id
        ).order_by(Contact.created_at.desc()).all()
        contacts_data = [
            {
                "id": c.id,
                "name": c.name,
                "email": c.email,
                "company": c.company or "",
                "phone": c.phone or "",
                "list_name": c.list_name or "General",
                "notes": c.notes or "",
                "created_at": c.created_at,
            }
            for c in contacts
        ]
    finally:
        db.close()

    return _render("contacts.html", {
        "request": request,
        "user": user,
        "active": "contacts",
        "flash": flash,
        "contacts": contacts_data,
    })


@app.post("/contacts/add")
async def contacts_add(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    company: str = Form(""),
    phone: str = Form(""),
    list_name: str = Form("General"),
    notes: str = Form(""),
):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    email = email.strip().lower()
    if not email or not _EMAIL_RE.fullmatch(email):
        _set_flash(ud, "Invalid email address.", "error")
        return _redirect("/contacts")

    db = SessionLocal()
    try:
        contact = Contact(
            user_id=user.id,
            name=name.strip(),
            email=email,
            company=company.strip(),
            phone=phone.strip(),
            list_name=list_name.strip() or "General",
            notes=notes.strip(),
        )
        db.add(contact)
        db.commit()
    finally:
        db.close()

    _set_flash(ud, f"Contact '{name.strip()}' added successfully.", "success")
    return _redirect("/contacts")


@app.post("/contacts/delete/{cid}")
async def contacts_delete(request: Request, cid: int):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(
            Contact.id == cid, Contact.user_id == user.id
        ).first()
        if contact:
            db.delete(contact)
            db.commit()
            _set_flash(ud, "Contact deleted.", "success")
        else:
            _set_flash(ud, "Contact not found.", "error")
    finally:
        db.close()

    return _redirect("/contacts")


@app.post("/contacts/import")
async def contacts_import(request: Request, file: UploadFile = File(...)):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    contents = await file.read()
    try:
        text_data = contents.decode("utf-8-sig")
    except Exception:
        text_data = contents.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text_data))
    added = 0
    skipped = 0
    db = SessionLocal()
    try:
        for row in reader:
            # Normalize header names (case-insensitive)
            row_lower = {k.strip().lower(): v for k, v in row.items()}
            email_val = (row_lower.get("email") or "").strip().lower()
            name_val = (row_lower.get("name") or row_lower.get("full name") or row_lower.get("fullname") or "").strip()
            if not email_val or not _EMAIL_RE.fullmatch(email_val):
                skipped += 1
                continue
            if not name_val:
                name_val = email_val.split("@")[0]
            contact = Contact(
                user_id=user.id,
                name=name_val,
                email=email_val,
                company=(row_lower.get("company") or "").strip(),
                phone=(row_lower.get("phone") or row_lower.get("phone number") or "").strip(),
                list_name=(row_lower.get("list") or row_lower.get("list_name") or "General").strip() or "General",
                notes=(row_lower.get("notes") or "").strip(),
            )
            db.add(contact)
            added += 1
        db.commit()
    except Exception as e:
        db.rollback()
        _set_flash(ud, f"Import failed: {e}", "error")
        return _redirect("/contacts")
    finally:
        db.close()

    _set_flash(ud, f"Imported {added} contact(s). {skipped} row(s) skipped.", "success")
    return _redirect("/contacts")


@app.get("/contacts/export")
async def contacts_export(request: Request):
    user, redir = _require_auth(request)
    if redir:
        return redir

    db = SessionLocal()
    try:
        contacts = db.query(Contact).filter(Contact.user_id == user.id).all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["name", "email", "company", "phone", "list_name", "notes"])
        for c in contacts:
            writer.writerow([c.name, c.email, c.company or "", c.phone or "", c.list_name or "General", c.notes or ""])
        csv_bytes = output.getvalue().encode("utf-8")
    finally:
        db.close()

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=contacts.csv"},
    )


# ── Scheduled Emails routes ───────────────────────────────────────────────────

@app.get("/scheduled")
async def scheduled_page(request: Request):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    flash = _consume_flash(ud)

    db = SessionLocal()
    try:
        pending = db.query(ScheduledEmail).filter(
            ScheduledEmail.user_id == user.id,
            ScheduledEmail.sent == False,
            ScheduledEmail.failed == False,
        ).order_by(ScheduledEmail.send_at.asc()).all()
        history = db.query(ScheduledEmail).filter(
            ScheduledEmail.user_id == user.id,
            (ScheduledEmail.sent == True) | (ScheduledEmail.failed == True),
        ).order_by(ScheduledEmail.send_at.desc()).limit(50).all()

        def _sched_dict(s):
            return {
                "id": s.id,
                "recipient": s.recipient,
                "subject": s.subject,
                "body": s.body,
                "send_at": s.send_at,
                "sent": s.sent,
                "failed": s.failed,
                "error_msg": s.error_msg or "",
                "created_at": s.created_at,
            }

        pending_data = [_sched_dict(s) for s in pending]
        history_data = [_sched_dict(s) for s in history]
    finally:
        db.close()

    return _render("scheduled.html", {
        "request": request,
        "user": user,
        "active": "scheduled",
        "flash": flash,
        "pending": pending_data,
        "history": history_data,
    })


@app.post("/scheduled/add")
async def scheduled_add(
    request: Request,
    recipient: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    send_at: str = Form(...),
):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    recipient = recipient.strip().lower()
    if not recipient or not _EMAIL_RE.fullmatch(recipient):
        _set_flash(ud, "Invalid recipient email address.", "error")
        return _redirect("/scheduled")

    try:
        send_at_dt = datetime.fromisoformat(send_at)
    except Exception:
        _set_flash(ud, "Invalid date/time format.", "error")
        return _redirect("/scheduled")

    if send_at_dt <= datetime.utcnow():
        _set_flash(ud, "Scheduled time must be in the future.", "error")
        return _redirect("/scheduled")

    db = SessionLocal()
    try:
        sched = ScheduledEmail(
            user_id=user.id,
            recipient=recipient,
            subject=subject.strip(),
            body=body.strip(),
            send_at=send_at_dt,
        )
        db.add(sched)
        db.commit()
    finally:
        db.close()

    _set_flash(ud, f"Email scheduled for {send_at_dt.strftime('%Y-%m-%d %H:%M')} UTC.", "success")
    return _redirect("/scheduled")


@app.post("/scheduled/cancel/{sid}")
async def scheduled_cancel(request: Request, sid: int):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    db = SessionLocal()
    try:
        sched = db.query(ScheduledEmail).filter(
            ScheduledEmail.id == sid,
            ScheduledEmail.user_id == user.id,
            ScheduledEmail.sent == False,
            ScheduledEmail.failed == False,
        ).first()
        if sched:
            db.delete(sched)
            db.commit()
            _set_flash(ud, "Scheduled email cancelled.", "success")
        else:
            _set_flash(ud, "Scheduled email not found or already sent.", "error")
    finally:
        db.close()

    return _redirect("/scheduled")


# ── Analytics route ───────────────────────────────────────────────────────────

@app.get("/analytics")
async def analytics_page(request: Request):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    flash = _consume_flash(ud)

    db = SessionLocal()
    try:
        campaigns = db.query(CampaignLog).filter(
            CampaignLog.user_id == user.id,
        ).order_by(CampaignLog.sent_at.desc()).all()

        total_campaigns = len(campaigns)
        total_sent = sum(c.success_count or 0 for c in campaigns)
        total_failed = sum(c.fail_count or 0 for c in campaigns)
        total_recipients = sum(c.recipient_count or 0 for c in campaigns)
        success_rate = round(total_sent / total_recipients * 100, 1) if total_recipients else 0.0

        campaigns_data = []
        for c in campaigns:
            rc = c.recipient_count or 1
            campaigns_data.append({
                "id": c.id,
                "subject": c.subject,
                "recipient_count": c.recipient_count or 0,
                "success_count": c.success_count or 0,
                "fail_count": c.fail_count or 0,
                "dry_run": c.dry_run,
                "sent_at": c.sent_at,
                "success_pct": round((c.success_count or 0) / rc * 100),
                "fail_pct": round((c.fail_count or 0) / rc * 100),
            })
    finally:
        db.close()

    return _render("analytics.html", {
        "request": request,
        "user": user,
        "active": "analytics",
        "flash": flash,
        "campaigns": campaigns_data,
        "total_campaigns": total_campaigns,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "success_rate": success_rate,
    })


# ── Sent Mail route ───────────────────────────────────────────────────────────

@app.get("/sent")
async def sent_page(request: Request):
    user, redir = _require_auth(request)
    if redir:
        return redir

    ud = _get_user_data(user.id)
    flash = _consume_flash(ud)

    db = SessionLocal()
    try:
        campaigns = db.query(CampaignLog).filter(
            CampaignLog.user_id == user.id,
        ).order_by(CampaignLog.sent_at.desc()).all()
        total_sent = sum(c.success_count or 0 for c in campaigns)
        campaigns_data = [
            {
                "id": c.id,
                "subject": c.subject,
                "recipient_count": c.recipient_count or 0,
                "success_count": c.success_count or 0,
                "fail_count": c.fail_count or 0,
                "dry_run": c.dry_run,
                "sent_at": c.sent_at,
            }
            for c in campaigns
        ]
    finally:
        db.close()

    return _render("sent.html", {
        "request": request,
        "user": user,
        "active": "sent",
        "flash": flash,
        "campaigns": campaigns_data,
        "total_sent": total_sent,
    })
