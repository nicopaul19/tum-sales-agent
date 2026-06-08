"""
NGO Email Writer — Applies fixed outreach email templates to all accounts in
the NGO_180526_InvoiceManagement campaign.

No GPT-4o. Pure template fill from Notion account fields.

Splits accounts 50/50 between two owners (Carlo Renner / Lisa Gavrilova),
sets Owner* (people), Campaign Sender, and generates the correct email body
and subject for each account in the right language.

Templates:
  German  → for accounts with Country in DACH (Germany, Austria, Switzerland, Liechtenstein)
  English → for all others

Fills:
  - Cold Email Body        (rich_text)
  - Cold Email Subject Text (rich_text)
  - Campaign Sender        (rich_text)
  - Owner*                 (people)

Usage:
    python -m agents.ngo_email_writer              # split + write all 40 accounts
    python -m agents.ngo_email_writer --dry-run    # preview without writing
    python -m agents.ngo_email_writer --force      # overwrite existing emails too
"""

import sys
import random
import argparse
from datetime import datetime
from pathlib import Path

import requests as http_requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import NOTION_TOKEN, NOTION_DB_ACCOUNTS_ID
from utils.notion_client import NOTION_API_VERSION
from utils.campaign_tracker import load_campaign_guidance, sync_campaign_tracker

console = Console()

CAMPAIGN_ID = "NGO_180526_InvoiceManagement"

# Owners — full names used in email body, Notion IDs for Owner* field
OWNERS = [
    {
        "full_name": "Carlo Renner",
        "first_name": "Carlo",
        "notion_id": "2c5d872b-594c-81ca-abfc-00023d45afd3",
    },
    {
        "full_name": "Lisa Gavrilova",
        "first_name": "Lisa",
        "notion_id": "328d872b-594c-8133-8a1f-00020ea856a1",
    },
]

DACH_COUNTRIES = {
    "germany", "austria", "switzerland", "liechtenstein",
    "deutschland", "österreich", "schweiz",
    "de", "at", "ch",
}

# Work area translations (EN → DE) for the German template
WORK_AREA_DE = {
    "Poverty Alleviation": "Armutsbekämpfung",
    "Health": "Gesundheits",
    "Education and Training": "Bildungs",
    "Education": "Bildungs",
    "Environment and Climate": "Umwelt- und Klimaschutz",
    "Environment": "Umweltschutz",
    "Humanitarian Aid": "humanitäre Hilfe",
    "Disaster Relief": "Katastrophenhilfe",
    "Development": "Entwicklungs",
    "Housing": "Wohnungsbau",
    "Water and Sanitation": "WASH",
    "WASH": "WASH",
    "Other Societal Benefits": "gesellschaftliche",
    "Specific Diseases": "Gesundheits",
    "Agriculture": "Landwirtschafts",
    "Animal Welfare": "Tierschutz",
    "Conservation": "Naturschutz",
}

# ---------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------

GERMAN_BODY = """\
Hallo {salutation},

ich hoffe, es geht Ihnen gut. Mein Name ist {sender}, Mitgründer Deutschlands erster AI-for-Good Studentenorganisation an der Technischen Universität München, TUM Social AI. Wir entwickeln derzeit ein pro-bono KI-Tool zur Rechnungsprüfung und -verwaltung mit einer unserer Nonprofit-Partnerorganisationen und wollten fragen, ob {ngo_name} ebenfalls viel Zeit damit verbringt, monatlich Hunderte oder sogar Tausende Rechnungen für Ihre {work_area} Arbeit zu verwalten.

Um unsere Modelle weiterzuentwickeln, suchen wir weitere Nonprofits, die das KI-Automatisierungstool mit uns testen und verfeinern möchten. Sobald das Tool ausgereift ist, werden wir Ihnen dieses kostenfrei zur Verfügung stellen. Unser Verein macht ausschließlich pro-bono Arbeit.

Gerne können wir nächste Woche einen kurzen Austausch einrichten. Sollte dieser Bereich nicht in Ihrer Verantwortung liegen, können Sie diese Email an den oder die Verantwortliche/n für Ihre Buchhaltungsabteilung weiterleiten.

Ich freue mich auf Ihre Rückmeldung!

Beste Grüße,
{sender}\
"""

