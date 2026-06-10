"""
NGO Scraper Agent — Scans existing NGO accounts in Notion to find those matching
the Entreculturas model: NGOs that distribute funds to local partner organizations
in developing countries.

These NGOs manage high volumes of invoices from local partners and are ideal
candidates for the AI Invoice Management tool.

Pipeline:
1. Query Notion Accounts DB for NGOs at "Prospect Qualified" or "Contacted Mail" status
2. GPT-4o classifies each: does this NGO fund local partners abroad?
   (uses Mission*, Work Area, Country, org name; fetches website if mission is thin)
3. Tag matches with campaign ID + update trigger event

Usage:
    python -m agents.ngo_scraper                          # run full pipeline
    python -m agents.ngo_scraper --dry-run                # preview without writing to Notion
    python -m agents.ngo_scraper --min-confidence 0.7     # stricter threshold
    python -m agents.ngo_scraper --all-statuses           # scan ALL NGOs, not just Prospect/Contacted
"""
import sys
import re
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from openai import OpenAI
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import resilient_http as http_requests

from utils.config import OPENAI_API_KEY, NOTION_TOKEN, NOTION_DB_ACCOUNTS_ID
from utils.api_logger import log_api_usage
from utils.notion_client import NOTION_API_VERSION

console = Console()

CAMPAIGN_ID = "NGO_180526_InvoiceManagement"
TRIGGER_EVENT = (
    "NGO distributes funds to local partner organizations in developing countries, "
    "likely managing high volumes of invoices from partners. "
    "Strong fit for AI invoice inspection & management tool (Entreculturas model)."
)

# Statuses to scan by default
TARGET_STATUSES = {"Prospect Qualified", "Contacted Mail \U0001f4e9"}

# Social partnership campaigns — only scan NGOs from these
SOCIAL_CAMPAIGNS = [
    "NGO_base_development_161025",
    "NGO_Spain_Environment_201025",
    "NGO_Jin_Education_201025",
    "AUARA_Substitution",
    "NGOs_Health_Environment_Animals_DE",
    "NGOs_19032026_Health",
    "NGOs_19032026_Health_CH",
    "NGOs_28042026_Poverty_Alleviation",
]

# Min characters in Mission* before we bother fetching the website
MISSION_MIN_CHARS = 60

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


# ---------------------------------------------------------------------------
# Pydantic model for GPT-4o structured output
# ---------------------------------------------------------------------------


class NGOClassification(BaseModel):
    """GPT-4o classification of whether an NGO matches the Entreculturas model."""
    is_match: bool = Field(
        description="True if this NGO distributes funds/grants to LOCAL partner organizations "
                    "in developing countries (not just running its own programs directly)."
    )
    confidence: float = Field(
        description="Confidence 0.0-1.0 that this is a genuine match."
    )
    reason: str = Field(
        description="1-2 sentences: why this NGO is or is not a match for the invoice management tool."
    )


# ---------------------------------------------------------------------------
# Notion query helpers
# ---------------------------------------------------------------------------


def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def _extract_text(prop: dict) -> str:
    """Extract plain text from a Notion rich_text or title property."""
    ptype = prop.get("type", "")
    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if ptype == "select" and prop.get("select"):
        return prop["select"].get("name", "")
    if ptype == "multi_select":
        return ", ".join(ms.get("name", "") for ms in prop.get("multi_select", []))
    if ptype == "url":
        return prop.get("url") or ""
    if ptype == "email":
        return prop.get("email") or ""
    if ptype == "status" and prop.get("status"):
        return prop["status"].get("name", "")
    if ptype == "formula":
        return prop.get("formula", {}).get("string", "") or ""
    return ""


