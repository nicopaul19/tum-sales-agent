"""
Assign partnership outreach drafts and Notion ownership by account.

The script balances Gmail outreach drafts across owners while keeping every
contact for the same account under one owner. It updates:
- Gmail draft body signatures and campaign copy
- Notion Account Owner / Campaign Sender
- Notion Contact Owner / Campaign Sender / draft copy for matched emails

Gmail labels require a token with Gmail label/modify scopes. The current
draft-only token can update drafts but cannot apply labels; use --labels-only
after re-authorizing Gmail with label scopes.
"""
from __future__ import annotations

import argparse
import csv
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

import requests
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.update_ai_for_good_hackathon_drafts import build_body, list_rewrites  # noqa: E402
from utils.config import DATA_DIR, NOTION_DB_ACCOUNTS_ID, NOTION_DB_CONTACTS_ID  # noqa: E402
from utils.gmail_client import _build_raw_message, _get_gmail_service  # noqa: E402
from utils.notion_client import _notion_api_headers  # noqa: E402


CURRENT_CAMPAIGN_OWNERS = [
    {
        "name": "Timon",
        "full_name": "Timon",
        "notion_id": "1fbd872b-594c-815e-a384-00029d08a0d3",
        "gmail_label": "Strategic Partnerships/Timon",
    },
    {
        "name": "Felix",
        "full_name": "Felix Laumann",
        "notion_id": "263d872b-594c-81d3-8ae5-0002c1fa4a8c",
        "gmail_label": "Strategic Partnerships/Felix",
    },
    {
        "name": "Till",
        "full_name": "Till",
        "notion_id": "260d872b-594c-8129-b8bc-00029a451251",
        "gmail_label": "Strategic Partnerships/Till",
    },
    {
        "name": "Nicolas",
        "full_name": "Nicolas Paul",
        "notion_id": "260d872b-594c-818a-ae96-0002584ae99a",
        "gmail_label": "Strategic Partnerships/Nicolas",
    },
]

FUTURE_OWNER_ROTATION = ["Timon", "Felix", "Till"]


@dataclass
class ContactPage:
    page_id: str
    name: str
    email: str
    account_ids: list[str]


@dataclass
class AccountPage:
    page_id: str
    name: str


def plain_text(prop: dict) -> str:
    if not prop:
        return ""
    ptype = prop.get("type", "")
    if ptype == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", []))
    if ptype == "email":
        return prop.get("email") or ""
    if ptype == "formula" and prop.get("formula", {}).get("type") == "string":
        return prop.get("formula", {}).get("string") or ""
    return ""


def query_database(database_id: str) -> list[dict]:
    results: list[dict] = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{database_id}/query",
            headers=_notion_api_headers(),
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        results.extend(payload.get("results", []))
        if not payload.get("has_more"):
            return results
        cursor = payload.get("next_cursor")


def load_notion_contacts() -> list[ContactPage]:
    contacts: list[ContactPage] = []
    for page in query_database(NOTION_DB_CONTACTS_ID):
        props = page.get("properties", {})
        account_relation = props.get("Accounts", {}).get("relation", []) or []
        email = plain_text(props.get("Email*", {})) or plain_text(props.get("Email", {}))
        contacts.append(
            ContactPage(
                page_id=page.get("id", ""),
                name=plain_text(props.get("Contact Name", {})).strip(),
                email=email.strip().lower(),
                account_ids=[item.get("id", "") for item in account_relation if item.get("id")],
            )
        )
    return contacts


def load_notion_accounts() -> list[AccountPage]:
    accounts: list[AccountPage] = []
    for page in query_database(NOTION_DB_ACCOUNTS_ID):
        props = page.get("properties", {})
        accounts.append(
            AccountPage(
                page_id=page.get("id", ""),
                name=plain_text(props.get("Organization*", {})).strip(),
            )
        )
    return accounts


def assign_by_account(rewrites: list, owners: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list] = defaultdict(list)
    for rewrite in rewrites:
        grouped[rewrite.company].append(rewrite)

    counts = Counter()
    owner_by_company: dict[str, dict] = {}
    for company, company_rewrites in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0].lower())):
        owner = min(owners, key=lambda candidate: (counts[candidate["name"]], candidate["name"]))
        owner_by_company[company] = owner
        counts[owner["name"]] += len(company_rewrites)
    return owner_by_company


def patch_notion_page(page_id: str, properties: dict) -> bool:
    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=_notion_api_headers(),
        json={"properties": properties},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Notion update failed {page_id[:8]}: {resp.status_code} {resp.text[:200]}")
        return False
    return True


def owner_people(owner: dict) -> dict:
    return {"people": [{"object": "user", "id": owner["notion_id"]}]}


def owner_sender(owner: dict) -> dict:
    return {"rich_text": [{"text": {"content": owner["full_name"]}}]}


def rich_text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": (value or "")[:2000]}}]}