ENGLISH_BODY = """\
Hi {salutation},

I hope you're doing well. My name is {sender}, co-founder of Germany's first AI-for-Good student organization at the Technical University of Munich, TUM Social AI. We are currently developing a pro-bono AI tool for invoice inspection and management with one of our nonprofit partner organizations, and wanted to ask whether {ngo_name} also spends significant time each month managing hundreds or even thousands of invoices for your {work_area} work.

To further develop our models, we are looking for additional nonprofits willing to test and refine the AI automation tool with us. Once the tool is mature, we will provide it to you free of charge. Our organization does exclusively pro-bono work.

We'd love to set up a short exchange next week. If this area is not within your responsibility, feel free to forward this email to the person responsible for your accounting department.

Looking forward to hearing from you!

Best regards,
{sender}\
"""

GERMAN_BODY_B = """\
Hallo {salutation},

mein Name ist {sender}, Mitgründer von TUM Social AI, Deutschlands erster AI-for-Good Studentenorganisation an der Technischen Universität München. Wir pilotieren mit Entreculturas ein pro-bono KI-Tool, das Rechnungsprüfung und Rechnungsverwaltung für Nonprofits stark vereinfacht.

Wäre das für {ngo_name} relevant, falls Sie für Ihre {work_area} Arbeit regelmäßig Rechnungen von lokalen Partnerorganisationen prüfen müssen? Wir suchen weitere Nonprofits, die das Tool mit uns testen. Sobald es ausgereift ist, stellen wir es kostenfrei bereit.

Wäre nächste Woche ein kurzer Austausch relevant?

Beste Grüße,
{sender}\
"""

ENGLISH_BODY_B = """\
Hi {salutation},

My name is {sender}, co-founder of TUM Social AI, Germany's first AI-for-Good student organization at the Technical University of Munich. We are piloting a pro-bono AI invoice inspection and management tool with Entreculturas.

Would this be relevant for {ngo_name} if your {work_area} work involves regularly reviewing invoices from local partner organizations? We are looking for more nonprofits to test the tool with us. Once mature, we will provide it free of charge.

Would a short exchange next week be relevant?

Best regards,
{sender}\
"""

GERMAN_SUBJECT = "{ngo_name} || TUM Social AI: KI-Rechnungsprüfung"
ENGLISH_SUBJECT = "{ngo_name} || TUM Social AI: AI Invoice Inspection"
GERMAN_SUBJECT_B = "{ngo_name} x TUM Social AI"
ENGLISH_SUBJECT_B = "{ngo_name} x TUM Social AI"


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def _text(prop: dict) -> str:
    t = prop.get("type", "")
    if t == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title", []))
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", []))
    if t == "select" and prop.get("select"):
        return prop["select"].get("name", "")
    if t == "multi_select":
        vals = [ms.get("name", "") for ms in prop.get("multi_select", [])]
        return vals[0] if vals else ""
    if t == "status" and prop.get("status"):
        return prop["status"].get("name", "")
    if t == "formula":
        return prop.get("formula", {}).get("string", "") or ""
    return ""


def fetch_campaign_accounts() -> list[dict]:
    """Fetch all Accounts tagged with CAMPAIGN_ID from Notion."""
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ACCOUNTS_ID}/query"
    body = {
        "page_size": 100,
        "filter": {
            "property": "Campaign ID",
            "multi_select": {"contains": CAMPAIGN_ID},
        },
    }

    accounts = []
    has_more = True
    start_cursor = None

    while has_more:
        if start_cursor:
            body["start_cursor"] = start_cursor
        resp = http_requests.post(url, headers=_headers(), json=body)
        if resp.status_code != 200:
            console.print(f"[red]Notion error: {resp.status_code} — {resp.json().get('message','')}[/red]")
            return []
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            accounts.append({
                "page_id": page["id"],
                "name": _text(props.get("Organization*", {})),
                "person_name": _text(props.get("[Suspect] Contact Name", {})).strip(),
                "country": _text(props.get("Country", {})).lower().strip(),
                "work_area": _text(props.get("Work Area NGO", {})).strip(),
                "existing_email_body": _text(props.get("Cold Email Body", {})).strip(),
                "existing_email_subject": _text(props.get("Cold Email Subject Text", {})).strip(),
            })
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return accounts


