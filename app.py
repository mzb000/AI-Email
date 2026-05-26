from __future__ import annotations

from datetime import datetime
from email.utils import parseaddr
import json
from pathlib import Path
import re
from threading import Lock
from typing import Callable, List

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from starlette import status
from starlette.responses import Response

from auto_reply import AutoReplyService
from auth import get_current_user, hash_password, verify_password, create_token
from database import SessionLocal, User
from config import (
    AUTO_REPLY_POLL_INTERVAL,
    AUTO_REPLY_SEARCH_CRITERIA,
    BATCH_SIZE,
    DELAY,
)
from mailbox import get_mailbox_snapshot
from send_emails import EmailTemplate, send_bulk_emails

import json as _json

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates_web"
STATIC_DIR = BASE_DIR / "static"
DRAFT_STORE_PATH = BASE_DIR / "saved_emails.json"

app = FastAPI(title="Email Automation Control Center")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    auto_reload=True,
    cache_size=0,
)
_jinja_env.filters["tojson"] = lambda v: _json.dumps(v, ensure_ascii=False)


def _render(name: str, context: dict) -> HTMLResponse:
    tmpl = _jinja_env.get_template(name)
    return HTMLResponse(tmpl.render(**context))


# Per-user state: user_id -> dict
app.state.user_data: dict[int, dict] = {}
# Per-user AutoReplyService instances: user_id -> AutoReplyService
app.state.user_services: dict[int, AutoReplyService] = {}


