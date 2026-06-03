"""
NGO Web Discovery Agent — Discovers NEW German NGOs from the internet that match
the Entreculturas model: NGOs distributing funds to local partner orgs in developing countries.

Sources (in order):
1. VENRO members list (venro.org/mitglieder/) — German dev NGO umbrella, ~130 orgs
2. Targeted DuckDuckGo searches for additional German NGOs
3. betterplace.org German NGO partner list (supplementary)

Pipeline:
1. Fetch NGO names + websites from structured sources
2. Deduplicate against ALL existing Notion accounts (by domain + name)
3. Fetch website text + GPT-4o classification
4. Save new matches to data/tables/ngo_discovery_YYYYMMDD.csv
5. Optionally add new matches to Notion Accounts DB (Prospect Qualified + campaign tag)

Usage:
    python -m agents.ngo_web_discovery --dry-run            # classify without touching Notion
    python -m agents.ngo_web_discovery --add-to-notion      # classify + create new Notion accounts
    python -m agents.ngo_web_discovery --max-orgs 80        # cap number of orgs to process
    python -m agents.ngo_web_discovery --min-confidence 0.7 # stricter threshold
"""

import sys
import re
import csv
import time
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin

import requests as http_requests
from bs4 import BeautifulSoup
from openai import OpenAI
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import OPENAI_API_KEY, NOTION_TOKEN, NOTION_DB_ACCOUNTS_ID
from utils.api_logger import log_api_usage
from utils.notion_client import NOTION_API_VERSION

console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAMPAIGN_ID = "NGO_180526_InvoiceManagement"
TRIGGER_EVENT = (
    "NGO distributes funds to local partner organizations in developing countries, "
    "likely managing high volumes of invoices from partners. "
    "Strong fit for AI invoice inspection & management tool (Entreculturas model)."
)
MISSION_MIN_CHARS = 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Discovery sources: (label, url, scraper_fn_name)
SOURCES = [
    "VENRO",           # venro.org/mitglieder/ — German dev NGO umbrella
    "DDG_SEARCH",      # DuckDuckGo targeted queries
]

# Targeted DuckDuckGo queries for German NGOs
DDG_QUERIES = [
    "site:venro.org OR deutsche NGO Entwicklungszusammenarbeit Partnerorganisationen",
    "deutsche Entwicklungshilfe NGO lokale Partner Entwicklungsländer",
    "Germany development NGO grants local partners Sub-Saharan Africa",
    "Entwicklungshilfe NGO Deutschland Förderung lokale Organisationen",
    "German international NGO sub-grants implementing partners",
]

# Output dir
DATA_DIR = Path(__file__).parent.parent / "data" / "tables"


# ---------------------------------------------------------------------------
# Pydantic model (same as ngo_scraper.py)
# ---------------------------------------------------------------------------


class NGOClassification(BaseModel):
    is_match: bool = Field(
        description="True if this NGO distributes funds/grants to LOCAL partner organizations "
                    "in developing countries (not just running its own programs directly)."
    )
    confidence: float = Field(description="Confidence 0.0-1.0 that this is a genuine match.")
    reason: str = Field(
        description="1-2 sentences: why this NGO is or is not a match for the invoice management tool."
    )
    mission_summary: str = Field(
        description="1 sentence summarizing what this NGO does. Used as Mission field in Notion."
    )


# ---------------------------------------------------------------------------
# Web fetch helpers
# ---------------------------------------------------------------------------


