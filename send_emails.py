from __future__ import annotations

import smtplib
import time
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Sequence

from config import (
    BATCH_SIZE,
    DELAY,
    EMAIL,
    PASSWORD,
    SEND_TEMPLATE,
    SMTP_PORT,
    SMTP_SERVER,
)


@dataclass
class EmailTemplate:
    subject: str
    body: str


def load_template(path: Path | str | None = None) -> EmailTemplate:
    template_path = Path(path or SEND_TEMPLATE)
    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    raw_text = template_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        raise ValueError(f"Template file is empty: {template_path}")

    lines = [line.rstrip() for line in raw_text.splitlines()]
    if not lines or not lines[0].lower().startswith("subject:"):
        raise ValueError(
            "Template must start with a 'Subject: <text>' line followed by the body."
        )

    subject = lines[0].split(":", 1)[1].strip()
    body = "\n".join(lines[1:]).strip()
    return EmailTemplate(subject=subject, body=body)


def load_recipients_from_file(path: Path | str) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Recipient list not found: {file_path}")

    recipients: List[str] = []
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            recipients.append(line)

    if not recipients:
        raise ValueError("Recipient list is empty after filtering blanks and comments.")

    return recipients


def _normalize_recipients(recipients: Iterable[str]) -> list[str]:
    sanitized: List[str] = []
    seen = set()
    for address in recipients:
        cleaned = (address or "").strip()
        if not cleaned or cleaned in seen:
            continue
        sanitized.append(cleaned)
        seen.add(cleaned)
    return sanitized


def _chunked(items: Sequence[str], size: int) -> Iterator[list[str]]:
    if size <= 0:
        raise ValueError("Batch size must be greater than zero.")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _render_text(template_text: str, context: dict[str, str]) -> str:
    try:
        return template_text.format(**context)
    except KeyError as exc:  # pragma: no cover - defensive programming
        missing_key = exc.args[0]
        raise ValueError(f"Template placeholder '{{{missing_key}}}' is not provided.") from exc


def _sanitize_header_value(value: str) -> str:
    return " ".join((value or "").splitlines()).strip()


def build_message(
    recipient: str,
    template: EmailTemplate,
    from_email: str | None = None,
    attachments: list[tuple[str, bytes, str | None]] | None = None,
    **context: str,
) -> EmailMessage:
    payload = {"recipient": recipient, **context}
    subject = _sanitize_header_value(_render_text(template.subject, payload))
    body = _render_text(template.body or "", payload)

    message = EmailMessage()
    message["From"] = from_email if from_email else EMAIL
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    for filename, data, content_type in (attachments or []):
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    return message


@contextmanager
def smtp_connection(
    email: str | None = None,
    password: str | None = None,
) -> Iterator[smtplib.SMTP]:
    actual_email = email if email else EMAIL
    actual_password = password if password else PASSWORD

    # Port 465 → SMTP_SSL (implicit TLS), anything else → STARTTLS
    if int(SMTP_PORT) == 465:
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()

    try:
        server.login(actual_email, actual_password)
        yield server
    finally:
        try:  # pragma: no cover - best effort cleanup
            server.quit()
        except smtplib.SMTPException:
            server.close()


def _emit(status_callback: Callable[[str], None] | None, message: str) -> None:
    if status_callback is not None:
        status_callback(message)
    else:
        print(message)


def send_bulk_emails(
    recipients: Iterable[str],
    *,
    template_path: Path | str | None = None,
    template: EmailTemplate | None = None,
    delay: float | None = None,
    batch_size: int | None = None,
    dry_run: bool = False,
    status_callback: Callable[[str], None] | None = None,
    email: str | None = None,
    password: str | None = None,
    attachments: list[tuple[str, bytes, str | None]] | None = None,
) -> None:
    normalized = _normalize_recipients(recipients)
    if not normalized:
        raise ValueError("No valid recipient addresses provided.")

    template_obj = template or load_template(template_path)
    actual_delay = DELAY if delay is None else max(0, delay)
    actual_batch_size = BATCH_SIZE if batch_size is None else batch_size
    from_email = email if email else EMAIL

    if dry_run:
        _emit(status_callback, "[DRY-RUN] Previewing first 5 recipients (or fewer):")
        for preview_recipient in normalized[:5]:
            msg = build_message(preview_recipient, template_obj, from_email=from_email, attachments=attachments)
            _emit(status_callback, f"To: {preview_recipient} | Subject: {msg['Subject']}")
        _emit(status_callback, f"Total recipients queued: {len(normalized)}")
        return

    with smtp_connection(email=email, password=password) as server:
        for chunk_index, chunk in enumerate(_chunked(normalized, actual_batch_size), start=1):
            _emit(status_callback, f"Sending batch {chunk_index}: {len(chunk)} recipients...")
            for recipient in chunk:
                message = build_message(recipient, template_obj, from_email=from_email, attachments=attachments)
                server.send_message(message)
                if actual_delay:
                    time.sleep(actual_delay)
            _emit(status_callback, f"Batch {chunk_index} complete.")

    _emit(
        status_callback,
        f"Successfully sent {len(normalized)} emails in batches of {actual_batch_size}.",
    )