def fetch_all_target_ngos(all_statuses: bool = False) -> list[dict]:
    """
    Query Notion Accounts DB for NGOs at target statuses.

    Returns list of dicts with all relevant fields extracted.
    """
    headers = _notion_headers()
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ACCOUNTS_ID}/query"

    # Build filter: NGO type + in social partnership campaign + target status
    campaign_filter = {
        "or": [
            {"property": "Campaign ID", "multi_select": {"contains": c}}
            for c in SOCIAL_CAMPAIGNS
        ]
    }

    if all_statuses:
        query_filter = {
            "and": [
                {"property": "Account Type*", "select": {"equals": "NGO"}},
                campaign_filter,
            ]
        }
    else:
        query_filter = {
            "and": [
                {"property": "Account Type*", "select": {"equals": "NGO"}},
                campaign_filter,
                {"or": [
                    {"property": "Status", "status": {"equals": s}}
                    for s in TARGET_STATUSES
                ]},
            ]
        }

    results = []
    has_more = True
    start_cursor = None

    while has_more:
        body = {"page_size": 100, "filter": query_filter}
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = http_requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            console.print(f"[red]Notion query error: {resp.status_code} - {resp.json().get('message', '')}[/red]")
            return []

        data = resp.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    # Parse into clean dicts
    ngos = []
    for page in results:
        props = page.get("properties", {})
        ngo = {
            "page_id": page.get("id", ""),
            "name": _extract_text(props.get("Organization*", {})),
            "mission": _extract_text(props.get("Mission*", {})),
            "company_description": _extract_text(props.get("Company Description", {})),
            "work_area": _extract_text(props.get("Work Area NGO", {})),
            "country": _extract_text(props.get("Country", {})),
            "city": _extract_text(props.get("City", {})),
            "website": _extract_text(props.get("Website URL*", {})),
            "domain": _extract_text(props.get("Domain*", {})),
            "status": _extract_text(props.get("Status", {})),
            "trigger": _extract_text(props.get("Trigger Event", {})),
            "campaign_ids": [
                ms.get("name", "")
                for ms in props.get("Campaign ID", {}).get("multi_select", [])
            ],
            "general_email": _extract_text(props.get("General Email", {})),
        }
        # Skip entries with no name
        if ngo["name"]:
            ngos.append(ngo)

    return ngos


# ---------------------------------------------------------------------------
# Website fetching (fallback when Mission is thin)
# ---------------------------------------------------------------------------


def fetch_page_text(url: str, max_chars: int = 6000) -> str:
    """Fetch a page and return stripped text content (best-effort)."""
    if not url:
        return ""
    try:
        if not url.startswith("http"):
            url = f"https://{url}"
        resp = http_requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code >= 400:
            return ""
        html = resp.content[:40000].decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "iframe"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        return text[:max_chars]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# GPT-4o classification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert analyst identifying NGOs that operate like Entreculturas:
they COLLECT donations/grants centrally and then DISTRIBUTE funds to LOCAL PARTNER ORGANIZATIONS
in developing countries. These local partners execute projects on the ground (building schools,
running clinics, providing microfinance, etc.) and send invoices back to the central NGO for
reimbursement/reporting.

This operating model means they handle MANY invoices from partner orgs every month.

MATCH criteria (the NGO must clearly do this):
- Channels funds to local partner organizations, community-based orgs, or local NGOs abroad
- Acts as intermediary/umbrella: collects money centrally, distributes to local implementing partners
- Works OUTSIDE of Europe through a NETWORK of local organizations
- Mentions "local partners", "partner organizations", "sub-grants", "regranting", "implementing partners"

DO NOT MATCH:
- NGOs that ONLY run their own programs directly with their own staff (e.g. MSF-style)
- Pure advocacy/lobbying NGOs (e.g. FIAN, Amnesty) that don't fund local implementation
- Domestic-only charities (only active in Germany/Europe)
- Student clubs, foundations that only give scholarships
- NGOs that work locally in one country without partner networks
- NGOs where the description is too vague to determine the operating model (set confidence very low)