def write_to_notion(page_id: str, subject: str, body: str, owner: dict, ab_variant: str) -> bool:
    """Write email content, subject, owner, and campaign sender to a Notion account page."""
    resp = http_requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=_headers(),
        json={
            "properties": {
                "Cold Email Subject Text": {"rich_text": [{"text": {"content": subject}}]},
                "Cold Email Body": {"rich_text": [{"text": {"content": body}}]},
                "Campaign Sender": {"rich_text": [{"text": {"content": owner["full_name"]}}]},
                "Owner*": {"people": [{"object": "user", "id": owner["notion_id"]}]},
                "AB Variant": {"select": {"name": ab_variant}},
            }
        },
    )
    if resp.status_code != 200:
        console.print(f"  [red]  Notion {resp.status_code}: {resp.json().get('message','')[:120]}[/red]")
    return resp.status_code == 200


# ---------------------------------------------------------------------------
# Template fill logic
# ---------------------------------------------------------------------------

def build_email(account: dict, owner: dict, variant: str = "A") -> tuple[str, str]:
    """
    Return (subject, body) filled from account data and owner.
    Language is German for DACH countries, English otherwise.
    """
    ngo_name = account["name"] or "your organization"
    country = account["country"]
    is_german = country in DACH_COUNTRIES

    # Salutation: use contact first name if available, else org-level team greeting
    person_name = account["person_name"]
    if person_name:
        first_name = person_name.strip().split()[0]
        salutation = first_name
    else:
        salutation = f"{ngo_name}-Team" if is_german else f"{ngo_name} team"

    # Work area localisation
    raw_work_area = account["work_area"]
    if is_german:
        work_area = WORK_AREA_DE.get(raw_work_area, raw_work_area or "programmspezifische")
    else:
        work_area = raw_work_area.lower() if raw_work_area else "program-specific"

    sender_name = owner["full_name"]

    if is_german:
        subject_template = GERMAN_SUBJECT_B if variant == "B" else GERMAN_SUBJECT
        body_template = GERMAN_BODY_B if variant == "B" else GERMAN_BODY
        subject = subject_template.format(ngo_name=ngo_name)
        body = body_template.format(
            salutation=salutation,
            ngo_name=ngo_name,
            work_area=work_area,
            sender=sender_name,
        )
    else:
        subject_template = ENGLISH_SUBJECT_B if variant == "B" else ENGLISH_SUBJECT
        body_template = ENGLISH_BODY_B if variant == "B" else ENGLISH_BODY
        subject = subject_template.format(ngo_name=ngo_name)
        body = body_template.format(
            salutation=salutation,
            ngo_name=ngo_name,
            work_area=work_area,
            sender=sender_name,
        )

    return subject, body


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False, force: bool = False):
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Sales Agent — NGO Email Writer[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    console.print(f"[cyan]Campaign: {CAMPAIGN_ID}[/cyan]")
    if dry_run:
        console.print("[yellow]DRY RUN — will NOT write to Notion[/yellow]")
    if force:
        console.print("[yellow]FORCE — will overwrite existing emails[/yellow]")
    console.print("=" * 60)

    if not NOTION_TOKEN or not NOTION_DB_ACCOUNTS_ID:
        console.print("[red]Error: NOTION_TOKEN or NOTION_DB_ACCOUNTS_ID not set[/red]")
        return

    guidance = load_campaign_guidance(CAMPAIGN_ID)
    if guidance:
        console.print(Panel(guidance, title="Campaign Tracker guidance", border_style="cyan"))

    console.print(f"\n[cyan]Fetching accounts tagged with {CAMPAIGN_ID}...[/cyan]")
    accounts = fetch_campaign_accounts()
    console.print(f"[green]  Found {len(accounts)} accounts[/green]")

    if not accounts:
        console.print("[yellow]No accounts found for this campaign.[/yellow]")
        return

    # --- 50/50 owner split (deterministic shuffle by page_id for reproducibility) ---
    sorted_accounts = sorted(accounts, key=lambda a: a["page_id"])
    random.seed(42)
    random.shuffle(sorted_accounts)
    half = len(sorted_accounts) // 2
    owner_map: dict[str, dict] = {}
    for acc in sorted_accounts[:half]:
        owner_map[acc["page_id"]] = OWNERS[0]   # Carlo
    for acc in sorted_accounts[half:]:
        owner_map[acc["page_id"]] = OWNERS[1]   # Lisa
    # Handle odd count: extra goes to Carlo
    if len(sorted_accounts) % 2 == 1:
        owner_map[sorted_accounts[half]["page_id"]] = OWNERS[0]

    console.print(
        f"\n[cyan]Owner split:[/cyan] "
        f"{OWNERS[0]['full_name']} → {sum(1 for o in owner_map.values() if o == OWNERS[0])} accounts  |  "
        f"{OWNERS[1]['full_name']} → {sum(1 for o in owner_map.values() if o == OWNERS[1])} accounts"
    )

    written = skipped = errors = german = english = 0
    carlo_count = lisa_count = 0
    variant_counts = {"A": 0, "B": 0}

    for i, acc in enumerate(accounts, 1):
        name = acc["name"] or "(no name)"
        owner = owner_map[acc["page_id"]]
        variant = "A" if i % 2 == 1 else "B"
        variant_counts[variant] += 1

        # Skip if email already exists and not forcing
        if not force and acc["existing_email_body"]:
            console.print(f"  ({i}/{len(accounts)}) [dim]Skip (already has email): {name}[/dim]")
            skipped += 1
            continue

        subject, body = build_email(acc, owner, variant=variant)
        is_de = acc["country"] in DACH_COUNTRIES
        lang_label = "[DE]" if is_de else "[EN]"
        owner_label = f"[{owner['first_name']}]"
        if is_de:
            german += 1
        else:
            english += 1
        if owner == OWNERS[0]:
            carlo_count += 1
        else:
            lisa_count += 1

        console.print(Panel(
            f"[cyan]Owner:[/cyan] {owner['full_name']}   [cyan]Variant:[/cyan] {variant}   [cyan]Subject:[/cyan] {subject}\n\n{body}",
            title=f"({i}/{len(accounts)}) {lang_label} {owner_label} {name}",
            border_style="green" if is_de else "blue",
        ))

        if not dry_run:
            ok = write_to_notion(acc["page_id"], subject, body, owner, variant)
            if ok:
                written += 1
                console.print(f"  [green]✓ Written[/green]")
            else:
                errors += 1
                console.print(f"  [red]✗ Failed to write[/red]")
        else:
            console.print(f"  [yellow]Dry run — not written[/yellow]")

    # Summary
    console.print("\n")
    t = Table(title="NGO Email Writer — Summary")
    t.add_column("Metric", style="cyan")
    t.add_column("Value", style="green")
    t.add_row("Campaign", CAMPAIGN_ID)
    t.add_row("Total accounts", str(len(accounts)))
    t.add_row("Skipped (already have email)", str(skipped))
    t.add_row("German emails", str(german))
    t.add_row("English emails", str(english))
    t.add_row(f"Assigned to {OWNERS[0]['full_name']}", str(carlo_count))
    t.add_row(f"Assigned to {OWNERS[1]['full_name']}", str(lisa_count))
    t.add_row("Variant A / B", f"{variant_counts['A']} / {variant_counts['B']}")
    t.add_row("Written to Notion", str(written))
    t.add_row("Errors", str(errors))
    console.print(t)

    if not dry_run:
        console.print("\n[cyan]Syncing Campaign Tracker after NGO email writer run...[/cyan]")
        sync_campaign_tracker(campaign_id=CAMPAIGN_ID)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Write fixed email templates to NGO invoice campaign accounts"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--force", action="store_true", help="Overwrite existing emails")
    args = parser.parse_args()
    run(dry_run=args.dry_run, force=args.force)
