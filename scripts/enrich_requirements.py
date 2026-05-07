#!/usr/bin/env python3
"""
Enrichment script for Social Project Applications (Tally) DB.

After a Tally form submission lands in the Requirements DB, this script:
1. Reads new entries (Application Status = "New" or empty)
2. Finds or creates the Account in the Accounts DB (by organization name, fuzzy)
3. Finds or creates the Product Owner in the Contacts DB
4. Links them via relations in the Requirements entry
5. Also links the Contact to the Account in the Contacts DB
6. Sets Application Status to "Under Review"

Property name mapping (actual DB schema as of April 2026):
  - Title:              "1.1 Organization Name"
  - PO Name:            "4.1.1 PO Name"         (rich_text)
  - PO Role:            "4.1.2 PO Role"          (rich_text)
  - PO Email:           "4.1.3 PO Email"         (email)
  - PO Phone:           "4.1.4 PO Phone Number"  (phone_number)
  - Status:             "Application Status"     (select)
  - Account relation:   "Account"                (relation -> Accounts DB)
  - PO relation:        "Product Owner"          (relation -> Contacts DB)

Usage:
    cd tum_sales_agent
    source venv/bin/activate
    python scripts/enrich_requirements.py          # process all new/unlinked entries
    python scripts/enrich_requirements.py --dry-run # preview without writing
    python scripts/enrich_requirements.py --all     # reprocess ALL entries, not just new
"""

import os
import sys
import re
import argparse
import requests
from dotenv import load_dotenv
from pathlib import Path
from typing import Optional, Dict, List, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
ACCOUNTS_DB_ID = os.getenv("NOTION_DB_ACCOUNTS_ID")
CONTACTS_DB_ID = os.getenv("NOTION_DB_CONTACTS_ID")
REQUIREMENTS_DB_ID = os.getenv("NOTION_DB_REQUIREMENTS_ID")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
API = "https://api.notion.com/v1"


# ── Helpers ──────────────────────────────────────────────────────────────

