from __future__ import annotations

import email
import json
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from threading import Event, Thread
from typing import Callable, Iterable, Set

import imaplib

from config import (
    AUTO_REPLY,
    AUTO_REPLY_FETCH_LIMIT,
    AUTO_REPLY_POLL_INTERVAL,
    AUTO_REPLY_SEARCH_CRITERIA,
    EMAIL,
    IMAP_SERVER,
    PASSWORD,
    REPLIED_LOG,
    REPLY_TEMPLATE,
)
from send_emails import build_message, load_template, smtp_connection


def _load_replied_ids(path: Path | str = REPLIED_LOG) -> Set[str]:
    file_path = Path(path)
    if not file_path.exists():
        return set()
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if isinstance(data, list):
        return {str(item) for item in data}
    return set()


def _save_replied_ids(ids: Iterable[str], path: Path | str = REPLIED_LOG) -> None:
    file_path = Path(path)
    file_path.write_text(json.dumps(sorted(set(ids))), encoding="utf-8")


def _decode_subject(raw_subject: str | None) -> str:
    if not raw_subject:
        return ""
    decoded = make_header(decode_header(raw_subject))
    return str(decoded)


def _fetch_target_mail(imap: imaplib.IMAP4_SSL) -> list[tuple[bytes, EmailMessage]]:
    imap.select("INBOX")
    criteria_tokens = [token for token in AUTO_REPLY_SEARCH_CRITERIA.split() if token]

    # Default strategy: search emails from the last 24 hours regardless of read/unread status.
    # This is more reliable than UNSEEN because viewing the inbox can mark emails as read.
    # Deduplication is handled by replied_ids.json so we won't double-reply.
    if not criteria_tokens or criteria_tokens == ["UNSEEN"]:
        since_date = (datetime.utcnow() - timedelta(hours=24)).strftime("%d-%b-%Y")
        criteria_tokens = ["SINCE", since_date]

    status, data = imap.search(None, *criteria_tokens)
    if status != "OK":
        return []
    email_ids = data[0].split()
    if AUTO_REPLY_FETCH_LIMIT > 0:
        email_ids = email_ids[:AUTO_REPLY_FETCH_LIMIT]
    messages: list[tuple[bytes, EmailMessage]] = []
    for email_id in email_ids:
        status, msg_data = imap.fetch(email_id, "(RFC822)")
        if status != "OK" or not msg_data:
            continue
        raw_message = msg_data[0][1]
        message = email.message_from_bytes(raw_message)
        messages.append((email_id, message))
    return messages


def _should_skip_sender(address: str | None, from_email: str | None = None) -> bool:
    if not address:
        return True
    sender_addr = address.strip().lower()
    check_addr = (from_email or EMAIL).strip().lower()
    return sender_addr == check_addr


def _send_reply(
    email_id: bytes,
    original_message: EmailMessage,
    template,
    replied_ids: Set[str],
    imap: imaplib.IMAP4_SSL,
    email: str | None = None,
    password: str | None = None,
) -> bool:
    message_id = original_message.get("Message-ID")
    if not message_id or message_id in replied_ids:
        return False

    sender_name, sender_email = parseaddr(original_message.get("From", ""))
    if _should_skip_sender(sender_email, from_email=email):
        return False

    context = {
        "original_subject": _decode_subject(original_message.get("Subject")),
        "sender_email": sender_email,
        "sender_name": sender_name or sender_email,
    }

    from_email_addr = email if email else EMAIL
    reply_message = build_message(sender_email, template, from_email=from_email_addr, **context)
    reply_message["In-Reply-To"] = original_message.get("Message-ID", "")
    reply_message["References"] = original_message.get("Message-ID", "")

    with smtp_connection(email=email, password=password) as smtp:
        smtp.send_message(reply_message)

    replied_ids.add(message_id)
    imap.store(email_id, "+FLAGS", "(\\Seen)")
    return True


def process_unread_messages(
    email: str | None = None,
    password: str | None = None,
) -> int:
    template = load_template(REPLY_TEMPLATE)
    replied_ids = _load_replied_ids()
    processed_count = 0

    actual_email = email if email else EMAIL
    actual_password = password if password else PASSWORD

    with imaplib.IMAP4_SSL(IMAP_SERVER) as imap:
        imap.login(actual_email, actual_password)
        for email_id, message in _fetch_target_mail(imap):
            if _send_reply(email_id, message, template, replied_ids, imap, email=email, password=password):
                processed_count += 1

    if processed_count:
        _save_replied_ids(replied_ids)
    return processed_count


class AutoReplyService:
    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        poll_interval: int | None = None,
    ) -> None:
        self.email = email
        self.password = password
        self.poll_interval = max(5, poll_interval or AUTO_REPLY_POLL_INTERVAL)
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._status_callback: Callable[[str], None] | None = None

    def _emit(self, message: str) -> None:
        if self._status_callback:
            self._status_callback(message)
        else:
            print(message)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, status_callback: Callable[[str], None] | None = None) -> bool:
        if self.is_running:
            return False

        self._status_callback = status_callback
        self._stop_event.clear()

        svc_email = self.email
        svc_password = self.password

        def _worker() -> None:
            self._emit("Auto-reply worker started.")
            while not self._stop_event.is_set():
                processed = process_unread_messages(email=svc_email, password=svc_password)
                if processed:
                    self._emit(f"Auto-replied to {processed} message(s).")
                else:
                    self._emit("No new emails detected.")
                self._stop_event.wait(self.poll_interval)
            self._emit("Auto-reply worker stopped.")

        self._thread = Thread(target=_worker, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 5)
        self._thread = None


def run_auto_reply() -> None:
    if not AUTO_REPLY:
        print("Auto-reply is disabled via configuration.")
        return

    service = AutoReplyService()
    print("Auto-reply service started. Press Ctrl+C to stop.")
    try:
        service.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Auto-reply service stopped by user.")
        service.stop()
