"""
Upload Agent - Imports Apollo-enriched leads into Notion Accounts + Contacts DBs.

Takes a CSV file (enriched by Apollo with emails, titles, etc.) and:
1. Compares Apollo CSV against weekly_qualified_leads_no_contact.csv
2. Companies from the no-contact list that Apollo also couldn't find
   → saved to no_person_found_at_lead_account.csv (manual lookup)
3. Ensures new Notion properties exist (idempotent)
4. For each Apollo row: check if account exists → create or update; create contact
5. Log summary of uploads

Usage:
    python -m agents.upload_agent --csv data/tables/apollo_enriched.csv
"""
import sys
import argparse
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import pandas as pd
from rich.console import Console
from rich.table import Table

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import (
    NOTION_DB_ACCOUNTS_ID,
    NOTION_DB_CONTACTS_ID,
    DEFAULT_CAMPAIGN_SENDER,
    QUALIFIED_CSV,
    QUALIFIED_NO_CONTACT_CSV,
    NO_PERSON_CSV,
    TABLES_DIR
)
from utils.notion_client import (
    clean_value,
    get_existing_accounts_from_notion,
    get_existing_contacts_from_notion,
    is_status_engaged_or_above,
    create_contact_in_notion,
    ensure_notion_properties,
    create_account_in_notion,
    update_account_in_notion,
)
from utils.preflight import run_preflight

console = Console()


def resolve_campaign_sender(sender: str = "", interactive: bool = True) -> str:
    """Resolve the human sender for this campaign."""
    sender = (sender or "").strip() or (DEFAULT_CAMPAIGN_SENDER or "").strip()
    if sender:
        return sender
    if interactive:
        sender = console.input("[bold]Who will execute this campaign? Full sender name: [/bold]").strip()
        if sender:
            return sender
    raise ValueError("Campaign sender is required. Pass --sender \"Full Name\" or set DEFAULT_CAMPAIGN_SENDER in .env.")


def normalize_domain(domain: str) -> str:
    """Normalize a domain for comparison."""
    if not domain or not isinstance(domain, str) or domain == "nan":
        return ""
    domain = domain.lower().strip()
    if domain.startswith("http://"):
        domain = domain[7:]
    if domain.startswith("https://"):
        domain = domain[8:]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.rstrip("/")


def find_missing_companies(no_contact_df: pd.DataFrame, apollo_df: pd.DataFrame) -> pd.DataFrame:
    """
    Find companies from the no-contact list that Apollo also couldn't find.

    Matches by company_domain (primary) and company_name (fallback).

    Args:
        no_contact_df: Campaign shortlist leads WITHOUT contact
        apollo_df: Apollo-enriched DataFrame

    Returns:
        DataFrame of companies Apollo couldn't match either.
    """
    # Build lookup sets from Apollo CSV
    apollo_domains = set()
    apollo_names = set()

    for _, row in apollo_df.iterrows():
        domain = normalize_domain(str(row.get("company_domain", "") or row.get("Website", "") or ""))
        name = str(row.get("company_name", "") or row.get("Company Name", "") or "").lower().strip()
        if domain:
            apollo_domains.add(domain)
        if name:
            apollo_names.add(name)

    # Find no-contact leads still missing from Apollo
    missing = []
    for _, row in no_contact_df.iterrows():
        domain = normalize_domain(str(row.get("company_domain", "")))
        name = str(row.get("company_name", "")).lower().strip()

        in_apollo = False
        if domain and domain in apollo_domains:
            in_apollo = True
        elif name and name in apollo_names:
            in_apollo = True

        if not in_apollo:
            missing.append(row)

    return pd.DataFrame(missing) if missing else pd.DataFrame()