def fetch_page_text(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return stripped text content."""
    if not url:
        return ""
    try:
        if not url.startswith("http"):
            url = f"https://{url}"
        resp = http_requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        if resp.status_code >= 400:
            return ""
        html = resp.content[:60000].decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "iframe", "aside"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        return text[:max_chars]
    except Exception:
        return ""


def extract_domain(url: str) -> str:
    """Normalize a URL to just its domain for dedup."""
    if not url:
        return ""
    try:
        if not url.startswith("http"):
            url = "https://" + url
        parsed = urlparse(url)
        domain = parsed.netloc.lower().lstrip("www.")
        return domain.rstrip("/")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Source 1: VENRO members list
# ---------------------------------------------------------------------------


def scrape_venro_members() -> list[dict]:
    """
    Scrape VENRO's member list at venro.org/mitglieder/liste
    Uses the alphabetical member list page which has 144 member profile links.
    Returns list of {name, website, description}
    """
    console.print("[cyan]  Fetching VENRO members list...[/cyan]")
    orgs = []

    list_url = "https://venro.org/mitglieder/liste"
    try:
        resp = http_requests.get(list_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            console.print(f"[yellow]  VENRO returned {resp.status_code}[/yellow]")
            return []

        soup = BeautifulSoup(resp.content, "html.parser")

        # VENRO uses "mehr" links pointing to individual member profile pages
        # Pattern: /mitglieder/unsere-mitglieder/mitglied/<slug>
        member_profile_links = [
            a for a in soup.select("a[href*='/mitglieder/unsere-mitglieder/mitglied/']")
        ]

        # Extract org names from the page text (they appear right before each "mehr" link)
        # The list page shows: OrgName\nAddress\nmehr — we extract via parent text nodes
        seen_slugs = set()
        for link in member_profile_links:
            href = link.get("href", "")
            slug = href.rstrip("/").split("/")[-1]
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            # Walk up to find the nearest text sibling/parent that is the org name
            name = ""
            parent = link.parent
            for _ in range(5):
                if parent is None:
                    break
                # Look for the first substantial text in this container
                for child in parent.children:
                    text = child.get_text(strip=True) if hasattr(child, "get_text") else str(child).strip()
                    if text and text.lower() not in ("mehr", "details") and len(text) > 4:
                        name = text
                        break
                if name:
                    break
                parent = parent.parent

            # Fallback: derive a readable name from the slug
            if not name:
                name = slug.replace("-", " ").title()

            profile_url = urljoin("https://venro.org", href)
            orgs.append({
                "name": name,
                "profile_url": profile_url,
                "website": "",      # filled in below
                "description": "",
                "source": "VENRO",
            })

        console.print(f"[cyan]  VENRO: {len(orgs)} member profiles — fetching external websites...[/cyan]")

        # Follow each profile page to get the external website URL
        for i, org in enumerate(orgs):
            ext = _extract_external_from_venro_profile(org["profile_url"])
            org["website"] = ext
            if (i + 1) % 20 == 0:
                console.print(f"  [dim]  ...{i+1}/{len(orgs)} fetched[/dim]")
            time.sleep(0.4)  # polite crawl rate

        console.print(f"[green]  VENRO: scraped {len(orgs)} member organizations[/green]")
        return orgs

    except Exception as e:
        console.print(f"[yellow]  VENRO scrape error: {e}[/yellow]")
        return []


def _extract_external_from_venro_profile(profile_url: str) -> str:
    """Follow a VENRO member profile page and extract the NGO's external website."""
    try:
        resp = http_requests.get(profile_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.content, "html.parser")
        # The profile page usually has a "Website" or "Homepage" field with external link
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            if (href.startswith("http")
                    and "venro.org" not in href
                    and "facebook" not in href
                    and "twitter" not in href
                    and "linkedin" not in href
                    and "." in href):
                return href
        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Source 2: DuckDuckGo searches
# ---------------------------------------------------------------------------


def _search_duckduckgo(query: str, max_results: int = 10) -> list[dict]:
    """Search DuckDuckGo HTML and return {title, url} results."""
    results = []
    for attempt in range(2):
        try:
            resp = http_requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=HEADERS,
                timeout=15,
            )
            if resp.status_code == 202:
                time.sleep(4)
                continue
            if resp.status_code != 200:
                break

            soup = BeautifulSoup(resp.content, "html.parser")
            for result in soup.select(".result"):
                title_el = result.select_one(".result__title a")
                url_el = result.select_one(".result__url")
                if title_el:
                    title = title_el.get_text(strip=True)
                    href = title_el.get("href", "")
                    # DDG wraps URLs — extract actual URL
                    if "uddg=" in href:
                        from urllib.parse import unquote, parse_qs
                        qs = parse_qs(urlparse(href).query)
                        href = qs.get("uddg", [href])[0]
                        href = unquote(href)
                    if href.startswith("http"):
                        results.append({"name": title, "url": href})
                if len(results) >= max_results:
                    break
            break
        except Exception:
            time.sleep(2)
    return results