def normalize(name: str) -> str:
    """Normalize an org/contact name for fuzzy comparison.
    
    Strips legal suffixes, punctuation, extra whitespace, lowercases.
    """
    s = name.lower().strip()
    # Remove common legal suffixes
    for suffix in [
        " e.v.", " e. v.", " ev", " ggmbh", " gmbh", " ag",
        " inc", " ltd", " corp", " llc", " stiftung",
        " - zentrale", " zentrale",
    ]:
        s = s.replace(suffix, "")
    # Remove parenthetical content
    s = re.sub(r'\([^)]*\)', '', s)
    # Remove punctuation except spaces
    s = re.sub(r'[^a-z0-9äöüß\s]', '', s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def get_text(page: dict, prop: str) -> str:
    """Extract plain text from a Notion rich_text property."""
    p = page["properties"].get(prop, {})
    if p.get("type") == "rich_text":
        return "".join(rt["plain_text"] for rt in p.get("rich_text", []))
    if p.get("type") == "title":
        return "".join(rt["plain_text"] for rt in p.get("title", []))
    if p.get("type") == "email":
        return p.get("email") or ""
    if p.get("type") == "phone_number":
        return p.get("phone_number") or ""
    return ""


def get_select(page: dict, prop: str) -> str:
    """Extract select value."""
    p = page["properties"].get(prop, {})
    sel = p.get("select")
    return sel["name"] if sel else ""


def query_db_all(db_id: str, filter_obj: Optional[dict] = None) -> list:
    """Query a Notion database, paginating through all results."""
    results = []
    payload = {"page_size": 100}
    if filter_obj:
        payload["filter"] = filter_obj
    has_more = True
    while has_more:
        resp = requests.post(f"{API}/databases/{db_id}/query", headers=HEADERS, json=payload)
        if resp.status_code != 200:
            print(f"  ERROR querying DB {db_id}: {resp.status_code} - {resp.text[:200]}")
            break
        data = resp.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        if has_more:
            payload["start_cursor"] = data["next_cursor"]
    return results


def update_page(page_id: str, properties: dict) -> Optional[dict]:
    """Update a Notion page's properties."""
    resp = requests.patch(
        f"{API}/pages/{page_id}",
        headers=HEADERS,
        json={"properties": properties},
    )
    if resp.status_code != 200:
        print(f"  ERROR updating page {page_id}: {resp.status_code}: {resp.text[:300]}")
        return None
    return resp.json()


# ── Accounts lookup ──────────────────────────────────────────────────────

def load_all_accounts() -> Dict[str, dict]:
    """Load all accounts and return dict keyed by normalized name."""
    accounts = {}
    pages = query_db_all(ACCOUNTS_DB_ID)
    for page in pages:
        props = page["properties"]
        name = ""
        for pname, pdata in props.items():
            if pdata.get("type") == "title":
                titles = pdata.get("title", [])
                if titles:
                    name = titles[0].get("plain_text", "")
                break
        if name and name.strip():
            norm = normalize(name)
            if norm:  # Skip empty-after-normalization names
                accounts[norm] = {
                    "id": page["id"],
                    "name": name.strip(),
                }
    print(f"  Loaded {len(accounts)} accounts from Accounts DB")
    return accounts


def find_account_fuzzy(org_name: str, accounts: Dict[str, dict]) -> Optional[dict]:
    """Find an account by normalized name matching.
    
    Tries exact normalized match first, then checks if one name 
    contains the other (for cases like 'VENRO' vs full legal name).
    
    Guards against trivial matches (empty names, very short strings).
    """
    norm = normalize(org_name)
    if len(norm) < 3:
        return None  # Name too short for reliable matching
    
    # Exact normalized match
    if norm in accounts:
        return accounts[norm]
    
    # Containment match: check if either name contains the other
    # Only match if the shorter string is at least 4 chars (avoid 'e', 'ai' etc.)
    best_match = None
    for key, acct in accounts.items():
        if len(key) < 4:
            continue  # Skip trivially short account names
        shorter = min(len(norm), len(key))
        if shorter < 4:
            continue
        if norm in key or key in norm:
            # Prefer longer key (more specific match)
            if best_match is None or len(key) > len(best_match[0]):
                best_match = (key, acct)
    
    if best_match:
        return best_match[1]
    
    return None


def create_account(org_name: str) -> Optional[dict]:
    """Create a new Account in the Accounts DB."""
    payload = {
        "parent": {"database_id": ACCOUNTS_DB_ID},
        "properties": {
            "Organization*": {"title": [{"text": {"content": org_name}}]},
            "Account Type*": {"select": {"name": "NGO"}},
            "Status": {"status": {"name": "Prospect Qualified"}},
        },
    }
    resp = requests.post(f"{API}/pages", headers=HEADERS, json=payload)
    if resp.status_code != 200:
        print(f"  ERROR creating account: {resp.status_code}: {resp.text[:300]}")
        return None
    return resp.json()


# ── Contacts lookup ──────────────────────────────────────────────────────

def load_all_contacts() -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """Load all contacts and return dicts keyed by normalized name and email."""
    by_name = {}
    by_email = {}
    pages = query_db_all(CONTACTS_DB_ID)
    for page in pages:
        props = page["properties"]
        name = ""
        for pname, pdata in props.items():
            if pdata.get("type") == "title":
                titles = pdata.get("title", [])
                if titles:
                    name = titles[0].get("plain_text", "").strip()
                break
        email = ""
        ep = props.get("Email", {})
        if ep.get("email"):
            email = ep["email"].lower().strip()

        contact_data = {
            "id": page["id"],
            "name": name,
            "email": email,
            "properties": props,
        }
        if name:
            by_name[name.lower().strip()] = contact_data
        if email:
            by_email[email] = contact_data

    print(f"  Loaded {len(by_name)} contacts from Contacts DB")
    return by_name, by_email


def find_contact(po_name: str, po_email: str,
                 contacts_by_name: Dict[str, dict],
                 contacts_by_email: Dict[str, dict]) -> Optional[dict]:
    """Find a contact by email first, then by name."""
    if po_email:
        email_norm = po_email.lower().strip()
        if email_norm in contacts_by_email:
            return contacts_by_email[email_norm]
    if po_name:
        name_norm = po_name.lower().strip()
        if name_norm in contacts_by_name:
            return contacts_by_name[name_norm]
    return None


def create_contact(name: str, email: str, phone: str, role: str,
                   account_id: str = "") -> Optional[dict]:
    """Create a new Contact in the Contacts DB."""
    props = {
        "Contact Name": {"title": [{"text": {"content": name}}]},
    }
    if email:
        props["Email"] = {"email": email}
    if phone:
        props["Phone"] = {"phone_number": phone}
    if role:
        props["Job Title"] = {"rich_text": [{"text": {"content": role}}]}
    if account_id:
        props["Accounts"] = {"relation": [{"id": account_id}]}

    payload = {"parent": {"database_id": CONTACTS_DB_ID}, "properties": props}
    resp = requests.post(f"{API}/pages", headers=HEADERS, json=payload)
    if resp.status_code != 200:
        print(f"  ERROR creating contact: {resp.status_code}: {resp.text[:300]}")
        return None
    return resp.json()


# ── Processing ───────────────────────────────────────────────────────────

def process_entry(entry: dict,
                  accounts: Dict[str, dict],
                  contacts_by_name: Dict[str, dict],
                  contacts_by_email: Dict[str, dict],
                  dry_run: bool = False) -> dict:
    """Process a single Requirements form entry.
    
    Returns a summary dict with what was done.
    """
    # Extract fields using the ACTUAL DB property names
    org_name = get_text(entry, "1.1 Organization Name")
    po_name = get_text(entry, "4.1.1 PO Name")
    po_email = get_text(entry, "4.1.3 PO Email")
    po_phone = get_text(entry, "4.1.4 PO Phone Number")
    po_role = get_text(entry, "4.1.2 PO Role")
    app_status = get_select(entry, "Application Status")
    entry_id = entry["id"]

    # Check if already linked
    account_rel = entry["properties"].get("Account", {}).get("relation", [])
    po_rel = entry["properties"].get("Product Owner", {}).get("relation", [])
    has_account = bool(account_rel)
    has_po = bool(po_rel)

    result = {
        "org": org_name,
        "po": po_name,
        "status": app_status,
        "account_action": "already linked",
        "contact_action": "already linked",
        "linked": False,
    }

    print(f"\n{'─' * 60}")
    print(f"  Org: {org_name}")
    print(f"  PO:  {po_name} ({po_email})")
    print(f"  Application Status: {app_status or '(empty)'}")
    print(f"  Account linked: {'YES' if has_account else 'NO'}")
    print(f"  PO linked: {'YES' if has_po else 'NO'}")

    account_id = None
    contact_id = None

    # ── Step 1: Find or create Account ──
    if has_account:
        account_id = account_rel[0]["id"]
        print(f"  → Account already linked: {account_id[:12]}...")
    else:
        acct = find_account_fuzzy(org_name, accounts)
        if acct:
            account_id = acct["id"]
            result["account_action"] = f"found existing: {acct['name']}"
            print(f"  → Account FOUND: '{acct['name']}' ({account_id[:12]}...)")
        else:
            if dry_run:
                result["account_action"] = "would create"
                print(f"  → Account NOT FOUND — would CREATE: {org_name}")
            else:
                new_acct = create_account(org_name)
                if new_acct:
                    account_id = new_acct["id"]
                    # Add to cache for duplicate prevention within same run
                    accounts[normalize(org_name)] = {"id": account_id, "name": org_name}
                    result["account_action"] = "CREATED"
                    print(f"  → Account CREATED: {account_id[:12]}...")
                else:
                    result["account_action"] = "FAILED"
                    print(f"  → Account creation FAILED")

    # ── Step 2: Find or create Contact ──
    if has_po:
        contact_id = po_rel[0]["id"]
        print(f"  → Contact already linked: {contact_id[:12]}...")
    else:
        contact = find_contact(po_name, po_email, contacts_by_name, contacts_by_email)
        if contact:
            contact_id = contact["id"]
            result["contact_action"] = f"found existing: {contact['name']}"
            print(f"  → Contact FOUND: '{contact['name']}' ({contact_id[:12]}...)")

            # Update contact with missing info
            if not dry_run:
                updates = {}
                # Fill email if missing
                if po_email and not contact.get("email"):
                    updates["Email"] = {"email": po_email}
                # Fill phone if missing
                phone_prop = contact["properties"].get("Phone", {})
                if po_phone and not phone_prop.get("phone_number"):
                    updates["Phone"] = {"phone_number": po_phone}
                # Fill job title if missing
                jt_prop = contact["properties"].get("Job Title", {})
                jt_text = ""
                if jt_prop.get("rich_text"):
                    jt_text = jt_prop["rich_text"][0].get("plain_text", "")
                if po_role and not jt_text:
                    updates["Job Title"] = {"rich_text": [{"text": {"content": po_role}}]}
                # Link to account if not already
                if account_id:
                    existing_accts = contact["properties"].get("Accounts", {}).get("relation", [])
                    existing_ids = {a["id"] for a in existing_accts}
                    if account_id not in existing_ids:
                        updates["Accounts"] = {"relation": existing_accts + [{"id": account_id}]}
                if updates:
                    update_page(contact_id, updates)
                    print(f"  → Contact UPDATED with missing fields")
        else:
            if dry_run:
                result["contact_action"] = "would create"
                print(f"  → Contact NOT FOUND — would CREATE: {po_name}")
            else:
                new_contact = create_contact(po_name, po_email, po_phone, po_role, account_id or "")
                if new_contact:
                    contact_id = new_contact["id"]
                    # Add to cache
                    contacts_by_name[po_name.lower().strip()] = {
                        "id": contact_id, "name": po_name, "email": po_email, "properties": {}
                    }
                    if po_email:
                        contacts_by_email[po_email.lower().strip()] = contacts_by_name[po_name.lower().strip()]
                    result["contact_action"] = "CREATED"
                    print(f"  → Contact CREATED: {contact_id[:12]}...")
                else:
                    result["contact_action"] = "FAILED"
                    print(f"  → Contact creation FAILED")

    # ── Step 3: Link Account and Contact to the Requirements entry ──
    if dry_run:
        if not has_account and account_id:
            print(f"  → Would link Account to entry")
        if not has_po and contact_id:
            print(f"  → Would link Product Owner to entry")
        if not app_status:
            print(f"  → Would set Application Status → 'Under Review'")
        return result

    req_updates = {}
    if account_id and not has_account:
        req_updates["Account"] = {"relation": [{"id": account_id}]}
    if contact_id and not has_po:
        req_updates["Product Owner"] = {"relation": [{"id": contact_id}]}
    # Set status to "Under Review" if currently empty or "New"
    if not app_status or app_status == "New":
        req_updates["Application Status"] = {"select": {"name": "Under Review"}}

    if req_updates:
        update_page(entry_id, req_updates)
        result["linked"] = True
        print(f"  → Requirements entry UPDATED ✓")

    return result


def main():
    parser = argparse.ArgumentParser(description="Enrich Requirements DB entries")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--all", action="store_true",
                        help="Process ALL entries (not just new/unlinked)")
    args = parser.parse_args()

    if not all([NOTION_TOKEN, ACCOUNTS_DB_ID, CONTACTS_DB_ID, REQUIREMENTS_DB_ID]):
        print("Missing required environment variables:")
        for var, val in [
            ("NOTION_TOKEN", NOTION_TOKEN),
            ("NOTION_DB_ACCOUNTS_ID", ACCOUNTS_DB_ID),
            ("NOTION_DB_CONTACTS_ID", CONTACTS_DB_ID),
            ("NOTION_DB_REQUIREMENTS_ID", REQUIREMENTS_DB_ID),
        ]:
            print(f"  {var}: {'set' if val else 'MISSING'}")
        sys.exit(1)

    print("=" * 60)
    print("TUM Social AI — Requirements Enrichment")
    print("=" * 60)
    if args.dry_run:
        print("[DRY RUN MODE — no changes will be made]")
    print()

    # Load reference data
    print("Loading reference data...")
    accounts = load_all_accounts()
    contacts_by_name, contacts_by_email = load_all_contacts()

    # Fetch entries — either all or just those needing processing
    if args.all:
        entries = query_db_all(REQUIREMENTS_DB_ID)
        print(f"\nFetched ALL {len(entries)} entries.")
    else:
        # Get entries that are still "New", have no status, or are missing relations
        # We can't filter on empty relations via API, so fetch all and filter locally
        entries = query_db_all(REQUIREMENTS_DB_ID)
        unprocessed = []
        for e in entries:
            status = get_select(e, "Application Status")
            has_acct = bool(e["properties"].get("Account", {}).get("relation", []))
            has_po = bool(e["properties"].get("Product Owner", {}).get("relation", []))
            # Include if: no status, "New" status, or missing either relation
            if not status or status == "New" or not has_acct or not has_po:
                unprocessed.append(e)
        entries = unprocessed
        print(f"\nFound {len(entries)} entries needing processing (out of {len(query_db_all(REQUIREMENTS_DB_ID))} total).")

    if not entries:
        print("\nNo entries to process. Everything is up to date!")
        return

    # Process each entry
    summaries = []
    for entry in entries:
        summary = process_entry(entry, accounts, contacts_by_name, contacts_by_email, dry_run=args.dry_run)
        summaries.append(summary)

    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    created_accounts = sum(1 for s in summaries if s["account_action"] == "CREATED")
    found_accounts = sum(1 for s in summaries if s["account_action"].startswith("found"))
    created_contacts = sum(1 for s in summaries if s["contact_action"] == "CREATED")
    found_contacts = sum(1 for s in summaries if s["contact_action"].startswith("found"))
    linked = sum(1 for s in summaries if s["linked"])
    
    print(f"  Entries processed: {len(summaries)}")
    print(f"  Accounts: {created_accounts} created, {found_accounts} found existing")
    print(f"  Contacts: {created_contacts} created, {found_contacts} found existing")
    if not args.dry_run:
        print(f"  Entries updated: {linked}")
    
    print("\n" + "=" * 60)
    print("DONE!")
    print("=" * 60)


if __name__ == "__main__":
    main()