def build_assignment_rows(rewrites: list, owner_by_company: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for rewrite in rewrites:
        owner = owner_by_company[rewrite.company]
        body = build_body(rewrite.greeting, rewrite.company, rewrite.angle, owner["full_name"])
        rows.append(
            {
                "draft_id": rewrite.draft_id,
                "message_to": rewrite.to_header,
                "company": rewrite.company,
                "angle": rewrite.angle,
                "subject": rewrite.new_subject,
                "body": body,
                "owner_name": owner["name"],
                "owner_full_name": owner["full_name"],
                "owner_notion_id": owner["notion_id"],
                "gmail_label": owner["gmail_label"],
            }
        )
    return rows


def write_report(rows: list[dict], apply: bool) -> Path:
    path = DATA_DIR / "reports" / f"partnership_outreach_owner_assignments_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{'live' if apply else 'dry_run'}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["empty"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def update_gmail_drafts(service, rows: list[dict], delay: float) -> int:
    updated = 0
    for row in rows:
        raw = _build_raw_message(row["message_to"], row["subject"], row["body"])
        service.users().drafts().update(
            userId="me",
            id=row["draft_id"],
            body={"id": row["draft_id"], "message": {"raw": raw}},
        ).execute()
        updated += 1
        time.sleep(delay)
    return updated


def update_notion(rows: list[dict]) -> tuple[int, int, int]:
    contacts = load_notion_contacts()
    accounts = load_notion_accounts()
    contacts_by_email = {contact.email: contact for contact in contacts if contact.email}
    contacts_by_account: dict[str, list[ContactPage]] = defaultdict(list)
    for contact in contacts:
        for account_id in contact.account_ids:
            contacts_by_account[account_id].append(contact)
    accounts_by_id = {account.page_id: account for account in accounts}

    assigned_account_ids: dict[str, dict] = {}
    matched_contacts: dict[str, dict] = {}
    rows_by_email = {}
    for row in rows:
        email = parseaddr(row["message_to"] or "")[1].strip().lower()
        rows_by_email[email] = row
        contact = contacts_by_email.get(email)
        if not contact:
            continue
        matched_contacts[contact.page_id] = row
        for account_id in contact.account_ids:
            assigned_account_ids[account_id] = row

    account_updates = 0
    contact_updates = 0
    missing_contacts = 0

    for row in rows:
        email = parseaddr(row["message_to"] or "")[1].strip().lower()
        if email not in contacts_by_email:
            missing_contacts += 1

    for account_id, row in assigned_account_ids.items():
        owner = {"full_name": row["owner_full_name"], "notion_id": row["owner_notion_id"]}
        if patch_notion_page(
            account_id,
            {
                "Account Owner": owner_people(owner),
                "Campaign Sender": owner_sender(owner),
            },
        ):
            account_updates += 1
        time.sleep(0.35)

        # Keep every contact under the account with the same owner, even when
        # multiple people at one company have outreach drafts.
        for contact in contacts_by_account.get(account_id, []):
            contact_row = rows_by_email.get(contact.email, row)
            owner = {"full_name": contact_row["owner_full_name"], "notion_id": contact_row["owner_notion_id"]}
            properties = {
                "Contact Owner*": owner_people(owner),
                "Campaign Sender": owner_sender(owner),
            }
            if contact.email in rows_by_email:
                properties["Cold Email Subject"] = rich_text(contact_row["subject"])
                properties["Cold Email Body"] = rich_text(contact_row["body"])
            if patch_notion_page(contact.page_id, properties):
                contact_updates += 1
            time.sleep(0.35)

    return account_updates, contact_updates, missing_contacts


def apply_gmail_labels(service, rows: list[dict]) -> int:
    # This intentionally fails fast with the current compose-only token.
    # Keeping the code path here makes the workflow repeatable after re-auth.
    label_response = service.users().labels().list(userId="me").execute()
    labels = {item["name"]: item["id"] for item in label_response.get("labels", [])}
    for row in rows:
        if row["gmail_label"] not in labels:
            created = service.users().labels().create(
                userId="me",
                body={
                    "name": row["gmail_label"],
                    "messageListVisibility": "show",
                    "labelListVisibility": "labelShow",
                },
            ).execute()
            labels[row["gmail_label"]] = created["id"]
    updated = 0
    peer_label_ids = sorted({labels[row["gmail_label"]] for row in rows if row["gmail_label"] in labels})
    for row in rows:
        full = service.users().drafts().get(userId="me", id=row["draft_id"], format="metadata").execute()
        message_id = full.get("message", {}).get("id")
        if not message_id:
            continue
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={
                "addLabelIds": [labels[row["gmail_label"]]],
                "removeLabelIds": [label_id for label_id in peer_label_ids if label_id != labels[row["gmail_label"]]],
            },
        ).execute()
        updated += 1
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Assign partnership outreach owners by account")
    parser.add_argument("--apply", action="store_true", help="Apply Gmail draft and Notion updates")
    parser.add_argument("--apply-gmail-labels", action="store_true", help="Also apply Gmail labels if token has label scopes")
    parser.add_argument("--labels-only", action="store_true", help="Only apply Gmail labels after OAuth has label scopes")
    parser.add_argument("--sleep", type=float, default=0.05, help="Gmail update delay")
    args = parser.parse_args()

    service = _get_gmail_service()
    if not service:
        raise RuntimeError("Could not initialize Gmail service")

    rewrites = list_rewrites(service)
    owner_by_company = assign_by_account(rewrites, CURRENT_CAMPAIGN_OWNERS)
    rows = build_assignment_rows(rewrites, owner_by_company)
    report = write_report(rows, args.apply)
    split = Counter(row["owner_name"] for row in rows)
    print("split", dict(split))
    print("future_owner_rotation", ", ".join(FUTURE_OWNER_ROTATION))
    print(f"report={report}")

    if args.labels_only:
        gmail_label_updates = apply_gmail_labels(service, rows)
        print(f"gmail_label_updates={gmail_label_updates}")
        return 0

    if not args.apply:
        return 0

    gmail_updated = update_gmail_drafts(service, rows, args.sleep)
    account_updates, contact_updates, missing_contacts = update_notion(rows)
    gmail_label_updates = 0
    if args.apply_gmail_labels:
        gmail_label_updates = apply_gmail_labels(service, rows)
    print(
        f"gmail_drafts_updated={gmail_updated} notion_accounts_updated={account_updates} "
        f"notion_contacts_updated={contact_updates} missing_draft_contacts_in_notion={missing_contacts} "
        f"gmail_label_updates={gmail_label_updates}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
