"""
One-time Gmail OAuth setup for contact@tum-socialaiclub.de.

Run this once — it opens a browser, you log in, and the token is saved to
gmail_token.json for all future agent use.

Usage:
    python setup_gmail_auth.py
"""
import json
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
except ImportError:
    print("Installing required packages...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "google-auth-oauthlib", "google-auth-httplib2", "google-api-python-client"])
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

BASE_DIR = Path(__file__).parent
CREDS_FILE = next(BASE_DIR.glob("client_secret_*.json"), None)
TOKEN_FILE = BASE_DIR / "gmail_token.json"


def main():
    if not CREDS_FILE:
        print("ERROR: No client_secret_*.json found in this folder.")
        sys.exit(1)

    print(f"Using credentials: {CREDS_FILE.name}")

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        print(f"\nToken saved to: {TOKEN_FILE}")

    print("\nAuth successful! Testing access...")

    import googleapiclient.discovery
    service = googleapiclient.discovery.build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    print(f"Connected as: {profile.get('emailAddress')}")
    print(f"Total messages: {profile.get('messagesTotal')}")
    print("\nSetup complete. gmail_token.json is ready for the agent.")


if __name__ == "__main__":
    main()