def scrape_dzi_list() -> list[dict]:
    """
    Scrape DZI (Deutsches Zentralinstitut für soziale Fragen) spenden.org directory.
    DZI-certified orgs are vetted German donation orgs — high quality signal.
    Returns list of {name, website, source}
    """
    console.print("[cyan]  Fetching DZI certified organizations...[/cyan]")
    orgs = []
    try:
        resp = http_requests.get(
            "https://www.spenden.org/organisationen/",
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            console.print(f"[yellow]  spenden.org returned {resp.status_code}[/yellow]")
            return []

        soup = BeautifulSoup(resp.content, "html.parser")

        # Extract org cards / links
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            name = link.get_text(strip=True)
            # External links to org websites
            if (href.startswith("http")
                    and "spenden.org" not in href
                    and "dzi.de" not in href
                    and name
                    and len(name) > 4):
                orgs.append({"name": name, "website": href, "description": "", "source": "DZI"})

        console.print(f"[green]  DZI/spenden.org: found {len(orgs)} organizations[/green]")
    except Exception as e:
        console.print(f"[yellow]  DZI scrape error: {e}[/yellow]")
    return orgs


def scrape_engagement_global() -> list[dict]:
    """
    Fetch Engagement Global partner list — German govt agency working with NGOs.
    Returns list of {name, website, source}
    """
    console.print("[cyan]  Fetching Engagement Global partner list...[/cyan]")
    orgs = []
    try:
        # Their partner/förderpartner section
        urls_to_try = [
            "https://www.engagement-global.de/partnerorganisationen.html",
            "https://www.engagement-global.de/unsere-partner.html",
        ]
        for url in urls_to_try:
            resp = http_requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, "html.parser")
                for link in soup.select("a[href]"):
                    href = link.get("href", "")
                    name = link.get_text(strip=True)
                    if (href.startswith("http")
                            and "engagement-global" not in href
                            and name and len(name) > 4):
                        orgs.append({"name": name, "website": href, "description": "", "source": "EG"})
                break
    except Exception as e:
        console.print(f"[yellow]  Engagement Global scrape error: {e}[/yellow]")

    console.print(f"[green]  Engagement Global: found {len(orgs)} organizations[/green]")
    return orgs


def discover_via_duckduckgo() -> list[dict]:
    """
    Run targeted DDG queries for German NGOs not covered by directory scraping.
    Returns list of {name, website, description, source}
    """
    console.print("[cyan]  Running DuckDuckGo discovery searches...[/cyan]")
    orgs = []
    seen_domains = set()

    # Simpler, more targeted single-concept queries
    targeted_queries = [
        "Entwicklungshilfe NGO Deutschland Projektpartner Afrika",
        "deutsche Hilfsorganisation lokale Partner Entwicklungsländer Spenden",
        "german humanitarian NGO sub-grants local partners developing countries",
        "Partnerorganisationen Entwicklungszusammenarbeit Deutschland NGO",
        "kleinere Hilfsorganisationen Deutschland Projektfinanzierung Partnerländer",
    ]

    skip_domains = {
        "facebook", "twitter", "linkedin", "youtube", "instagram",
        "wikipedia", "spiegel", "zeit", "faz", "sueddeutsche", "welt",
        "bundesregierung", "bmi.bund", "giz.de", "bmz.bund",
        "venro.org", "spenden.org", "dzi.de", "engagement-global.de",
    }

    for query in targeted_queries:
        results = _search_duckduckgo(query, max_results=8)
        for r in results:
            domain = extract_domain(r["url"])
            if not domain or domain in seen_domains:
                continue
            if any(skip in domain for skip in skip_domains):
                continue
            seen_domains.add(domain)
            orgs.append({
                "name": r["name"],
                "website": r["url"],
                "description": "",
                "source": "DDG",
            })
        time.sleep(3)

    console.print(f"[green]  DuckDuckGo: found {len(orgs)} candidate organizations[/green]")
    return orgs


# ---------------------------------------------------------------------------
# Source 3: Hardcoded German development NGO seeds
# (well-known orgs that might not surface via DDG or VENRO, for completeness)
# ---------------------------------------------------------------------------

