"""
Gmail Draft Client for TUM Social AI outreach.

Creates email drafts in contact@tum-socialaiclub.de via Gmail API (OAuth).
The team reviews and sends them manually.

Auth: gmail_token.json must exist (run setup_gmail_auth.py once to generate it).
"""
import base64
import email as email_lib
from email.utils import getaddresses
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()

BASE_DIR = Path(__file__).parent.parent
TOKEN_FILE = BASE_DIR / "gmail_token.json"
CREDS_FILE = next(BASE_DIR.glob("client_secret_*.json"), None)
SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

SENDER_ADDRESS = "partnerships@tum-socialaiclub.de"
SENDER_DISPLAY = "TUM Social AI Club"


def _get_gmail_service():
    """Return an authenticated Gmail API service, refreshing token if needed."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        console.print("[red]Gmail libraries not installed. Run setup_gmail_auth.py first.[/red]")
        return None

    if not TOKEN_FILE.exists():
        console.print("[red]gmail_token.json not found. Run setup_gmail_auth.py first.[/red]")
        return None

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        except Exception as e:
            console.print(f"[red]Failed to refresh Gmail token: {e}[/red]")
            return None

    try:
        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        console.print(f"[red]Failed to build Gmail service: {e}[/red]")
        return None


def _header_value(message: dict, name: str) -> str:
    """Return a Gmail message header value by case-insensitive name."""
    target = name.lower()
    for header in message.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == target:
            return header.get("value", "")
    return ""


def _payload_has_attachment(payload: dict) -> bool:
    """Return True if a Gmail payload contains attachment filenames."""
    if payload.get("filename"):
        return True
    return any(_payload_has_attachment(part) for part in payload.get("parts", []) or [])


def _draft_recipient_emails(message: dict) -> set[str]:
    """Extract normalized recipient emails from a draft message."""
    recipients = []
    for header_name in ("to", "cc", "bcc"):
        recipients.extend(getaddresses([_header_value(message, header_name)]))
    return {email.lower().strip() for _, email in recipients if email}


def _build_raw_message(to_email: str, subject: str, body: str) -> str:
    """Build a base64url encoded Gmail MIME message."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_DISPLAY} <{SENDER_ADDRESS}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


def find_existing_draft(service, to_email: str) -> Optional[str]:
    """
    Find the newest attachment-free draft from the partnerships alias to a recipient.

    This keeps forced copywriter reruns from creating duplicate Gmail drafts. We
    intentionally match by exact recipient email and sender alias only.
    """
    target = (to_email or "").lower().strip()
    if not target:
        return None

    page_token = None
    while True:
        params = {"userId": "me", "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        response = service.users().drafts().list(**params).execute()
        for draft in response.get("drafts", []):
            draft_id = draft.get("id", "")
            if not draft_id:
                continue
            full = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
            message = full.get("message", {})
            from_header = _header_value(message, "from").lower()
            if SENDER_ADDRESS.lower() not in from_header:
                continue
            if target not in _draft_recipient_emails(message):
                continue
            if _payload_has_attachment(message.get("payload", {})):
                continue
            return draft_id

        page_token = response.get("nextPageToken")
        if not page_token:
            return None


def create_draft(
    to_email: str,
    subject: str,
    body: str,
    contact_name: str = "",
    company_name: str = "",
    update_existing: bool = True,
) -> Optional[str]:
    """
    Create a Gmail draft in contact@tum-socialaiclub.de.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        contact_name: Used for logging only.
        company_name: Used for logging only.
        update_existing: If True, update an existing matching draft instead
            of creating a duplicate.

    Returns:
        Draft ID string if successful, None on failure.
    """
    service = _get_gmail_service()
    if not service:
        return None

    if not to_email or "@" not in to_email:
        console.print(f"  [yellow]Skipping Gmail draft — no valid email for {contact_name or 'contact'}[/yellow]")
        return None

    try:
        raw = _build_raw_message(to_email, subject, body)
        label = f"{contact_name} @ {company_name}" if contact_name and company_name else (contact_name or to_email)

        if update_existing:
            existing_draft_id = find_existing_draft(service, to_email)
            if existing_draft_id:
                draft = service.users().drafts().update(
                    userId="me",
                    id=existing_draft_id,
                    body={"id": existing_draft_id, "message": {"raw": raw}},
                ).execute()
                draft_id = draft.get("id", existing_draft_id)
                console.print(f"  [green]Gmail draft updated: {label} → {to_email} (ID: {draft_id[:8]}...)[/green]")
                return draft_id

        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}}
        ).execute()

        draft_id = draft.get("id", "")
        console.print(f"  [green]Gmail draft created: {label} → {to_email} (ID: {draft_id[:8]}...)[/green]")
        return draft_id

    except Exception as e:
        console.print(f"  [red]Gmail draft error for {contact_name}: {e}[/red]")
        return None