def save_no_person_found(missing_df: pd.DataFrame):
    """
    Append missing companies to the no_person_found CSV.

    Deduplicates by company_domain/company_name against existing entries.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    export_columns = ["date_added", "company_name", "company_domain",
                      "linkedin_url_post", "trigger", "score", "source"]

    # Prepare new rows
    new_rows = missing_df[[c for c in export_columns if c in missing_df.columns]].copy()
    new_rows["date_flagged"] = today

    # Load existing and deduplicate
    if NO_PERSON_CSV.exists():
        existing = pd.read_csv(NO_PERSON_CSV)

        existing_domains = set()
        existing_names = set()
        for _, row in existing.iterrows():
            d = normalize_domain(str(row.get("company_domain", "")))
            n = str(row.get("company_name", "")).lower().strip()
            if d:
                existing_domains.add(d)
            if n:
                existing_names.add(n)

        truly_new = []
        for _, row in new_rows.iterrows():
            d = normalize_domain(str(row.get("company_domain", "")))
            n = str(row.get("company_name", "")).lower().strip()
            if (d and d in existing_domains) or (n and n in existing_names):
                continue
            truly_new.append(row)

        if truly_new:
            new_df = pd.DataFrame(truly_new)
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined.to_csv(NO_PERSON_CSV, index=False)
            console.print(f"[yellow]Appended {len(truly_new)} new companies to {NO_PERSON_CSV.name}[/yellow]")
        else:
            console.print(f"[dim]No new companies to add to {NO_PERSON_CSV.name} (all already listed)[/dim]")
    else:
        new_rows.to_csv(NO_PERSON_CSV, index=False)
        console.print(f"[yellow]Created {NO_PERSON_CSV.name} with {len(new_rows)} companies[/yellow]")


def parse_apollo_row(row: pd.Series) -> dict:
    """
    Parse an Apollo CSV row into a normalized dict for Notion upload.

    Returns:
        Dict with keys matching the Notion field mapping.
    """
    first = clean_value(row.get("First Name"))
    last = clean_value(row.get("Last Name"))
    person_name = f"{first} {last}".strip()

    # Pick the best phone: Corporate Phone > Work Direct > Mobile > Home
    phone = ""
    for col in ["Corporate Phone", "Work Direct Phone", "Mobile Phone", "Home Phone"]:
        v = clean_value(row.get(col))
        if v:
            phone = v
            break

    # Parse funding amount
    funding_amount = row.get("Latest Funding Amount")
    if isinstance(funding_amount, float) and math.isnan(funding_amount):
        funding_amount = None

    # Parse employees
    employees = row.get("# Employees")
    if isinstance(employees, float) and math.isnan(employees):
        employees = None

    # Parse lead score
    lead_score = row.get("Lead Score")
    if isinstance(lead_score, float) and math.isnan(lead_score):
        lead_score = None

    return {
        # Account fields
        "company_name": clean_value(row.get("Company Name")) or "Unknown",
        "cleaned_name": clean_value(row.get("Company Name for Emails")),
        "website": clean_value(row.get("Website")),
        "company_linkedin": clean_value(row.get("Company Linkedin Url")),
        "city": clean_value(row.get("City")),
        "country": clean_value(row.get("Company Country")),
        "company_phone": clean_value(row.get("Company Phone")),
        "industry": clean_value(row.get("Industry")),
        "trigger": clean_value(row.get("Trigger")),
        "mission": clean_value(row.get("Mission (reasoning)")),
        "lead_score": lead_score,
        "employees": employees,
        "latest_funding": clean_value(row.get("Latest Funding")),
        "funding_amount": funding_amount,
        "apollo_account_id": clean_value(row.get("Apollo Account Id")),
        # Contact fields
        "person_name": person_name,
        "email": clean_value(row.get("Email")),
        "linkedin_url": clean_value(row.get("Person Linkedin Url")),
        "job_title": clean_value(row.get("Title")),
        "phone": phone,
        "apollo_contact_id": clean_value(row.get("Apollo Contact Id")),
    }


def group_by_company(rows: List[dict]) -> dict:
    """
    Group parsed Apollo rows by company (Apollo Account Id or domain fallback).

    Returns:
        Dict mapping group_key → list of parsed row dicts.
    """
    groups = defaultdict(list)

    for row in rows:
        key = row.get("apollo_account_id")
        if not key:
            key = normalize_domain(row.get("website", ""))
        if not key:
            key = row.get("company_name", "Unknown").lower().strip()
        groups[key].append(row)

    return dict(groups)


def find_existing_account(data: dict, accounts_lookup: dict) -> Optional[dict]:
    """
    Check if an account already exists in Notion.

    Looks up by domain first, then company name.

    Returns:
        Page data dict if found, None otherwise.
    """
    domain = normalize_domain(data.get("website", ""))
    if domain and domain in accounts_lookup["domains"]:
        return accounts_lookup["domains"][domain]

    name = data.get("company_name", "").lower().strip()
    if name and name in accounts_lookup["company_names"]:
        return accounts_lookup["company_names"][name]

    return None


def is_contact_duplicate(data: dict, contacts_lookup: dict) -> bool:
    """
    Check if a contact already exists in Notion.

    Priority: email → LinkedIn URL → name.
    """
    email = data.get("email", "").lower().strip()
    if email and "@" in email and email in contacts_lookup["emails"]:
        return True

    linkedin = data.get("linkedin_url", "").rstrip("/")
    if linkedin and linkedin in contacts_lookup["linkedin_urls"]:
        return True

    name = data.get("person_name", "").lower().strip()
    if name and name in contacts_lookup["names"]:
        return True

    return False


def run_upload(csv_path: str, sender: str = "", dry_run: bool = False, interactive: bool = True):
    """
    Upload Apollo-enriched leads to Notion.

    Args:
        csv_path: Path to the Apollo-enriched CSV file.
    """
    csv_file = Path(csv_path)
    if not csv_file.exists():
        console.print(f"[red]Error: CSV file not found: {csv_path}[/red]")
        return

    try:
        campaign_sender = resolve_campaign_sender(sender, interactive=interactive)
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        return

    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Sales Agent - Upload Agent[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    console.print(f"[cyan]Campaign sender: {campaign_sender}[/cyan]")
    if dry_run:
        console.print("[yellow]DRY RUN — no Notion writes[/yellow]")
    console.print("=" * 60)

    # Load Apollo CSV
    console.print(f"\n[cyan]Loading Apollo CSV: {csv_file.name}[/cyan]")
    apollo_df = pd.read_csv(csv_file)
    console.print(f"[cyan]Found {len(apollo_df)} rows in Apollo CSV[/cyan]")

    # --- Step 1: No-contact comparison (already implemented) ---
    missing = pd.DataFrame()
    no_contact_count = 0

    if QUALIFIED_NO_CONTACT_CSV.exists():
        no_contact_df = pd.read_csv(QUALIFIED_NO_CONTACT_CSV)
        no_contact_count = len(no_contact_df)
        console.print(f"[cyan]Found {no_contact_count} companies in no-contact qualified leads[/cyan]")

        if no_contact_count > 0:
            missing = find_missing_companies(no_contact_df, apollo_df)
            found_count = no_contact_count - len(missing)

            if not missing.empty:
                console.print(f"\n[yellow]Apollo couldn't find contacts for {len(missing)}/{no_contact_count} companies:[/yellow]")
                for _, row in missing.head(10).iterrows():
                    console.print(f"  - {row.get('company_name', '?')} ({row.get('company_domain', '?')})")
                if len(missing) > 10:
                    console.print(f"  ... and {len(missing) - 10} more")
                if dry_run:
                    console.print(f"[yellow]Dry run — would append {len(missing)} companies to {NO_PERSON_CSV.name}[/yellow]")
                else:
                    save_no_person_found(missing)
            else:
                console.print(f"[green]Apollo found contacts for all {no_contact_count} no-contact companies[/green]")

            if found_count > 0:
                console.print(f"[green]Apollo found contacts for {found_count} previously contactless companies[/green]")
    else:
        console.print(f"[dim]No {QUALIFIED_NO_CONTACT_CSV.name} found — skipping no-contact check[/dim]")

    # --- Step 2: Ensure new Notion properties exist ---
    console.print("\n[cyan]Step 2: Ensuring Notion properties exist...[/cyan]")
    if not NOTION_DB_ACCOUNTS_ID or not NOTION_DB_CONTACTS_ID:
        console.print("[red]Error: NOTION_DB_ACCOUNTS_ID and NOTION_DB_CONTACTS_ID must be set in .env[/red]")
        return
    if not dry_run:
        ensure_notion_properties(NOTION_DB_ACCOUNTS_ID, NOTION_DB_CONTACTS_ID)
    else:
        console.print("[dim]Dry run — skipping Notion schema patch[/dim]")

    # --- Step 2b: Preflight validation ---
    console.print("\n[cyan]Step 2b: Running preflight validation...[/cyan]")
    preflight = run_preflight(NOTION_DB_ACCOUNTS_ID, NOTION_DB_CONTACTS_ID)
    if not preflight.success:
        contact_only_errors = preflight.errors and all(
            "Contacts" in err or "Contacts DB" in err or "Failed to fetch Contacts" in err
            for err in preflight.errors
        )
        if contact_only_errors:
            console.print("[yellow]Contacts DB unavailable — continuing with Accounts DB contact fields only.[/yellow]")
        else:
            console.print("\n[bold red]Preflight found errors — properties listed above are missing from your Notion databases.[/bold red]")
            console.print("[yellow]Proceeding may cause API errors or missing data for those fields.[/yellow]")
            answer = "no"
            if interactive:
                answer = console.input("\n[bold]Continue anyway? (y/N): [/bold]").strip().lower()
            if answer not in ("y", "yes"):
                console.print("[red]Aborted.[/red]")
                return
            console.print("[yellow]Continuing with available mappings...[/yellow]\n")
    prop_map = preflight.prop_map
    status_map = preflight.status_map
    contacts_available = not any("Contacts" in err or "Failed to fetch Contacts" in err for err in preflight.errors)

    # --- Step 3: Fetch existing Notion data ---
    console.print("\n[cyan]Step 3: Fetching existing Notion data...[/cyan]")
    accounts_lookup = get_existing_accounts_from_notion(NOTION_DB_ACCOUNTS_ID, prop_map=prop_map)
    contacts_lookup = get_existing_contacts_from_notion(NOTION_DB_CONTACTS_ID) if contacts_available else {
        "emails": set(), "linkedin_urls": set(), "names": set()
    }

    # --- Step 4: Parse and group Apollo rows by company ---
    console.print("\n[cyan]Step 4: Parsing Apollo rows...[/cyan]")
    parsed_rows = [parse_apollo_row(row) for _, row in apollo_df.iterrows()]
    for row in parsed_rows:
        row["campaign_sender"] = campaign_sender
    company_groups = group_by_company(parsed_rows)
    console.print(f"[cyan]Grouped into {len(company_groups)} companies[/cyan]")

    # Campaign ID = "Workflow_DDMM"
    campaign_id = f"Workflow_{datetime.now().strftime('%d%m')}"
    console.print(f"[cyan]Campaign ID: {campaign_id}[/cyan]")

    # --- Step 5 & 6: Process each company group ---
    console.print("\n[cyan]Step 5-6: Uploading to Notion...[/cyan]")

    # Counters
    accounts_created = 0
    accounts_updated = 0
    accounts_skipped = 0
    contacts_created = 0
    contacts_skipped = 0
    errors = 0

    for group_key, rows in company_groups.items():
        # Use first row as the representative for account data
        account_data = rows[0]
        company_name = account_data.get("company_name", "Unknown")

        console.print(f"\n[bold]{company_name}[/bold] ({len(rows)} contact(s))")

        # Check if account exists
        existing = find_existing_account(account_data, accounts_lookup)

        if dry_run:
            console.print("  [yellow]Dry run — would create/update account and store sender/contact fields[/yellow]")
            accounts_skipped += 1
            continue

        if existing:
            page_id = existing["page_id"]
            current_status = existing.get("status", "")

            if is_status_engaged_or_above(current_status, status_map=status_map):
                # Engaged or above — fill empty fields only, no status change
                console.print(f"  [dim]Account exists (status: {current_status}) — filling empty fields only[/dim]")
                ok = update_account_in_notion(page_id, account_data, reset_status=False, campaign_id=campaign_id, prop_map=prop_map, status_map=status_map)
                if ok:
                    accounts_updated += 1
                else:
                    errors += 1
            else:
                # Below Engaged — reset to Prospect Qualified + fill empty fields
                console.print(f"  [yellow]Account exists (status: {current_status}) — resetting to Prospect Qualified[/yellow]")
                ok = update_account_in_notion(page_id, account_data, reset_status=True, campaign_id=campaign_id, prop_map=prop_map, status_map=status_map)
                if ok:
                    accounts_updated += 1
                else:
                    errors += 1
        else:
            # Create new account
            page_id = create_account_in_notion(account_data, campaign_id, NOTION_DB_ACCOUNTS_ID, prop_map=prop_map, status_map=status_map)
            if page_id:
                accounts_created += 1
                # Add to lookup so subsequent rows in same batch find it
                domain = normalize_domain(account_data.get("website", ""))
                page_data = {
                    "page_id": page_id,
                    "company_name": company_name,
                    "domain": domain,
                    "status": "Prospect Qualified",
                }
                if domain:
                    accounts_lookup["domains"][domain] = page_data
                accounts_lookup["company_names"][company_name.lower().strip()] = page_data
            else:
                errors += 1
                page_id = ""

        # Process contacts for this company
        if not contacts_available:
            console.print("  [yellow]Contacts DB unavailable — contact saved on Account fallback fields[/yellow]")
            contacts_skipped += len(rows)
            continue

        for contact_data in rows:
            person_name = contact_data.get("person_name", "").strip()
            if not person_name:
                console.print("  [dim]Skipping row with no contact name[/dim]")
                contacts_skipped += 1
                continue

            if is_contact_duplicate(contact_data, contacts_lookup):
                console.print(f"  [dim]Contact already exists: {person_name}[/dim]")
                contacts_skipped += 1
                continue

            ok = create_contact_in_notion(
                person_name=person_name,
                linkedin_url=contact_data.get("linkedin_url", ""),
                email=contact_data.get("email", ""),
                account_page_id=page_id,
                job_title=contact_data.get("job_title", ""),
                phone=contact_data.get("phone", ""),
                apollo_contact_id=contact_data.get("apollo_contact_id", ""),
                campaign_sender=campaign_sender,
                database_id=NOTION_DB_CONTACTS_ID,
                prop_map=prop_map,
            )

            if ok:
                contacts_created += 1
                # Add to lookup to prevent duplicates within the same batch
                email = contact_data.get("email", "").lower().strip()
                if email and "@" in email:
                    contacts_lookup["emails"].add(email)
                linkedin = contact_data.get("linkedin_url", "").rstrip("/")
                if linkedin:
                    contacts_lookup["linkedin_urls"].add(linkedin)
                if person_name:
                    contacts_lookup["names"].add(person_name.lower())
            else:
                errors += 1

    # --- Step 7: Summary ---
    console.print("\n" + "=" * 40)
    summary = Table(title="Upload Summary")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="green")
    summary.add_row("Apollo rows", str(len(apollo_df)))
    summary.add_row("Companies (groups)", str(len(company_groups)))
    summary.add_row("No-contact leads checked", str(no_contact_count))
    summary.add_row("Still missing (manual)", str(len(missing)) if not missing.empty else "0")
    summary.add_row("", "")
    summary.add_row("Accounts created", str(accounts_created))
    summary.add_row("Accounts updated", str(accounts_updated))
    summary.add_row("Accounts skipped", str(accounts_skipped))
    summary.add_row("", "")
    summary.add_row("Contacts created", str(contacts_created))
    summary.add_row("Contacts skipped", str(contacts_skipped))
    summary.add_row("", "")
    summary.add_row("Errors", str(errors))
    summary.add_row("Campaign ID", campaign_id)
    summary.add_row("Campaign sender", campaign_sender)
    console.print(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload Apollo-enriched leads to Notion")
    parser.add_argument("--csv", required=True, help="Path to the Apollo-enriched CSV file")
    parser.add_argument("--sender", default="", help="Full name of the teammate executing this campaign")
    parser.add_argument("--dry-run", action="store_true", help="Preview upload without writing to Notion")
    parser.add_argument("--no-input", action="store_true", help="Fail instead of prompting for missing sender/confirmations")
    args = parser.parse_args()

    run_upload(args.csv, sender=args.sender, dry_run=args.dry_run, interactive=not args.no_input)
