# Auto Email Reply & Campaign Control Center

A full-featured email automation toolkit that lets you:

- Send up to thousands of templated emails in batches with automatic throttling.
- Toggle a Gmail-compatible auto-reply daemon that watches your inbox via IMAP.
- Drive everything from a modern FastAPI dashboard or the original CLI commands.

The project hides credentials behind configuration switches, supports environment overrides, and keeps logs so you can validate every run.

---

## Features

| Area | Highlights |
| --- | --- |
| Bulk Sender | Template placeholders using Python format strings, deduplicated recipients, batching (default 1000), optional delay between sends, and a dry-run preview. |
| Auto Reply | IMAP polling with configurable search criteria (e.g., `UNSEEN`, `ALL SINCE <date>`), duplicate suppression via `replied_ids.json`, and status callbacks for the dashboard. |
| Web App | FastAPI + Jinja2 UI with cards for sending campaigns and monitoring auto replies, live logs, flash messaging, and responsive Space Grotesk styling. |
| CLI | `python main.py send ...` for campaigns and `python main.py auto-reply` for the daemon. |
| Config | Central `config.py` with environment variable overrides for SMTP/IMAP details, delays, batch size, template paths, and auto reply behavior. |

---

## Project Structure

```
Auto-Email-Reply/
├─ app.py                 # FastAPI application (dashboard + API handlers)
├─ auto_reply.py          # AutoReplyService, IMAP polling, reply logic
├─ send_emails.py         # Bulk send helpers, template parsing, SMTP utilities
├─ main.py                # CLI entry point (send / auto-reply commands)
├─ config.py              # Project-wide configuration with env overrides
├─ tamplates/
│   ├─ send.txt           # Default outbound message template
│   └─ reply.txt          # Default auto-reply template
├─ templates_web/
│   └─ dashboard.html     # Web dashboard layout
├─ static/
│   └─ styles.css         # Dashboard styling
├─ recipients.txt         # Sample list for testing the sender
├─ replied_ids.json       # Tracks which Message-IDs already received a reply
└─ venv/                  # Python virtual environment (not required for deployment)
```

---

## Requirements

- Python 3.10+ (matches the bundled `venv`)
- Gmail account with an App Password (or any SMTP/IMAP provider with TLS)
- Dependencies (install inside your virtual environment):
  ```bash
  pip install fastapi uvicorn python-multipart jinja2
  ```

> Tip: `pip install -r requirements.txt` is recommended for production. You can export one via `pip freeze > requirements.txt` once satisfied.

---

## Configuration

All primary settings live in `config.py` but can be overridden with environment variables:

| Setting | Env Var | Description |
| --- | --- | --- |
| `EMAIL` | `EMAIL_ADDRESS` | Sender / IMAP login email |
| `PASSWORD` | `EMAIL_APP_PASSWORD` | App password or token |
| `SMTP_SERVER`, `SMTP_PORT` | `SMTP_SERVER`, `SMTP_PORT` | Outgoing server info (defaults to Gmail TLS) |
| `IMAP_SERVER` | `IMAP_SERVER` | Incoming server for auto-reply |
| `DELAY` | `SEND_DELAY` | Seconds to wait between messages |
| `BATCH_SIZE` | `BATCH_SIZE` | Max emails per batch |
| `AUTO_REPLY` | `AUTO_REPLY` | Toggle auto-reply service (True/False) |
| `AUTO_REPLY_POLL_INTERVAL` | `AUTO_REPLY_POLL_INTERVAL` | Seconds between inbox scans |
| `AUTO_REPLY_SEARCH_CRITERIA` | `AUTO_REPLY_SEARCH_CRITERIA` | IMAP search string (default `UNSEEN`) |
| `AUTO_REPLY_FETCH_LIMIT` | `AUTO_REPLY_FETCH_LIMIT` | Max messages to pull per cycle |
| `SEND_TEMPLATE`, `REPLY_TEMPLATE` | — | Paths for the text templates |

Populate a `.env` file or set variables in PowerShell, e.g.:
```powershell
$env:EMAIL_ADDRESS = "yourname@gmail.com"
$env:EMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
$env:AUTO_REPLY = "true"
```

> **Security:** Never commit real credentials. Use environment variables or a secret manager.

---

## Templates

Both templates reside under `tamplates/` (typo preserved to avoid breaking references):

- `send.txt` must begin with `Subject: ...` followed by the body. Placeholders like `{recipient}` are supported.
- `reply.txt` also starts with `Subject:` and can reference placeholders exposed by the auto-reply context:
  - `{original_subject}` – decoded subject line from the inbound email
  - `{sender_name}` – name extracted from the `From` header
  - `{sender_email}` – raw email address

Example `reply.txt` snippet:
```
Subject: Re: {original_subject}

Hello {sender_name},
Thank you for your email...
```

---

## CLI Usage

Activate the virtual environment first:
```powershell
venv\Scripts\Activate.ps1
```

### Send Campaign
```powershell
python main.py send recipients.txt --dry-run
```
Options:
- `--template PATH` to override `tamplates/send.txt`
- `--delay 1.5` to change the send interval
- `--batch-size 200` to throttle batches
- Remove `--dry-run` to actually send emails

### Auto Reply
```powershell
python main.py auto-reply
```
- Respects `AUTO_REPLY` in config.
- Press `Ctrl+C` to stop.
- Uses `replied_ids.json` to avoid replying multiple times to the same message ID. Delete the file if you need to reprocess.

---

## Web Dashboard

Launch the FastAPI app:
```powershell
uvicorn app:app --reload
```
Visit `http://127.0.0.1:8000/` to:

- **Send Campaign** – paste recipients, subject, body, tweak delay/batch size, and review log output.
- **Auto Reply Control** – start/stop the daemon and monitor its log entries.
- **Flash messages** – confirm actions, errors, or dry-run previews.

> The server stores logs in memory (lists capped at 200 entries) so restarts reset the history.

---

## Troubleshooting

| Issue | Fix |
| --- | --- |
| `Template must start with 'Subject:'` | Ensure your template files begin with `Subject:` and have at least one blank line before the body. |
| Auto-reply not detecting Gmail messages | Set `AUTO_REPLY_SEARCH_CRITERIA=ALL` or add filters like `UNSEEN SINCE 24-Jan-2026`. Also confirm the email actually lands in the INBOX and isn’t auto-archived. |
| Duplicate replies during testing | Delete `replied_ids.json` before re-running old test messages. |
| `jinja2 must be installed` when running `uvicorn` | Install the missing dependency: `pip install jinja2`. |
| `Header values may not contain linefeed...` | Already mitigated by `_sanitize_header_value`, but if you change templates ensure placeholder substitutions don’t inject `\n` into the subject. |

---

## Roadmap Ideas

- User authentication for the dashboard
- Persistent logs (database or files)
- Support for multiple SMTP profiles and per-campaign attachments
- Dockerfile / container images for easier deployment
- Admin view of reply statistics

Feel free to fork, tweak, and file issues in the GitHub repository.

## Contributing
Pull requests are welcome!

## Team
Built with collaboration.
