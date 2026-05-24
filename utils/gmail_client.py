"""
Gmail Draft Client for TUM Social AI outreach.

Creates email drafts in contact@tum-socialaiclub.de via Gmail API (OAuth).
The team reviews and sends them manually.

Auth: gmail_token.json must exist (run setup_gmail_auth.py once to generate it).
"""
import base64
import email as email_lib
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


def create_draft(
    to_email: str,
    subject: str,
    body: str,
    contact_name: str = "",
    company_name: str = "",
) -> Optional[str]:
    """
    Create a Gmail draft in contact@tum-socialaiclub.de.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        contact_name: Used for logging only.
        company_name: Used for logging only.

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
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{SENDER_DISPLAY} <{SENDER_ADDRESS}>"
        msg["To"] = to_email

        msg.attach(MIMEText(body, "plain", "utf-8"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}}
        ).execute()

        draft_id = draft.get("id", "")
        label = f"{contact_name} @ {company_name}" if contact_name and company_name else (contact_name or to_email)
        console.print(f"  [green]Gmail draft created: {label} → {to_email} (ID: {draft_id[:8]}...)[/green]")
        return draft_id

    except Exception as e:
        console.print(f"  [red]Gmail draft error for {contact_name}: {e}[/red]")
        return None