def _get_user_data(user_id: int) -> dict:
    if user_id not in app.state.user_data:
        app.state.user_data[user_id] = {
            "send_log": [],
            "auto_reply_log": [],
            "flash": None,
            "saved_drafts": [],
            "draft_lock": Lock(),
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


# ── Draft helpers ────────────────────────────────────────────────────────────

def _load_saved_drafts() -> list[dict]:
    if not DRAFT_STORE_PATH.exists():
        return []
    try:
        data = json.loads(DRAFT_STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        cleaned: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            cleaned.append(
                {
                    "signature": str(item.get("signature") or ""),
                    "subject": str(item.get("subject") or ""),
                    "body": str(item.get("body") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                }
            )
        return cleaned[:30]
    return []


def _persist_saved_drafts(user_data: dict) -> None:
    try:
        DRAFT_STORE_PATH.write_text(
            json.dumps(user_data["saved_drafts"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _draft_signature(subject: str, body: str) -> str:
    return _normalize_text(subject)[:120] + "|" + _normalize_text(body)[:400]


def _save_draft(user_data: dict, subject: str, body: str) -> None:
    subject_value = (subject or "").strip()
    body_value = (body or "").strip()
    if not subject_value and not body_value:
        return

    signature = _draft_signature(subject_value, body_value)
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    with user_data["draft_lock"]:
        for item in user_data["saved_drafts"]:
            if item.get("signature") == signature:
                item.update({"subject": subject_value, "body": body_value, "updated_at": now_iso})
                _persist_saved_drafts(user_data)
                return

        user_data["saved_drafts"].insert(
            0,
            {"signature": signature, "subject": subject_value, "body": body_value, "updated_at": now_iso},
        )
        if len(user_data["saved_drafts"]) > 30:
            del user_data["saved_drafts"][30:]
        _persist_saved_drafts(user_data)


# ── Utility helpers ──────────────────────────────────────────────────────────

def _push_log(target: List[str], message: str, prefix: str | None = None) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {prefix + ' ' if prefix else ''}{message}"
    target.append(line)
    if len(target) > 200:
        del target[: len(target) - 200]


def _set_flash(user_data: dict, text: str, level: str = "info") -> None:
    user_data["flash"] = {"text": text, "level": level}


def _consume_flash(user_data: dict) -> dict | None:
    message = user_data.get("flash")
    user_data["flash"] = None
    return message


def _redirect_home() -> RedirectResponse:
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


_EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)


def _extract_recipients(raw_text: str) -> tuple[list[str], list[str]]:
    if not raw_text:
        return [], []

    separators_normalized = (
        raw_text.replace(";", ",")
        .replace("\t", ",")
        .replace("\r", "\n")
    )

    candidates: list[str] = []
    for line in separators_normalized.splitlines():
        for token in line.split(","):
            cleaned = token.strip()
            if cleaned:
                candidates.append(cleaned)

    valid: list[str] = []
    invalid: list[str] = []
    seen = set()
    for item in candidates:
        _, addr = parseaddr(item)
        addr = (addr or "").strip()
        if addr and _EMAIL_RE.fullmatch(addr):
            if addr not in seen:
                valid.append(addr)
                seen.add(addr)
        else:
            invalid.append(item)

    return valid, invalid


# ── Auth routes ──────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page(request: Request, error: str = ""):
    user = get_current_user(request)
    if user:
        return _redirect_home()
    return _render("login.html", {"request": request, "error": error})


@app.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email.strip().lower()).first()
    finally:
        db.close()

    if not user or not verify_password(password, user.password_hash):
        return _render("login.html", {"request": request, "error": "Invalid email or password."})

    token = create_token(user.id)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("token", token, httponly=True, max_age=60 * 60 * 24 * 30, samesite="lax")
    return response


@app.get("/signup")
async def signup_page(request: Request, error: str = ""):
    user = get_current_user(request)
    if user:
        return _redirect_home()
    return _render("signup.html", {"request": request, "error": error})


@app.post("/signup")
async def signup_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    name = name.strip()
    email = email.strip().lower()

    if not name or not email or not password:
        return _render("signup.html", {"request": request, "error": "All fields are required."})

    if not _EMAIL_RE.fullmatch(email):
        return _render("signup.html", {"request": request, "error": "Please enter a valid email address."})

    if len(password) < 8:
        return _render("signup.html", {"request": request, "error": "Password must be at least 8 characters."})

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            return _render("signup.html", {"request": request, "error": "An account with this email already exists."})

        new_user = User(
            name=name,
            email=email,
            password_hash=hash_password(password),
            gmail_address="",
            gmail_app_password="",
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        user_id = new_user.id
    finally:
        db.close()

    token = create_token(user_id)
    response = RedirectResponse("/settings?welcome=1", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("token", token, httponly=True, max_age=60 * 60 * 24 * 30, samesite="lax")
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("token")
    return response


@app.get("/settings")
async def settings_page(request: Request, welcome: str = ""):
    user = get_current_user(request)
    if not user:
        return _redirect_login()
    return _render(
        "settings.html",
        {
            "request": request,
            "user": user,
            "welcome": welcome == "1",
            "error": "",
            "success": "",
        },
    )


@app.post("/settings")
async def settings_submit(
    request: Request,
    gmail_address: str = Form(""),
    gmail_app_password: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return _redirect_login()

    gmail_address = gmail_address.strip()
    gmail_app_password = gmail_app_password.strip()

    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.id == user.id).first()
        if db_user:
            db_user.gmail_address = gmail_address
            db_user.gmail_app_password = gmail_app_password
            db.commit()
            db.refresh(db_user)
    finally:
        db.close()

    # Restart auto-reply service if it was running
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
            ud = _get_user_data(uid)

            def capture(message: str) -> None:
                _push_log(ud["auto_reply_log"], message, prefix="AutoReply")

            app.state.user_services[uid].start(status_callback=capture)

    return _render(
        "settings.html",
        {
            "request": request,
            "user": db_user if db_user else user,
            "welcome": False,
            "error": "",
            "success": "Settings saved successfully.",
        },
    )


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.get("/")
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect_login()

    ud = _get_user_data(user.id)
    svc = _get_user_service(user)

    flash = _consume_flash(ud)

    # Warn if no Gmail credentials configured
    if not user.gmail_address:
        if not flash:
            flash = {
                "text": "No Gmail credentials configured. Go to Settings to add your Gmail address and app password.",
                "level": "warning",
            }

    return _render(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "user_email": user.gmail_address or user.email,
            "default_delay": DELAY,
            "default_batch_size": BATCH_SIZE,
            "auto_reply_running": svc.is_running,
            "auto_reply_poll_interval": AUTO_REPLY_POLL_INTERVAL,
            "auto_reply_search": AUTO_REPLY_SEARCH_CRITERIA,
            "send_log": list(ud["send_log"]),
            "auto_reply_log": list(ud["auto_reply_log"]),
            "flash": flash,
            "mailbox": get_mailbox_snapshot(email=user.gmail_address or None, password=user.gmail_app_password or None),
            "saved_drafts": list(ud["saved_drafts"]),
        },
    )


# ── Draft routes ─────────────────────────────────────────────────────────────

@app.post("/draft/save")
async def save_draft(request: Request, subject: str = Form(""), body: str = Form("")):
    user = get_current_user(request)
    if not user:
        return {"ok": False}
    ud = _get_user_data(user.id)
    _save_draft(ud, subject, body)
    return {"ok": True}


@app.get("/draft/suggest")
async def suggest_draft(request: Request, q: str = ""):
    user = get_current_user(request)
    if not user:
        return {"items": []}

    ud = _get_user_data(user.id)
    query = _normalize_text(q)
    if not query:
        return {"items": []}

    def score_item(item: dict) -> float:
        subject = str(item.get("subject", ""))
        body = str(item.get("body", ""))
        haystack = _normalize_text(subject + " " + body)
        if not haystack:
            return 0.0

        score = 0.0
        if query in haystack:
            score += 2.0

        query_tokens = {t for t in query.split() if t}
        hay_tokens = {t for t in haystack.split() if t}
        if query_tokens and hay_tokens:
            overlap = len(query_tokens & hay_tokens) / max(1, len(query_tokens))
            score += overlap

        q_chars = query[:200]
        h_chars = haystack[:800]
        if q_chars and h_chars:
            score += (len(set(q_chars) & set(h_chars)) / max(1, len(set(q_chars)))) * 0.5

        return score

    with ud["draft_lock"]:
        scored = [(score_item(item), item) for item in ud["saved_drafts"]]

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results: list[dict] = []
    for score, item in scored:
        if score <= 0:
            continue
        results.append(
            {
                "subject": item.get("subject", ""),
                "body": item.get("body", ""),
                "updated_at": item.get("updated_at"),
                "score": score,
            }
        )
        if len(results) >= 5:
            break
    return {"items": results}


# ── Send route ───────────────────────────────────────────────────────────────

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
    user = get_current_user(request)
    if not user:
        return _redirect_login()

    ud = _get_user_data(user.id)

    if not user.gmail_address or not user.gmail_app_password:
        _set_flash(ud, "Please configure your Gmail credentials in Settings before sending.", "error")
        return _redirect_home()

    recipients: List[str] = []
    invalid: List[str] = []

    if recipients_text:
        valid_text, invalid_text = _extract_recipients(recipients_text)
        recipients.extend(valid_text)
        invalid.extend(invalid_text)

    seen = set()
    recipients = [r for r in recipients if not (r in seen or seen.add(r))]

    if not recipients:
        if invalid:
            sample = ", ".join(invalid[:6])
            more = "" if len(invalid) <= 6 else f" (+{len(invalid) - 6} more)"
            _set_flash(ud, f"No valid recipient emails found. Invalid entries: {sample}{more}", "error")
            return _redirect_home()
        _set_flash(ud, "Please provide at least one recipient email address.", "error")
        return _redirect_home()

    if invalid:
        sample = ", ".join(invalid[:6])
        more = "" if len(invalid) <= 6 else f" (+{len(invalid) - 6} more)"
        _set_flash(ud, f"Ignored invalid recipient entries: {sample}{more}", "info")

    try:
        subject_value = subject.strip()
        body_value = body.strip()
        template = EmailTemplate(subject=subject_value, body=body_value)

        delay_value = float(delay) if delay not in (None, "") else None
        batch_value = int(batch_size) if batch_size not in (None, "") else None
    except ValueError as exc:
        _set_flash(ud, f"Invalid numeric value: {exc}", "error")
        return _redirect_home()

    send_log: List[str] = []

    def capture(message: str) -> None:
        _push_log(send_log, message, prefix="Campaign")

    try:
        attachment_payload: list[tuple[str, bytes, str | None]] = []
        if attachments:
            for upload in attachments:
                if upload is None or not upload.filename:
                    continue
                data = await upload.read()
                attachment_payload.append((upload.filename, data, upload.content_type))

        send_bulk_emails(
            recipients,
            template=template,
            attachments=attachment_payload,
            delay=delay_value,
            batch_size=batch_value,
            dry_run=dry_run,
            status_callback=capture,
            email=user.gmail_address,
            password=user.gmail_app_password,
        )
        if dry_run:
            _set_flash(ud, "Dry-run completed successfully.", "success")
        else:
            _set_flash(ud, "Emails sent successfully.", "success")
            _save_draft(ud, subject_value, body_value)
    except Exception as exc:
        _push_log(send_log, f"Error: {exc}", prefix="Campaign")
        _set_flash(ud, f"Failed to send campaign: {exc}", "error")
    finally:
        ud["send_log"] = send_log

    if dry_run:
        return _redirect_home()
    return RedirectResponse("/?sent=1", status_code=status.HTTP_303_SEE_OTHER)


# ── Auto-reply routes ─────────────────────────────────────────────────────────

@app.post("/auto-reply/start")
async def start_auto_reply(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect_login()

    ud = _get_user_data(user.id)
    svc = _get_user_service(user)

    if not user.gmail_address or not user.gmail_app_password:
        _set_flash(ud, "Please configure your Gmail credentials in Settings before starting auto-reply.", "error")
        return _redirect_home()

    if svc.is_running:
        _set_flash(ud, "Auto-reply is already running.", "info")
        return _redirect_home()

    def capture(message: str) -> None:
        _push_log(ud["auto_reply_log"], message, prefix="AutoReply")

    svc.start(status_callback=capture)
    _set_flash(ud, "Auto-reply service started.", "success")
    return _redirect_home()


@app.post("/auto-reply/stop")
async def stop_auto_reply(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect_login()

    ud = _get_user_data(user.id)
    svc = _get_user_service(user)

    if not svc.is_running:
        _set_flash(ud, "Auto-reply is not running.", "info")
        return _redirect_home()

    svc.stop()
    _set_flash(ud, "Auto-reply service stopped.", "success")
    return _redirect_home()