When unsure, lean toward NOT matching. We only want clear fits."""


def classify_ngo(
    client: OpenAI,
    ngo: dict,
    website_text: str = "",
) -> Optional[NGOClassification]:
    """Use GPT-4o to classify whether an NGO matches the Entreculturas funding model."""

    context_parts = [f"**Organization:** {ngo['name']}"]
    if ngo["work_area"]:
        context_parts.append(f"**Work Area:** {ngo['work_area']}")
    if ngo["country"]:
        context_parts.append(f"**Country/HQ:** {ngo['country']}")
    if ngo["city"]:
        context_parts.append(f"**City:** {ngo['city']}")
    if ngo["mission"]:
        context_parts.append(f"**Mission:** {ngo['mission']}")
    if ngo["company_description"]:
        context_parts.append(f"**Description:** {ngo['company_description']}")
    if ngo["trigger"]:
        context_parts.append(f"**Existing Trigger:** {ngo['trigger']}")
    if ngo["website"]:
        context_parts.append(f"**Website:** {ngo['website']}")
    if website_text:
        context_parts.append(f"\n**Website content (excerpt):**\n{website_text[:4000]}")

    user_prompt = "\n".join(context_parts)
    user_prompt += "\n\nDoes this NGO distribute funds to local partner organizations in developing countries?"

    try:
        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=NGOClassification,
            max_tokens=500,
        )

        result = response.choices[0].message.parsed

        log_api_usage(
            "ngo_scraper", "classify_ngo", "gpt-4o",
            response.usage,
            {
                "name": ngo["name"],
                "is_match": result.is_match,
                "confidence": result.confidence,
            },
        )

        return result

    except Exception as e:
        console.print(f"[red]  Classification error for {ngo['name']}: {e}[/red]")
        return None


# ---------------------------------------------------------------------------
# Notion update: tag with campaign + update trigger
# ---------------------------------------------------------------------------


def tag_account_with_campaign(page_id: str, existing_campaigns: list[str]) -> bool:
    """Add the campaign ID to the account's Campaign ID multi_select and update trigger."""
    headers = _notion_headers()

    # Append new campaign to existing ones
    all_campaigns = list(existing_campaigns)
    if CAMPAIGN_ID not in all_campaigns:
        all_campaigns.append(CAMPAIGN_ID)

    properties = {
        "Campaign ID": {"multi_select": [{"name": c} for c in all_campaigns]},
        "Trigger Event": {"rich_text": [{"text": {"content": TRIGGER_EVENT}}]},
    }

    try:
        resp = http_requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=headers,
            json={"properties": properties},
        )
        return resp.status_code == 200
    except Exception as e:
        console.print(f"  [red]Error tagging account: {e}[/red]")
        return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_ngo_scraper(
    dry_run: bool = False,
    min_confidence: float = 0.6,
    all_statuses: bool = False,
):
    """
    Scan existing NGO accounts and tag those matching the Entreculturas model.

    Args:
        dry_run: Preview without writing to Notion.
        min_confidence: Minimum GPT-4o confidence to accept a match.
        all_statuses: If True, scan ALL NGOs regardless of status.
    """
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Sales Agent - NGO Invoice Campaign Scanner[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    console.print(f"[cyan]Campaign: {CAMPAIGN_ID}[/cyan]")
    statuses_label = "all statuses" if all_statuses else ", ".join(TARGET_STATUSES)
    console.print(f"[cyan]Target statuses: {statuses_label}[/cyan]")
    console.print(f"[cyan]Source campaigns: {len(SOCIAL_CAMPAIGNS)} social partnership campaigns[/cyan]")
    console.print(f"[cyan]Min confidence: {min_confidence}[/cyan]")
    if dry_run:
        console.print("[yellow]DRY RUN — will NOT write to Notion[/yellow]")
    console.print("=" * 60)

    # Validate
    if not OPENAI_API_KEY:
        console.print("[red]Error: OPENAI_API_KEY not configured[/red]")
        return
    if not NOTION_TOKEN or not NOTION_DB_ACCOUNTS_ID:
        console.print("[red]Error: NOTION_TOKEN or NOTION_DB_ACCOUNTS_ID not configured[/red]")
        return

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=180.0, max_retries=4)

    # --- Step 1: Fetch NGOs from Notion ---
    console.print("\n[cyan]Step 1: Fetching NGO accounts from Notion...[/cyan]")
    ngos = fetch_all_target_ngos(all_statuses=all_statuses)
    console.print(f"[green]  Found {len(ngos)} NGO accounts[/green]")

    if not ngos:
        console.print("[yellow]No NGOs found at target statuses.[/yellow]")
        return

    # --- Step 2: Skip already-tagged ---
    to_classify = []
    already_tagged = 0
    for ngo in ngos:
        if CAMPAIGN_ID in ngo["campaign_ids"]:
            already_tagged += 1
        else:
            to_classify.append(ngo)

    if already_tagged:
        console.print(f"[dim]  Skipping {already_tagged} already tagged with {CAMPAIGN_ID}[/dim]")

    console.print(f"[cyan]  {len(to_classify)} NGOs to classify[/cyan]")

    if not to_classify:
        console.print("[green]All NGOs already processed.[/green]")
        return

    # --- Step 3: Classify each NGO ---
    console.print(f"\n[cyan]Step 2: Classifying {len(to_classify)} NGOs with GPT-4o...[/cyan]")

    matches = []
    not_match = 0
    low_confidence = 0
    errors = 0
    website_fetches = 0

    for i, ngo in enumerate(to_classify, 1):
        console.print(f"  [bold]({i}/{len(to_classify)}) {ngo['name']}[/bold]", end="")

        # Check if we need to fetch the website for more context
        mission_text = (ngo["mission"] or "") + " " + (ngo["company_description"] or "")
        website_text = ""
        if len(mission_text.strip()) < MISSION_MIN_CHARS and ngo["website"]:
            console.print(f" [dim](fetching website...)[/dim]", end="")
            website_text = fetch_page_text(ngo["website"])
            website_fetches += 1

        console.print()  # newline

        result = classify_ngo(client, ngo, website_text)
        if not result:
            errors += 1
            continue

        if not result.is_match:
            console.print(f"    [dim]Not a match: {result.reason[:80]}[/dim]")
            not_match += 1
            continue

        if result.confidence < min_confidence:
            console.print(f"    [yellow]Low confidence ({result.confidence:.2f}): {result.reason[:60]}[/yellow]")
            low_confidence += 1
            continue

        console.print(f"    [green]MATCH ({result.confidence:.2f}): {result.reason[:80]}[/green]")
        matches.append((ngo, result))

    # --- Step 4: Summary ---
    console.print(f"\n[cyan]Step 3: Results summary[/cyan]")

    summary_table = Table(title="NGO Invoice Campaign Scanner")
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", style="green")
    summary_table.add_row("Total NGOs fetched", str(len(ngos)))
    summary_table.add_row("Already tagged", str(already_tagged))
    summary_table.add_row("Classified", str(len(to_classify)))
    summary_table.add_row("Matches", f"[bold green]{len(matches)}[/bold green]")
    summary_table.add_row("Not a match", str(not_match))
    summary_table.add_row("Low confidence", str(low_confidence))
    summary_table.add_row("Errors", str(errors))
    summary_table.add_row("Website fetches", str(website_fetches))
    summary_table.add_row("Campaign", CAMPAIGN_ID)
    console.print(summary_table)

    if not matches:
        console.print("[yellow]No new NGOs matched the Entreculturas model.[/yellow]")
        return

    # Preview matches
    console.print(f"\n[bold]Matched NGOs:[/bold]")
    for j, (ngo, result) in enumerate(matches, 1):
        console.print(Panel(
            f"[cyan]Name:[/cyan] {ngo['name']}\n"
            f"[cyan]Status:[/cyan] {ngo['status']}\n"
            f"[cyan]Work Area:[/cyan] {ngo['work_area'] or 'N/A'}\n"
            f"[cyan]Country:[/cyan] {ngo['country'] or 'N/A'}\n"
            f"[cyan]City:[/cyan] {ngo['city'] or 'N/A'}\n"
            f"[cyan]Website:[/cyan] {ngo['website'] or 'N/A'}\n"
            f"[cyan]Mission:[/cyan] {(ngo['mission'] or 'N/A')[:150]}\n"
            f"[cyan]Confidence:[/cyan] {result.confidence:.2f}\n"
            f"[cyan]Reason:[/cyan] {result.reason}",
            title=f"#{j} — {ngo['name']}",
            border_style="green",
        ))

    # --- Step 5: Tag in Notion ---
    if dry_run:
        console.print(f"\n[yellow]DRY RUN complete. {len(matches)} NGOs would be tagged with {CAMPAIGN_ID}.[/yellow]")
        return

    console.print(f"\n[cyan]Step 4: Tagging {len(matches)} accounts in Notion...[/cyan]")

    tagged = 0
    failed = 0

    for ngo, result in matches:
        ok = tag_account_with_campaign(ngo["page_id"], ngo["campaign_ids"])
        if ok:
            tagged += 1
            console.print(f"  [green]Tagged: {ngo['name']}[/green]")
        else:
            failed += 1
            console.print(f"  [red]Failed: {ngo['name']}[/red]")

    console.print(f"\n[green]Done. Tagged {tagged}/{len(matches)} accounts with {CAMPAIGN_ID}.[/green]")
    if failed:
        console.print(f"[red]Failed: {failed}[/red]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scan existing NGO accounts for Entreculturas-model matches"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Notion")
    parser.add_argument("--min-confidence", type=float, default=0.6,
                        help="Min GPT-4o confidence threshold (default 0.6)")
    parser.add_argument("--all-statuses", action="store_true",
                        help="Scan ALL NGO accounts, not just Prospect Qualified / Contacted Mail")
    args = parser.parse_args()

    run_ngo_scraper(
        dry_run=args.dry_run,
        min_confidence=args.min_confidence,
        all_statuses=args.all_statuses,
    )