GERMAN_NGO_SEEDS = [
    {"name": "Welthungerhilfe", "website": "https://www.welthungerhilfe.de", "source": "seed"},
    {"name": "Brot für die Welt", "website": "https://www.brot-fuer-die-welt.de", "source": "seed"},
    {"name": "Misereor", "website": "https://www.misereor.de", "source": "seed"},
    {"name": "terre des hommes Deutschland", "website": "https://www.tdh.de", "source": "seed"},
    {"name": "Kindernothilfe", "website": "https://www.kindernothilfe.de", "source": "seed"},
    {"name": "Diakonie Katastrophenhilfe", "website": "https://www.diakonie-katastrophenhilfe.de", "source": "seed"},
    {"name": "Caritas international", "website": "https://www.caritas-international.de", "source": "seed"},
    {"name": "AWO International", "website": "https://www.awo-international.de", "source": "seed"},
    {"name": "Malteser International", "website": "https://www.malteser-international.org", "source": "seed"},
    {"name": "Evangelischer Entwicklungsdienst (EED/Brot für die Welt)", "website": "https://www.brot-fuer-die-welt.de", "source": "seed"},
    {"name": "Deutsche Welthungerhilfe", "website": "https://www.welthungerhilfe.de", "source": "seed"},
    {"name": "Oxfam Deutschland", "website": "https://www.oxfam.de", "source": "seed"},
    {"name": "ActionAid Deutschland", "website": "https://www.actionaid.de", "source": "seed"},
    {"name": "HelpAge Deutschland", "website": "https://www.helpage.de", "source": "seed"},
    {"name": "Plan International Deutschland", "website": "https://www.plan.de", "source": "seed"},
    {"name": "Save the Children Deutschland", "website": "https://www.savethechildren.de", "source": "seed"},
    {"name": "World Vision Deutschland", "website": "https://www.worldvision.de", "source": "seed"},
    {"name": "SOS Kinderdorf International", "website": "https://www.sos-kinderdorf.de", "source": "seed"},
    {"name": "UNICEF Deutschland", "website": "https://www.unicef.de", "source": "seed"},
    {"name": "Deutsche AIDS-Hilfe / International AIDS", "website": "https://www.deutsche-aids-hilfe.de", "source": "seed"},
    {"name": "Forum Fairer Handel", "website": "https://www.forum-fairer-handel.de", "source": "seed"},
    {"name": "CARE Deutschland-Luxemburg", "website": "https://www.care.de", "source": "seed"},
    {"name": "Aktion gegen den Hunger (ACF Deutschland)", "website": "https://www.aktion-gegen-den-hunger.de", "source": "seed"},
    {"name": "Ärzte der Welt (Médicos del Mundo DE)", "website": "https://www.aerzte-der-welt.de", "source": "seed"},
    {"name": "ZDF Hilfe / UNHCR Deutschland", "website": "https://www.unhcr.org/de", "source": "seed"},
    {"name": "Christoffel Blindenmission (CBM)", "website": "https://www.cbm.org", "source": "seed"},
    {"name": "Sightsavers Deutschland", "website": "https://www.sightsavers.de", "source": "seed"},
    {"name": "Naturschutzbund NABU International", "website": "https://www.nabu-international.de", "source": "seed"},
    {"name": "GreenCross Deutschland", "website": "https://www.greencross.de", "source": "seed"},
    {"name": "Menschen für Menschen", "website": "https://www.menschenfuermenschen.de", "source": "seed"},
    {"name": "HORIZONT3000", "website": "https://www.horizont3000.org", "source": "seed"},
    {"name": "Gemeinsam für Afrika", "website": "https://www.gemeinsam-fuer-afrika.de", "source": "seed"},
    {"name": "Engagement Global", "website": "https://www.engagement-global.de", "source": "seed"},
    {"name": "Naturfreunde Internationale Deutschland", "website": "https://www.naturfreunde.de/internationale", "source": "seed"},
    {"name": "Deutsche Stiftung Weltbevölkerung (DSW)", "website": "https://www.dsw.org", "source": "seed"},
    {"name": "Werkstatt Ökonomie", "website": "https://www.woe.de", "source": "seed"},
    {"name": "Christliche Initiative Romero", "website": "https://www.ci-romero.de", "source": "seed"},
    {"name": "INKOTA-netzwerk", "website": "https://www.inkota.de", "source": "seed"},
    {"name": "Welt-Sichten (missio München)", "website": "https://www.missio-muenchen.de", "source": "seed"},
    {"name": "Misereor Diözesankomitee", "website": "https://www.misereor.de", "source": "seed"},
    {"name": "Forum Weltkirche", "website": "https://www.forum-weltkirche.de", "source": "seed"},
    {"name": "GADES - Gesellschaft für Entwicklungszusammenarbeit", "website": "https://www.gades.de", "source": "seed"},
    {"name": "Stiftung Weltbevölkerung", "website": "https://www.dsw.org", "source": "seed"},
    {"name": "Karl Kübel Stiftung", "website": "https://www.kks-online.de", "source": "seed"},
    {"name": "Renovabis (Osteuropa-Hilfswerk)", "website": "https://www.renovabis.de", "source": "seed"},
    {"name": "Adveniat (Lateinamerika-Hilfswerk)", "website": "https://www.adveniat.de", "source": "seed"},
    {"name": "Missio Aachen", "website": "https://www.missio-hilft.de", "source": "seed"},
    {"name": "Hilfswerk Österreich (DE branch)", "website": "https://www.hilfswerk.at", "source": "seed"},
    {"name": "Deutsche Gesellschaft für Internationale Zusammenarbeit - NGO window", "website": "https://www.giz.de", "source": "seed"},
    {"name": "Ökumenischer Rat der Kirchen (German office)", "website": "https://www.oikumene.org", "source": "seed"},
]


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------


