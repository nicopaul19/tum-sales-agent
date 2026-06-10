"""
One-off backfill: populate "Contact Status" for all existing Notion Contacts.

Heuristic
---------
- "Contacted" → linked account is at "Contacted LinkedIn 🌐" or "Contacted Mail 📩"
- "Engaged"   → linked account is at "Engaged" or further in the pipeline
                 AND this contact is the account's only contact in the Contacts DB
- "New"       → everything else (early-stage account, multiple contacts on an
                 engaged account, or no linked account)

Usage
-----
    cd tum_sales_agent
    source venv/bin/activate
    python -m agents.backfill_contact_status            # live run
    python -m agents.backfill_contact_status --dry-run  # preview only
"""
import sys
import time
import argparse
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import resilient_http as http_requests

from utils.config import NOTION_TOKEN, NOTION_DB_CONTACTS_ID, NOTION_DB_ACCOUNTS_ID
from utils.notion_client import _notion_api_headers
from agents.notion_cleanup import STATUS_HIERARCHY

console = Console()

NOTION_DELAY = 0.35  # seconds between write calls

# Account status indices that map to each Contact Status value
CONTACTED_STATUSES = {
    "Contacted LinkedIn 🌐",  # index 5
    "Contacted Mail 📩",       # index 6
}
ENGAGED_THRESHOLD = STATUS_HIERARCHY.index("Engaged")  # 7


def _paginate_db(database_id: str, headers: dict, extra_body: dict = None) -> list:
    """Fetch all pages from a Notion database, handling pagination."""
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    results = []
    has_more = True
    start_cursor = None

    while has_more:
        body = {"page_size": 100, **(extra_body or {})}
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = http_requests.post(url, headers=headers, json=body, timeout=60)
        if resp.status_code != 200:
            console.print(f"[red]Notion API error {resp.status_code}: {resp.json().get('message', '')}[/red]")
            return results

        data = resp.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return results


def _get_account_status(props: dict) -> str:
    """Extract status name from an Account page's properties."""
    status_prop = props.get("Status", {})
    if status_prop.get("type") == "status" and status_prop.get("status"):
        return status_prop["status"].get("name", "")
    return ""


def _get_contact_account_ids(props: dict) -> list[str]:
    """Extract linked Account page IDs from a Contact page's Accounts relation."""
    rel = props.get("Accounts", {})
    if rel.get("type") == "relation":
        return [r["id"] for r in rel.get("relation", []) if r.get("id")]
    return []


def determine_contact_status(
    account_status: str,
    contact_count_for_account: int,
) -> str:
    """Return the appropriate Contact Status option value."""
    if not account_status:
        return "New"

    if account_status in CONTACTED_STATUSES:
        return "Contacted"

    try:
        idx = STATUS_HIERARCHY.index(account_status)
    except ValueError:
        return "New"

    if idx >= ENGAGED_THRESHOLD and contact_count_for_account == 1:
        return "Engaged"

    return "New"


def run(dry_run: bool = False) -> None:
    if not NOTION_TOKEN:
        console.print("[red]NOTION_TOKEN not set[/red]")
        sys.exit(1)
    if not NOTION_DB_CONTACTS_ID or not NOTION_DB_ACCOUNTS_ID:
        console.print("[red]NOTION_DB_CONTACTS_ID or NOTION_DB_ACCOUNTS_ID not set[/red]")
        sys.exit(1)

    headers = _notion_api_headers()

    # ── 1. Fetch all accounts and build status lookup ───────────────────────
    console.print("[cyan]Fetching accounts...[/cyan]")
    account_pages = _paginate_db(NOTION_DB_ACCOUNTS_ID, headers)
    account_status_map: dict[str, str] = {}  # account_id → status name
    for page in account_pages:
        account_id = page.get("id", "")
        status = _get_account_status(page.get("properties", {}))
        if account_id:
            account_status_map[account_id] = status
    console.print(f"[green]Loaded {len(account_status_map)} accounts[/green]")

    # ── 2. Fetch all contacts ────────────────────────────────────────────────
    console.print("[cyan]Fetching contacts...[/cyan]")
    contact_pages = _paginate_db(NOTION_DB_CONTACTS_ID, headers)
    console.print(f"[green]Loaded {len(contact_pages)} contacts[/green]")

    # ── 3. Count contacts per account ────────────────────────────────────────
    contacts_per_account: dict[str, int] = defaultdict(int)
    contact_data: list[dict] = []

    for page in contact_pages:
        page_id = page.get("id", "")
        props = page.get("properties", {})
        account_ids = _get_contact_account_ids(props)

        # Use the first linked account (contacts can only be linked to one account realistically)
        primary_account_id = account_ids[0] if account_ids else ""
        if primary_account_id:
            contacts_per_account[primary_account_id] += 1

        # Extract name for logging
        contact_name = ""
        for pdata in props.values():
            if pdata.get("type") == "title":
                titles = pdata.get("title", [])
                if titles:
                    contact_name = titles[0].get("plain_text", "")
                break

        contact_data.append({
            "page_id": page_id,
            "name": contact_name,
            "account_id": primary_account_id,
        })

    # ── 4. Determine and apply Contact Status ────────────────────────────────
    summary = {"Contacted": 0, "Engaged": 0, "New": 0, "skipped": 0}

    table = Table(title="Contact Status Backfill Preview" if dry_run else "Contact Status Backfill")
    table.add_column("Contact", style="cyan", max_width=30)
    table.add_column("Account Status", style="yellow", max_width=28)
    table.add_column("# Contacts", justify="right")
    table.add_column("→ Contact Status", style="green")

    for contact in contact_data:
        page_id = contact["page_id"]
        account_id = contact["account_id"]
        account_status = account_status_map.get(account_id, "") if account_id else ""
        contact_count = contacts_per_account.get(account_id, 0) if account_id else 0

        new_status = determine_contact_status(account_status, contact_count)
        summary[new_status] = summary.get(new_status, 0) + 1

        table.add_row(
            contact["name"] or "(no name)",
            account_status or "(no account)",
            str(contact_count),
            new_status,
        )

        if dry_run:
            continue

        # Write to Notion with retries
        for attempt in range(3):
            try:
                resp = http_requests.patch(
                    f"https://api.notion.com/v1/pages/{page_id}",
                    headers=headers,
                    json={
                        "properties": {
                            "Contact Status": {
                                "select": {"name": new_status}
                            }
                        }
                    },
                    timeout=60,
                )
                if resp.status_code == 200:
                    break
                err = resp.json().get("message", resp.text)
                console.print(f"[yellow]  Attempt {attempt+1} failed for {contact['name']}: {resp.status_code} - {err}[/yellow]")
                time.sleep(2 ** attempt)
            except Exception as e:
                console.print(f"[yellow]  Attempt {attempt+1} error for {contact['name']}: {e}[/yellow]")
                time.sleep(2 ** attempt)
        else:
            console.print(f"[red]  Gave up on {contact['name']} after 3 attempts[/red]")
            summary["skipped"] += 1
            continue

        time.sleep(NOTION_DELAY)

    console.print(table)
    mode = "[yellow]DRY RUN — no changes written[/yellow]" if dry_run else "[green]All updates written[/green]"
    console.print(mode)
    console.print(
        f"Summary → Contacted: {summary['Contacted']} | "
        f"Engaged: {summary['Engaged']} | "
        f"New: {summary['New']}"
        + (f" | Errors: {summary['skipped']}" if summary["skipped"] else "")
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill Contact Status in Notion Contacts DB")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Notion")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
