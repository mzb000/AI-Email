from __future__ import annotations

import email
import json
from datetime import timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List

import imaplib

from config import EMAIL, IMAP_SERVER, MAILBOX_CACHE, MAILBOX_FETCH_LIMIT, PASSWORD


def _decode_header_value(raw_value: str | None) -> str:
    if not raw_value:
        return ""
    try:
        return str(make_header(decode_header(raw_value)))
    except Exception:  # pragma: no cover - defensive
        return raw_value


def _extract_body(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = part.get("Content-Disposition", "").lower()
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        # fallback to first part
        first_part = message.get_payload(0)
        if first_part:
            payload = first_part.get_payload(decode=True) or b""
            return payload.decode(first_part.get_content_charset() or "utf-8", errors="ignore")
    else:
        payload = message.get_payload(decode=True) or b""
        return payload.decode(message.get_content_charset() or "utf-8", errors="ignore")
    return ""


def _load_cached_messages() -> List[Dict[str, Any]]:
    if not MAILBOX_CACHE.exists():
        return []
    try:
        return json.loads(MAILBOX_CACHE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save_cached_messages(messages: List[Dict[str, Any]]) -> None:
    MAILBOX_CACHE.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")


def _derive_date_fields(raw_date: str | None) -> Dict[str, str]:
    defaults = {
        "date_iso": "",
        "date_short": "",
        "date_full": "",
    }

    if not raw_date:
        return defaults

    try:
        parsed = parsedate_to_datetime(raw_date)
        if parsed is None:
            return defaults
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local_dt = parsed.astimezone()
    except (TypeError, ValueError, OverflowError):
        return defaults

    defaults["date_iso"] = local_dt.isoformat()
    defaults["date_short"] = local_dt.strftime("%b %d · %H:%M %Z")
    defaults["date_full"] = local_dt.strftime("%A, %d %B %Y at %H:%M:%S %Z")
    return defaults


def fetch_recent_emails(limit: int = MAILBOX_FETCH_LIMIT, email: str | None = None, password: str | None = None) -> List[Dict[str, Any]]:
    """Fetch the newest emails from the INBOX for display and persist them."""
    _email = email or EMAIL
    _password = password or PASSWORD

    with imaplib.IMAP4_SSL(IMAP_SERVER) as imap:
        imap.login(_email, _password)
        imap.select("INBOX")
        status, data = imap.search(None, "ALL")
        if status != "OK":
            return []

        id_list = [msg_id for msg_id in data[0].split() if msg_id]
        if not id_list:
            return []

        recent_ids = id_list[-limit:]
        messages: List[Dict[str, Any]] = []

        for msg_id in reversed(recent_ids):  # newest first
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw_msg = msg_data[0][1]
            message = email.message_from_bytes(raw_msg)

            body = _extract_body(message).strip()

            raw_date = message.get("Date")
            decoded_date = _decode_header_value(raw_date)
            date_fields = _derive_date_fields(raw_date)

            messages.append(
                {
                    "id": msg_id.decode(errors="ignore"),
                    "subject": _decode_header_value(message.get("Subject")),
                    "from": _decode_header_value(message.get("From")),
                    "date": decoded_date,
                    "body": body,
                    **date_fields,
                }
            )

        _save_cached_messages(messages)
        return messages


def get_mailbox_snapshot(limit: int = MAILBOX_FETCH_LIMIT, email: str | None = None, password: str | None = None) -> List[Dict[str, Any]]:
    try:
        return fetch_recent_emails(limit, email=email, password=password)
    except Exception:  # pragma: no cover - fallback to cache if IMAP fails
        return _load_cached_messages()[:limit]