def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def fetch_all_existing_domains() -> tuple[set, set]:
    """
    Fetch ALL Notion Accounts to build dedup sets of (domains, normalized_names).
    Returns (domains_set, names_set).
    """
    headers = _notion_headers()
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ACCOUNTS_ID}/query"

    domains = set()
    names = set()
    has_more = True
    start_cursor = None

    while has_more:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = http_requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            console.print(f"[red]Notion query error: {resp.status_code}[/red]")
            break

        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            # Name
            for pname, pdata in props.items():
                if pdata.get("type") == "title":
                    parts = pdata.get("title", [])
                    if parts:
                        names.add(parts[0].get("plain_text", "").lower().strip())
                    break
            # Website
            web_prop = props.get("Website URL*", {})
            if web_prop.get("type") == "url" and web_prop.get("url"):
                d = extract_domain(web_prop["url"])
                if d:
                    domains.add(d)
            # Domain field
            domain_prop = props.get("Domain*", {})
            raw_domain = ""
            if domain_prop.get("type") == "rich_text":
                parts = domain_prop.get("rich_text", [])
                if parts:
                    raw_domain = parts[0].get("plain_text", "").lower().strip()
            elif domain_prop.get("type") == "formula":
                raw_domain = domain_prop.get("formula", {}).get("string", "").lower().strip()
            if raw_domain:
                domains.add(raw_domain.lstrip("www."))

        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return domains, names


def create_ngo_in_notion(ngo: dict, mission_summary: str) -> bool:
    """
    Create a new NGO account in Notion Accounts DB.
    Sets Account Type* = NGO, Status = Prospect Qualified, Campaign = CAMPAIGN_ID.
    """
    headers = _notion_headers()

    name = ngo.get("name", "")[:200]
    website = ngo.get("website", "")
    country = ngo.get("country", "Germany")

    if not name:
        return False

    properties = {
        "Organization*": {"title": [{"text": {"content": name}}]},
        "Status": {"status": {"name": "Prospect Qualified"}},
        "Account Type*": {"select": {"name": "NGO"}},
        "Campaign ID": {"multi_select": [{"name": CAMPAIGN_ID}]},
        "Trigger Event": {"rich_text": [{"text": {"content": TRIGGER_EVENT}}]},
    }

    if website:
        if not website.startswith("http"):
            website = "https://" + website
        properties["Website URL*"] = {"url": website}

    if country:
        properties["Country"] = {"multi_select": [{"name": country}]}

    if mission_summary:
        properties["Mission*"] = {"rich_text": [{"text": {"content": mission_summary[:2000]}}]}

    try:
        resp = http_requests.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json={"parent": {"database_id": NOTION_DB_ACCOUNTS_ID}, "properties": properties},
        )
        return resp.status_code == 200
    except Exception as e:
        console.print(f"[red]  Error creating account: {e}[/red]")
        return False


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
- Government agencies (GIZ, BMWK, etc.)
- Political parties or government bodies

When unsure, lean toward NOT matching. We only want clear fits.
Always provide a mission_summary regardless of match status."""


def classify_ngo(client: OpenAI, ngo: dict, website_text: str = "") -> Optional[NGOClassification]:
    """GPT-4o classification of an NGO."""
    context_parts = [f"**Organization:** {ngo['name']}"]
    if ngo.get("description"):
        context_parts.append(f"**Description:** {ngo['description']}")
    if ngo.get("website"):
        context_parts.append(f"**Website:** {ngo['website']}")
    if website_text:
        context_parts.append(f"\n**Website content (excerpt):**\n{website_text[:5000]}")

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
            max_tokens=600,
        )

        result = response.choices[0].message.parsed

        log_api_usage(
            "ngo_web_discovery", "classify_ngo", "gpt-4o",
            response.usage,
            {"name": ngo["name"], "is_match": result.is_match, "confidence": result.confidence},
        )

        return result

    except Exception as e:
        console.print(f"[red]  Classification error for {ngo['name']}: {e}[/red]")
        return None


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------


def is_duplicate(ngo: dict, existing_domains: set, existing_names: set) -> bool:
    """Check if an NGO already exists in Notion by domain or name."""
    domain = extract_domain(ngo.get("website", ""))
    if domain and domain in existing_domains:
        return True
    name_norm = ngo.get("name", "").lower().strip()
    # Fuzzy name match — check if normalized name is contained in any existing name
    for existing in existing_names:
        if name_norm and (name_norm in existing or existing in name_norm):
            return True
    return False


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------


def save_results_csv(matches: list[dict], filename: str):
    """Save matched NGOs to CSV."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / filename

    fieldnames = [
        "name", "website", "domain", "country", "source",
        "confidence", "mission_summary", "reason",
        "added_to_notion",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for m in matches:
            writer.writerow({k: m.get(k, "") for k in fieldnames})

    console.print(f"[green]Results saved to: {path}[/green]")
    return path


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_web_discovery(
    dry_run: bool = True,
    add_to_notion: bool = False,
    max_orgs: int = 200,
    min_confidence: float = 0.65,
    skip_seeds: bool = False,
    skip_ddg: bool = False,
):
    """
    Discover new German NGOs from the web and classify them.

    Args:
        dry_run: If True, don't write to Notion.
        add_to_notion: If True (and not dry_run), create new accounts in Notion.
        max_orgs: Cap on number of orgs to classify (to control API cost).
        min_confidence: Min GPT-4o confidence to accept a match.
        skip_seeds: Skip hardcoded seed list.
        skip_ddg: Skip DuckDuckGo searches.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str = datetime.now().strftime("%Y%m%d")

    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Sales Agent - NGO Web Discovery[/bold magenta]")
    console.print(f"[dim]{timestamp}[/dim]")
    console.print(f"[cyan]Campaign: {CAMPAIGN_ID}[/cyan]")
    console.print(f"[cyan]Min confidence: {min_confidence}[/cyan]")
    console.print(f"[cyan]Max orgs to classify: {max_orgs}[/cyan]")
    if dry_run:
        console.print("[yellow]DRY RUN — will NOT write to Notion[/yellow]")
    elif add_to_notion:
        console.print("[green]LIVE — will create new Notion accounts[/green]")
    else:
        console.print("[cyan]Classify only — saving CSV, no Notion writes[/cyan]")
    console.print("=" * 60)

    if not OPENAI_API_KEY:
        console.print("[red]Error: OPENAI_API_KEY not configured[/red]")
        return
    if not NOTION_TOKEN or not NOTION_DB_ACCOUNTS_ID:
        console.print("[red]Error: NOTION_TOKEN or NOTION_DB_ACCOUNTS_ID not configured[/red]")
        return

    client = OpenAI(api_key=OPENAI_API_KEY)

    # ------------------------------------------------------------------
    # Step 1: Fetch existing Notion accounts for dedup
    # ------------------------------------------------------------------
    console.print("\n[cyan]Step 1: Fetching existing Notion accounts for dedup...[/cyan]")
    existing_domains, existing_names = fetch_all_existing_domains()
    console.print(f"[green]  Loaded {len(existing_domains)} domains, {len(existing_names)} org names[/green]")

    # ------------------------------------------------------------------
    # Step 2: Collect candidate NGOs from all sources
    # ------------------------------------------------------------------
    console.print("\n[cyan]Step 2: Collecting candidate NGOs from web sources...[/cyan]")

    all_candidates = []
    seen_domains_local: set[str] = set()

    def _add_candidates(orgs: list[dict]):
        added = 0
        for org in orgs:
            d = extract_domain(org.get("website", ""))
            if d and d in seen_domains_local:
                continue  # local dedup
            if d:
                seen_domains_local.add(d)
            all_candidates.append(org)
            added += 1
        return added

    # VENRO members (primary source — ~144 German development NGOs)
    venro_orgs = scrape_venro_members()
    n = _add_candidates(venro_orgs)
    console.print(f"  Added {n} from VENRO")

    # DZI certified orgs (German quality-certified donation orgs)
    dzi_orgs = scrape_dzi_list()
    n = _add_candidates(dzi_orgs)
    console.print(f"  Added {n} from DZI/spenden.org")

    # Engagement Global partners
    eg_orgs = scrape_engagement_global()
    n = _add_candidates(eg_orgs)
    console.print(f"  Added {n} from Engagement Global")

    # Hardcoded seeds (well-known German dev NGOs for completeness)
    if not skip_seeds:
        n = _add_candidates(GERMAN_NGO_SEEDS)
        console.print(f"  Added {n} from seed list")

    # DuckDuckGo discovery (catches smaller orgs not in directories)
    if not skip_ddg:
        ddg_orgs = discover_via_duckduckgo()
        n = _add_candidates(ddg_orgs)
        console.print(f"  Added {n} from DuckDuckGo")

    console.print(f"\n[bold]Total candidates before dedup: {len(all_candidates)}[/bold]")

    # ------------------------------------------------------------------
    # Step 3: Dedup against Notion
    # ------------------------------------------------------------------
    console.print("\n[cyan]Step 3: Deduplicating against existing Notion accounts...[/cyan]")

    new_candidates = []
    skipped_existing = 0
    for org in all_candidates:
        if is_duplicate(org, existing_domains, existing_names):
            skipped_existing += 1
        else:
            new_candidates.append(org)

    console.print(f"  Skipped {skipped_existing} already in Notion")
    console.print(f"[green]  {len(new_candidates)} new organizations to classify[/green]")

    if not new_candidates:
        console.print("[yellow]All discovered NGOs are already in Notion![/yellow]")
        return

    # Cap to control API cost
    to_classify = new_candidates[:max_orgs]
    if len(new_candidates) > max_orgs:
        console.print(f"[yellow]  Capped at {max_orgs} (use --max-orgs to increase)[/yellow]")

    # ------------------------------------------------------------------
    # Step 4: Classify with GPT-4o
    # ------------------------------------------------------------------
    console.print(f"\n[cyan]Step 4: Classifying {len(to_classify)} organizations with GPT-4o...[/cyan]")

    matches = []
    not_match = 0
    low_confidence = 0
    errors = 0
    website_fetches = 0

    for i, org in enumerate(to_classify, 1):
        console.print(f"  [bold]({i}/{len(to_classify)}) {org['name'][:60]}[/bold]", end="")

        # Always fetch website for web-discovered orgs (we don't have Notion mission field)
        website_text = ""
        if org.get("website"):
            console.print(f" [dim](fetching...)[/dim]", end="")
            website_text = fetch_page_text(org["website"])
            website_fetches += 1

        console.print()  # newline

        # Skip if we got nothing
        if not website_text and not org.get("description"):
            console.print(f"    [yellow]No content available, skipping[/yellow]")
            errors += 1
            continue

        result = classify_ngo(client, org, website_text)
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
        matches.append({
            "name": org["name"],
            "website": org.get("website", ""),
            "domain": extract_domain(org.get("website", "")),
            "country": org.get("country", "Germany"),
            "source": org.get("source", "web"),
            "confidence": result.confidence,
            "mission_summary": result.mission_summary,
            "reason": result.reason,
            "added_to_notion": False,
        })

        # Small delay between API calls to avoid rate limiting on website fetches
        time.sleep(0.5)

    # ------------------------------------------------------------------
    # Step 5: Summary + preview
    # ------------------------------------------------------------------
    console.print(f"\n[cyan]Step 5: Results[/cyan]")

    summary_table = Table(title="NGO Web Discovery Results")
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", style="green")
    summary_table.add_row("Candidates discovered", str(len(all_candidates)))
    summary_table.add_row("Already in Notion (skipped)", str(skipped_existing))
    summary_table.add_row("Classified", str(len(to_classify)))
    summary_table.add_row("New matches", f"[bold green]{len(matches)}[/bold green]")
    summary_table.add_row("Not a match", str(not_match))
    summary_table.add_row("Low confidence", str(low_confidence))
    summary_table.add_row("Errors / no content", str(errors))
    summary_table.add_row("Website fetches", str(website_fetches))
    console.print(summary_table)

    if not matches:
        console.print("[yellow]No new NGOs matched the Entreculturas model.[/yellow]")
        return

    # Preview
    console.print(f"\n[bold]New Matches:[/bold]")
    for j, m in enumerate(matches, 1):
        console.print(Panel(
            f"[cyan]Name:[/cyan] {m['name']}\n"
            f"[cyan]Website:[/cyan] {m['website']}\n"
            f"[cyan]Country:[/cyan] {m['country']}\n"
            f"[cyan]Source:[/cyan] {m['source']}\n"
            f"[cyan]Confidence:[/cyan] {m['confidence']:.2f}\n"
            f"[cyan]Mission:[/cyan] {m['mission_summary']}\n"
            f"[cyan]Reason:[/cyan] {m['reason']}",
            title=f"#{j} — {m['name']}",
            border_style="green",
        ))

    # ------------------------------------------------------------------
    # Step 6: Add to Notion (optional)
    # ------------------------------------------------------------------
    if not dry_run and add_to_notion:
        console.print(f"\n[cyan]Step 6: Creating {len(matches)} new accounts in Notion...[/cyan]")
        created = 0
        failed = 0
        for m in matches:
            ok = create_ngo_in_notion(m, m["mission_summary"])
            if ok:
                created += 1
                m["added_to_notion"] = True
                console.print(f"  [green]Created: {m['name']}[/green]")
            else:
                failed += 1
                console.print(f"  [red]Failed: {m['name']}[/red]")
        console.print(f"\n[green]Created {created}/{len(matches)} accounts in Notion.[/green]")
        if failed:
            console.print(f"[red]Failed: {failed}[/red]")
    elif dry_run:
        console.print(f"\n[yellow]DRY RUN — {len(matches)} NGOs would be added to Notion.[/yellow]")
    else:
        console.print(f"\n[cyan]Classify-only mode — use --add-to-notion to create Notion accounts.[/cyan]")

    # ------------------------------------------------------------------
    # Step 7: Save CSV
    # ------------------------------------------------------------------
    csv_filename = f"ngo_discovery_{date_str}.csv"
    save_results_csv(matches, csv_filename)

    console.print(f"\n[bold green]Discovery complete. {len(matches)} new NGOs found.[/bold green]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Discover new German NGOs from the web matching the Entreculturas model"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify without writing to Notion")
    parser.add_argument("--add-to-notion", action="store_true",
                        help="Create new accounts in Notion for matched NGOs")
    parser.add_argument("--max-orgs", type=int, default=200,
                        help="Max number of orgs to classify (default: 200)")
    parser.add_argument("--min-confidence", type=float, default=0.65,
                        help="Min GPT-4o confidence threshold (default: 0.65)")
    parser.add_argument("--skip-seeds", action="store_true",
                        help="Skip hardcoded German NGO seed list")
    parser.add_argument("--skip-ddg", action="store_true",
                        help="Skip DuckDuckGo searches")
    args = parser.parse_args()

    run_web_discovery(
        dry_run=args.dry_run,
        add_to_notion=args.add_to_notion,
        max_orgs=args.max_orgs,
        min_confidence=args.min_confidence,
        skip_seeds=args.skip_seeds,
        skip_ddg=args.skip_ddg,
    )
