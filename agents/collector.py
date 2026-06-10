"""
Collector Agent - Aggregates leads from three streams into master_input.csv

Streams:
1. Manual Screenshots (GPT-4o Vision) -> data/inputs/images/
2. LinkedIn Post URLs -> data/inputs/linkedin_urls/
3. Manual Contacts -> data/inputs/manual_contacts/

Usage:
    python -m agents.collector          # One-time collection
    python -m agents.collector --watch  # Watch mode for continuous collection
"""
import os
import sys
import base64
import argparse
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Set, Dict

import pandas as pd
import requests
from openai import OpenAI
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from rich.console import Console
from rich.table import Table
from pydantic import BaseModel, Field

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import (
    OPENAI_API_KEY,
    MASTER_CSV,
    IMAGES_NEW_DIR,
    IMAGES_PROCESSED_DIR,
    LINKEDIN_URLS_NEW_DIR,
    LINKEDIN_URLS_PROCESSED_DIR,
    MANUAL_CONTACTS_NEW_DIR,
    MANUAL_CONTACTS_PROCESSED_DIR,
    MASTER_CSV_HEADERS,
    TABLES_DIR,
    LOGS_DIR
)
from utils.api_logger import log_api_usage

console = Console()

# Module-level domain cache to avoid redundant lookups within a single run
_domain_cache: Dict[str, str] = {}


class ExtractedLead(BaseModel):
    """Structured output from GPT-4o Vision."""
    company_name: str
    person_name: str
    context: str


class ExtractedEntity(BaseModel):
    """A single company/person extracted from a LinkedIn post."""
    company_name: str = Field(description="Company or organization name")
    company_domain: str = Field(default="", description="Company website domain (e.g., 'openai.com', 'google.com'). ALWAYS provide your best guess - prefer .com, .ai, .io extensions.")
    person_name: str = Field(default="", description="Person's name if mentioned with this company")
    person_linkedin_url: str = Field(default="", description="Person's LinkedIn profile URL if tagged/linked in the post")
    person_role: str = Field(default="", description="Person's role/title if mentioned")
    entity_type: str = Field(description="Type: 'company', 'vc', 'startup', 'sponsor', 'speaker', 'other'")
    trigger: str = Field(description="Outreach trigger - WHY we're reaching out. E.g., 'Speaker at TUM.ai E-Lab Final Pitch', 'Jury member at Munich AI Event', 'Sponsor of Student Hackathon'. Be specific about the event/context.")


class ExtractedPostEntities(BaseModel):
    """All entities extracted from a LinkedIn post."""
    post_context: str = Field(description="Brief summary of what the post is about")
    entities: List[ExtractedEntity] = Field(description="List of companies/people mentioned")


def ensure_files_exist():
    """Create necessary files and directories if they don't exist."""
    # Create directories
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_NEW_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    LINKEDIN_URLS_NEW_DIR.mkdir(parents=True, exist_ok=True)
    LINKEDIN_URLS_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize master CSV
    if not MASTER_CSV.exists():
        df = pd.DataFrame(columns=MASTER_CSV_HEADERS)
        df.to_csv(MASTER_CSV, index=False)
        console.print(f"[green]Created {MASTER_CSV}[/green]")


def normalize_domain(domain) -> str:
    """Normalize a domain for dedup: lowercase, strip www., trailing slashes."""
    if not domain or (isinstance(domain, float) and pd.isna(domain)):
        return ""
    domain = str(domain).lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.rstrip("/")


def is_profile_url(url) -> bool:
    """Check if URL is a LinkedIn profile URL (not a post URL)."""
    if not url or not isinstance(url, str):
        return False
    return "/in/" in url and "/posts/" not in url


def load_master_csv() -> pd.DataFrame:
    """Load the master CSV file, adding any missing columns from the current schema.

    Includes one-time migration: if old person_name_2/linkedin_url_contact_2 columns
    exist, split those into separate rows and drop the old columns.
    """
    ensure_files_exist()
    df = pd.read_csv(MASTER_CSV)

    # One-time migration: split person_name_2 rows into separate entries
    if "person_name_2" in df.columns:
        console.print("[cyan]Migrating CSV: splitting person_name_2 into separate rows...[/cyan]")
        new_rows = []
        for idx, row in df.iterrows():
            p2 = str(row.get("person_name_2", "") or "").strip()
            l2 = str(row.get("linkedin_url_contact_2", "") or "").strip()
            if p2 and p2 != "nan":
                new_row = row.to_dict()
                new_row["person_name"] = p2
                new_row["linkedin_url_contact"] = l2 if l2 != "nan" else ""
                # Remove old columns from the new row
                new_row.pop("person_name_2", None)
                new_row.pop("linkedin_url_contact_2", None)
                new_rows.append(new_row)

        # Drop old columns from existing df
        df = df.drop(columns=["person_name_2", "linkedin_url_contact_2"], errors="ignore")

        if new_rows:
            new_df = pd.DataFrame(new_rows)
            # Ensure columns match
            for col in df.columns:
                if col not in new_df.columns:
                    new_df[col] = ""
            new_df = new_df[df.columns]
            df = pd.concat([df, new_df], ignore_index=True)
            console.print(f"[green]Migration: created {len(new_rows)} new rows from person_name_2 data[/green]")

        # Save migrated CSV back to disk
        df.to_csv(MASTER_CSV, index=False)
        console.print("[green]Migration complete — old columns removed[/green]")

    # Backward compatibility: add missing columns with empty defaults
    for col in MASTER_CSV_HEADERS:
        if col not in df.columns:
            df[col] = ""
    df = df[MASTER_CSV_HEADERS]
    return df


def save_to_master_csv(leads: List[Dict]) -> int:
    """
    Append leads to master CSV with one-contact-per-row deduplication.

    Deduplication rules:
    - Primary key: (normalized_domain, normalized_person_name) or
      (normalized_company_name, normalized_person_name) if no domain
    - LinkedIn profile URLs are also checked for duplicates
    - Per-batch company cap: max 2 rows per company (existing + new).
      3rd+ contact gets status 'archived' instead of 'pending'.

    Args:
        leads: List of lead dicts matching CSV schema

    Returns:
        Number of new leads added
    """
    if not leads:
        return 0

    df = load_master_csv()

    # Build lookup indices from existing data
    # contact_key_set: (domain_or_company, normalized_person) pairs
    contact_key_set = set()
    profile_url_set = set()
    # company_row_count: how many rows per company (by domain or name)
    company_row_count = {}  # normalized key -> count

    for idx, row in df.iterrows():
        domain = normalize_domain(row.get("company_domain", ""))
        company = str(row.get("company_name", "")).lower().strip()
        person = str(row.get("person_name", "") or "").lower().strip()

        company_key = domain if domain else company

        # Track contact-level dedup keys
        if company_key and person:
            contact_key_set.add((company_key, person))

        # Track profile URLs
        url = row.get("linkedin_url_contact", "")
        if is_profile_url(url):
            profile_url_set.add(url)

        # Count rows per company
        if company_key:
            company_row_count[company_key] = company_row_count.get(company_key, 0) + 1

    new_rows = []
    skipped = 0

    for lead in leads:
        domain = normalize_domain(lead.get("company_domain", ""))
        company = str(lead.get("company_name", "")).lower().strip()
        person = str(lead.get("person_name", "") or "").lower().strip()
        url = lead.get("linkedin_url_contact", "")

        company_key = domain if domain else company

        # Skip if no company identifier
        if not company_key:
            continue

        # Skip duplicate profile URLs
        if is_profile_url(url) and url in profile_url_set:
            console.print(f"[yellow]Skipping duplicate profile URL: {url[:50]}[/yellow]")
            skipped += 1
            continue

        # Skip duplicate (company, person) pairs
        if person and (company_key, person) in contact_key_set:
            console.print(f"[yellow]Skipping duplicate: {lead.get('company_name')} / {lead.get('person_name')}[/yellow]")
            skipped += 1
            continue

        # Check company row cap (max 2 per company per batch)
        current_count = company_row_count.get(company_key, 0)
        if current_count >= 2:
            # 3rd+ contact → archive instead of pending
            lead = dict(lead)  # copy to avoid mutating original
            lead["status"] = "archived"
            console.print(f"[yellow]Archiving 3rd+ contact for {lead.get('company_name')}: {lead.get('person_name')}[/yellow]")

        # Add as new row
        new_rows.append(lead)

        # Update tracking sets
        if is_profile_url(url):
            profile_url_set.add(url)
        if person:
            contact_key_set.add((company_key, person))
        company_row_count[company_key] = current_count + 1

    # Append new rows
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        for col in MASTER_CSV_HEADERS:
            if col not in new_df.columns:
                new_df[col] = ""
        new_df = new_df[MASTER_CSV_HEADERS]
        df = pd.concat([df, new_df], ignore_index=True)

    # Save
    df.to_csv(MASTER_CSV, index=False)

    if new_rows:
        console.print(f"[green]Added {len(new_rows)} new lead rows[/green]")
    if skipped > 0:
        console.print(f"[yellow]Skipped {skipped} duplicate leads[/yellow]")

    return len(new_rows)


# =============================================================================
# STREAM 1: Screenshot Processing (GPT-4o Vision)
# =============================================================================

def encode_image(image_path: Path) -> str:
    """Encode image to base64 for GPT-4o Vision."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_image_mime_type(image_path: Path) -> str:
    """Get MIME type for image."""
    suffix = image_path.suffix.lower()
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp"
    }
    return mime_types.get(suffix, "image/png")


class ExtractedScreenshotLead(BaseModel):
    """A single company extracted from a screenshot."""
    company_name: str = Field(description="Company or organization name")
    company_domain: str = Field(default="", description="Company website domain (e.g., 'peec.ai', 'google.com'). ALWAYS provide your best guess based on company name.")
    person_name: str = Field(default="", description="Person's full name if visible")
    context: str = Field(description="Relevant context - what's happening in the screenshot, any events, triggers for outreach")


class ExtractedScreenshotLeads(BaseModel):
    """Multiple companies extracted from a single screenshot."""
    companies: List[ExtractedScreenshotLead] = Field(description="List of ALL companies visible in the screenshot. Extract EVERY company you see.")


def extract_lead_from_screenshot(image_path: Path) -> List[Dict]:
    """
    Use GPT-4o Vision to extract ALL lead info from a screenshot.

    Args:
        image_path: Path to the screenshot image

    Returns:
        List of lead dicts (empty list if extraction fails)
    """
    if not OPENAI_API_KEY:
        console.print("[red]Error: OPENAI_API_KEY not configured[/red]")
        return []

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=180.0, max_retries=4)

    try:
        base64_image = encode_image(image_path)
        mime_type = get_image_mime_type(image_path)

        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": """You are extracting lead information from screenshots (LinkedIn posts, event banners, company lists, job postings, etc.).

CRITICAL: Extract ALL companies visible in the screenshot. If there are multiple companies listed, extract EVERY SINGLE ONE.

For each company, extract:
- Company/organization name
- Company domain (ALWAYS provide your best guess - e.g., "peec.ai", "google.com")
- Person's name if visible
- Context/trigger for outreach (what's happening - events, job postings, announcements, list appearance)

COMPANY DOMAINS - ALWAYS provide a domain guess:
- For companies with "AI" in name, try .ai domain (Peec AI → peec.ai)
- For tech companies, try .com, .io, or .ai
- For well-known companies, use your knowledge
- NEVER leave company_domain empty - always provide your best guess
- Use lowercase, no "www." or "https://"

If the screenshot shows a list of companies (like an Apollo list, event sponsors, etc.), extract ALL of them."""
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Extract ALL companies from this screenshot. If there are multiple companies, extract every single one."
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            response_format=ExtractedScreenshotLeads,
            max_tokens=2000
        )

        extracted = response.choices[0].message.parsed
        log_api_usage("collector", "screenshot_extraction", "gpt-4o", response.usage, {"image": image_path.name})

        console.print(f"[dim]GPT-4o extracted {len(extracted.companies)} companies from {image_path.name}[/dim]")

        # Convert to lead dicts
        leads = []
        for company in extracted.companies:
            # Verify/improve company domain
            verified_domain = find_valid_domain(company.company_name, company.company_domain)
            if verified_domain != company.company_domain and verified_domain:
                console.print(f"[dim]Domain verified: {company.company_name} → {verified_domain}[/dim]")

            lead_dict = {
                "date_added": datetime.now().strftime("%Y-%m-%d"),
                "company_name": company.company_name,
                "company_domain": verified_domain,
                "person_name": company.person_name,
                "linkedin_url_contact": "",
                "linkedin_url_post": "",
                "trigger": company.context,  # Use context as trigger for screenshots
                "score": "",
                "reasoning": "",
                "source": "manual_screenshot",
                "status": "pending"
            }
            leads.append(lead_dict)
            console.print(f"[green]  → {company.company_name} (domain: {verified_domain})[/green]")

        return leads

    except Exception as e:
        console.print(f"[red]GPT-4o Vision error: {e}[/red]")
        return []


def process_new_screenshots() -> int:
    """
    Process all new screenshots from the 'new' folder and move to 'processed'.

    Returns:
        Number of leads extracted
    """
    console.print("\n[bold cyan]Stream 1: Processing Screenshots[/bold cyan]")

    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

    new_images = [
        f for f in IMAGES_NEW_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in image_extensions
    ]

    if not new_images:
        console.print("[dim]No new screenshots to process[/dim]")
        return 0

    console.print(f"[cyan]Found {len(new_images)} new screenshots[/cyan]")

    leads = []
    for image_path in new_images:
        console.print(f"[dim]Processing: {image_path.name}[/dim]")
        screenshot_leads = extract_lead_from_screenshot(image_path)

        if screenshot_leads:
            leads.extend(screenshot_leads)

        # Move to processed folder
        dest_path = IMAGES_PROCESSED_DIR / image_path.name
        image_path.rename(dest_path)
        console.print(f"[dim]Moved to processed: {image_path.name}[/dim]")

    added = save_to_master_csv(leads)
    return added


# =============================================================================
# Domain Verification
# =============================================================================

def verify_domain_exists(domain: str, timeout: float = 3.0) -> bool:
    """
    Check if a domain exists by making a HEAD request.

    Args:
        domain: Domain to check (e.g., "openai.com")
        timeout: Request timeout in seconds

    Returns:
        True if domain responds, False otherwise
    """
    if not domain:
        return False

    # Clean domain
    domain = domain.lower().strip()
    if domain.startswith("http"):
        return False  # Already a URL, not a domain

    try:
        url = f"https://{domain}"
        response = requests.head(url, timeout=timeout, allow_redirects=True)
        return response.status_code < 500
    except:
        try:
            # Try http if https fails
            url = f"http://{domain}"
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            return response.status_code < 500
        except:
            return False


def verify_company_on_homepage(domain: str, company_name: str, timeout: float = 5.0) -> bool:
    """
    Fetch homepage and verify company name appears on it.

    Args:
        domain: Domain to check
        company_name: Expected company name
        timeout: Request timeout

    Returns:
        True if company name found on homepage, False otherwise
    """
    if not domain or not company_name:
        return False

    try:
        url = f"https://{domain}"
        response = requests.get(url, timeout=timeout, allow_redirects=True)

        if response.status_code >= 500:
            return False

        # Get page content (try UTF-8, fallback to latin-1)
        try:
            content = response.content.decode('utf-8').lower()
        except:
            content = response.content.decode('latin-1', errors='ignore').lower()

        # Normalize company name for fuzzy matching
        company_normalized = company_name.lower().strip()
        company_no_spaces = re.sub(r'[^a-z0-9]', '', company_normalized)

        # Check if company name appears in content (various formats)
        checks = [
            company_normalized in content,
            company_no_spaces in content,
            company_name.lower().replace(' ', '') in content,
            # Check in title/meta tags (more reliable)
            f'<title>{company_normalized}' in content.replace('\n', ''),
            company_no_spaces in content[:5000]  # Check first 5KB (above-fold content)
        ]

        return any(checks)

    except:
        return False


def search_company_domain_ddg(company_name: str) -> Optional[str]:
    """
    Use DuckDuckGo Instant Answer API to find official company website.
    This is FREE and works well for known companies.

    Args:
        company_name: Company name to search

    Returns:
        Domain if found, None otherwise
    """
    try:
        # DuckDuckGo Instant Answer API (free, no auth needed!)
        url = "https://api.duckduckgo.com/"
        params = {
            "q": company_name,
            "format": "json",
            "no_html": 1,
            "skip_disambig": 1
        }

        response = requests.get(url, params=params, timeout=5)
        if response.status_code != 200:
            return None

        data = response.json()

        # Try to extract domain from AbstractURL or official website
        potential_domains = []

        if data.get("AbstractURL"):
            potential_domains.append(data["AbstractURL"])

        if data.get("Infobox"):
            for item in data["Infobox"].get("content", []):
                if item.get("label", "").lower() in ["website", "official site", "url"]:
                    potential_domains.append(item.get("value", ""))

        # Extract domain from URL
        for url in potential_domains:
            if url and isinstance(url, str):
                # Extract domain from URL
                match = re.search(r'(?:https?://)?(?:www\.)?([^/\s]+)', url)
                if match:
                    domain = match.group(1).lower()
                    # Verify it's a valid domain
                    if verify_domain_exists(domain):
                        return domain

        return None

    except:
        return None


def search_company_domain_google(company_name: str) -> Optional[str]:
    """
    Search Google for the official company website domain.
    Parses the HTML response to find the first non-social-media, non-Wikipedia domain.

    Args:
        company_name: Company name to search

    Returns:
        Domain if found, None otherwise
    """
    # Domains to skip (social media, generic sites)
    skip_domains = {
        "linkedin.com", "facebook.com", "twitter.com", "x.com",
        "instagram.com", "youtube.com", "wikipedia.org", "wikidata.org",
        "crunchbase.com", "glassdoor.com", "indeed.com", "bloomberg.com",
        "reuters.com", "google.com", "bing.com", "yahoo.com",
        "reddit.com", "medium.com", "github.com", "tiktok.com",
        "pitchbook.com", "apollo.io", "zoominfo.com",
    }

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        query = f"{company_name} official website"
        url = f"https://www.google.com/search?q={requests.utils.quote(query)}"
        response = requests.get(url, headers=headers, timeout=5)

        if response.status_code != 200:
            return None

        html = response.text

        # Extract URLs from Google search results
        # Google wraps result URLs in various patterns
        # Pattern 1: /url?q=https://domain.com/...
        url_matches = re.findall(r'/url\?q=(https?://[^&"]+)', html)
        # Pattern 2: Direct href links in result cards
        url_matches += re.findall(r'href="(https?://(?:www\.)?[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}[^"]*)"', html)

        for match_url in url_matches:
            # Extract domain from URL
            m = re.search(r'(?:https?://)?(?:www\.)?([a-zA-Z0-9\-]+\.[a-zA-Z0-9\.\-]+)', match_url)
            if not m:
                continue
            domain = m.group(1).lower().rstrip("/")

            # Skip social media and generic sites
            base_domain = ".".join(domain.split(".")[-2:])  # e.g., "linkedin.com"
            if base_domain in skip_domains:
                continue

            # Skip Google's own domains
            if "google" in domain:
                continue

            # Found a candidate
            return domain

        return None

    except Exception:
        return None


def lookup_domain_with_llm(company_name: str) -> Optional[str]:
    """
    Ask GPT-4o-mini directly for the company's official domain.
    GPT-4o-mini is very good at knowing company domains when asked directly.

    Args:
        company_name: Company name to look up

    Returns:
        Domain if known, None if unknown
    """
    if not OPENAI_API_KEY:
        return None

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=180.0, max_retries=4)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a domain lookup assistant. Reply with ONLY the domain (e.g., 'openai.com'). If you don't know, reply 'UNKNOWN'. No explanation."
                },
                {
                    "role": "user",
                    "content": f"What is the official website domain of the company '{company_name}'?"
                }
            ],
            max_tokens=30,
            temperature=0
        )

        log_api_usage("collector", "domain_lookup_llm", "gpt-4o-mini", response.usage,
                       {"company_name": company_name})

        result = response.choices[0].message.content.strip().lower()

        # Clean up the result
        result = result.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")

        if result == "unknown" or not result or " " in result or len(result) > 60:
            return None

        # Basic domain format validation
        if "." not in result:
            return None

        return result

    except Exception as e:
        console.print(f"[dim]LLM domain lookup error: {e}[/dim]")
        return None


def verify_domain_with_llm(company_name: str, candidate_domain: str) -> bool:
    """
    Ask GPT-4o-mini to verify if a candidate domain belongs to a company.
    This is the final arbiter for domain accuracy.

    Args:
        company_name: Company name
        candidate_domain: Domain to verify

    Returns:
        True if GPT-4o-mini confirms the domain is correct
    """
    if not OPENAI_API_KEY or not candidate_domain:
        return False

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=180.0, max_retries=4)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You verify company domains. Answer only YES or NO."
                },
                {
                    "role": "user",
                    "content": f"Is '{candidate_domain}' the official website domain of the company '{company_name}'? Consider that the company may be known by abbreviations or alternative names. Answer only YES or NO."
                }
            ],
            max_tokens=5,
            temperature=0
        )

        log_api_usage("collector", "domain_verify_llm", "gpt-4o-mini", response.usage,
                       {"company_name": company_name, "candidate_domain": candidate_domain})

        result = response.choices[0].message.content.strip().upper()
        return result.startswith("YES")

    except Exception as e:
        console.print(f"[dim]LLM domain verify error: {e}[/dim]")
        return False


def find_valid_domain(company_name: str, guessed_domain: str) -> str:
    """
    Multi-layer domain verification with high accuracy.

    Layer 0: GPT-4o-mini direct lookup (cheap, surprisingly accurate)
    Layer 1: DuckDuckGo Instant Answer (free, accurate for known companies)
    Layer 2: Google Search (new, finds most company domains)
    Layer 3: HTTP HEAD requests on GPT guess + variations
    Layer 4: Homepage content verification (company name on page)
    Layer 5: GPT-4o-mini final verification (cheap arbiter)

    Args:
        company_name: Company name for generating variations
        guessed_domain: GPT's initial domain guess

    Returns:
        Verified domain with highest confidence
    """
    # Check module-level cache first
    cache_key = company_name.lower().strip()
    if cache_key in _domain_cache:
        console.print(f"[dim]  Cache hit: {company_name} → {_domain_cache[cache_key]}[/dim]")
        return _domain_cache[cache_key]

    def _cache_and_return(domain: str) -> str:
        """Store result in cache and return it."""
        _domain_cache[cache_key] = domain
        return domain

    # Layer 0: Ask GPT-4o-mini directly (cheap and often very accurate)
    llm_domain = lookup_domain_with_llm(company_name)
    if llm_domain:
        console.print(f"[dim]  GPT-4o-mini lookup: {company_name} → {llm_domain}[/dim]")
        # Quick existence check
        if verify_domain_exists(llm_domain):
            # If the homepage also confirms, high confidence — return immediately
            if verify_company_on_homepage(llm_domain, company_name):
                console.print(f"[dim]  Verified (LLM + homepage): {llm_domain}[/dim]")
                return _cache_and_return(llm_domain)
            # Domain exists but homepage didn't confirm — still a strong signal,
            # hold as top candidate and continue checking
            top_candidate = llm_domain
        else:
            top_candidate = None
    else:
        top_candidate = None

    # Layer 1: Try DuckDuckGo instant answer (free & accurate!)
    ddg_domain = search_company_domain_ddg(company_name)
    if ddg_domain:
        if verify_company_on_homepage(ddg_domain, company_name):
            return _cache_and_return(ddg_domain)
        console.print(f"[dim]  DDG found: {ddg_domain}[/dim]")
        if not top_candidate:
            top_candidate = ddg_domain

    # Layer 2: Try Google Search
    google_domain = search_company_domain_google(company_name)
    if google_domain:
        console.print(f"[dim]  Google found: {google_domain}[/dim]")
        if verify_domain_exists(google_domain):
            if verify_company_on_homepage(google_domain, company_name):
                console.print(f"[dim]  Verified (Google + homepage): {google_domain}[/dim]")
                return _cache_and_return(google_domain)
            if not top_candidate:
                top_candidate = google_domain

    # Layer 3: Try guessed domain and variations via HTTP HEAD
    clean_name = re.sub(r'[^a-zA-Z0-9]', '', company_name.lower())
    clean_name_spaced = re.sub(r'[^a-zA-Z0-9\s]', '', company_name.lower()).replace(' ', '')

    domains_to_try = []
    if guessed_domain:
        guessed_domain = guessed_domain.lower().strip()
        domains_to_try.append(guessed_domain)

    extensions = ['.com', '.ai', '.io', '.co', '.org', '.de', '.net', '.vc', '.earth', '.tech']
    for ext in extensions:
        domains_to_try.append(f"{clean_name}{ext}")
        if clean_name != clean_name_spaced:
            domains_to_try.append(f"{clean_name_spaced}{ext}")

    # Remove duplicates while preserving order
    seen = set()
    unique_domains = []
    for d in domains_to_try:
        if d and d not in seen:
            seen.add(d)
            unique_domains.append(d)

    # Try each domain (limit to first 8 to avoid too much slowdown)
    verified_candidates = []
    for domain in unique_domains[:8]:
        if verify_domain_exists(domain):
            verified_candidates.append(domain)

    # Layer 4: Homepage content verification among HEAD-verified candidates
    if verified_candidates:
        for domain in verified_candidates:
            if verify_company_on_homepage(domain, company_name):
                console.print(f"[dim]  Verified with homepage check: {domain}[/dim]")
                return _cache_and_return(domain)

    # Layer 5: GPT-4o-mini final verification on the best candidate we have
    # Collect all candidates in priority order
    final_candidates = []
    if top_candidate:
        final_candidates.append(top_candidate)
    final_candidates.extend(verified_candidates)
    if guessed_domain and guessed_domain not in final_candidates:
        final_candidates.append(guessed_domain)

    for candidate in final_candidates:
        if verify_domain_with_llm(company_name, candidate):
            console.print(f"[dim]  LLM verified: {company_name} → {candidate}[/dim]")
            return _cache_and_return(candidate)

    # If we have a top candidate from LLM/DDG/Google but nothing verified,
    # still return it (better than a random guess)
    if top_candidate:
        return _cache_and_return(top_candidate)

    # If we have any verified candidate (exists via HTTP), return the first one
    if verified_candidates:
        return _cache_and_return(verified_candidates[0])

    # Return original guess if nothing worked
    result = guessed_domain if guessed_domain else ""
    return _cache_and_return(result)


# =============================================================================
# STREAM 2: LinkedIn Post URLs (Fetch & Extract)
# =============================================================================

def fetch_linkedin_post_content(url: str) -> Optional[str]:
    """
    Fetch content from a LinkedIn post URL.
    Extracts text and preserves LinkedIn profile URLs for entity extraction.

    Args:
        url: LinkedIn post URL

    Returns:
        Post content as text with embedded profile URLs, or None if fetch fails
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        html = response.text

        # Extract all LinkedIn profile URLs before cleaning HTML
        # Include %XX URL-encoded chars (e.g. %C3%A4 for ä) and trailing slug numbers
        profile_urls = re.findall(r'linkedin\.com/in/([a-zA-Z0-9_%\-\.~]+)', html)
        profile_urls = list(set(profile_urls))  # Deduplicate usernames
        # Strip trailing slashes or quotes that may have been captured
        profile_urls = [u.rstrip('/"\'') for u in profile_urls]

        # Convert to full URLs
        profile_urls = [f"https://www.linkedin.com/in/{username}" for username in profile_urls]

        # Extract name-URL pairs from anchor tags (e.g., <a href="linkedin.com/in/person">Name</a>)
        name_url_pairs = re.findall(
            r'<a[^>]*href=["\']?[^"\']*linkedin\.com/in/([a-zA-Z0-9_%\-\.~]+)["\']?[^>]*>([^<]+)</a>',
            html,
            re.IGNORECASE
        )
        # Convert to full URLs
        name_url_pairs = [(f"https://www.linkedin.com/in/{username.rstrip('/') }", name) for username, name in name_url_pairs]

        # Remove script and style tags
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)

        # Remove HTML tags but keep content
        text = re.sub(r'<[^>]+>', ' ', html)

        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        # Append extracted profile URLs section for GPT to use
        if profile_urls or name_url_pairs:
            text += "\n\n--- LINKEDIN PROFILE URLS FOUND IN POST ---\n"

            if name_url_pairs:
                for url_found, name in name_url_pairs:
                    text += f"- {name.strip()}: {url_found}\n"

            # Add any additional URLs not in name-URL pairs
            paired_urls = {pair[0] for pair in name_url_pairs}
            for profile_url in profile_urls:
                if profile_url not in paired_urls:
                    text += f"- Unknown: {profile_url}\n"

        return text[:15000]  # Limit to avoid token issues

    except Exception as e:
        console.print(f"[red]Error fetching LinkedIn post: {e}[/red]")
        return None


def extract_entities_from_post(url: str, content: str) -> List[Dict]:
    """
    Use GPT-4o to extract companies and people from LinkedIn post content.

    Args:
        url: Original LinkedIn post URL
        content: Post text content

    Returns:
        List of lead dicts
    """
    if not OPENAI_API_KEY:
        console.print("[red]Error: OPENAI_API_KEY not configured[/red]")
        return []

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=180.0, max_retries=4)

    try:
        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": """You are extracting company and people information from LinkedIn posts.

Your task: Find ALL companies, organizations, startups, VCs, sponsors, and notable people mentioned.

For each entity, extract:
- Company/organization name
- Company website domain (e.g., "openai.com", "google.com", "balderton.com"). Infer this from your knowledge of the company. Use the most common/official domain. Leave empty only if truly unknown.
- Person's name (if associated with the company)
- Person's LinkedIn profile URL (IMPORTANT: Look for tagged/linked profiles in the HTML. These appear as URLs like "linkedin.com/in/username" or profile links. Extract the full URL if available.)
- Their role/title if mentioned
- Type (company, vc, startup, sponsor, speaker, other)
- TRIGGER for outreach (IMPORTANT): A specific, personalized reason for reaching out based on the post context. Examples:
  * "Speaker at TUM.ai E-Lab Final Pitch 2026"
  * "Jury member at Munich AI Demo Day"
  * "Sponsor of TUM.ai Hackathon"
  * "Mentioned in TUM.ai's AI innovation showcase"
  Be specific - include event name, role, and context. This will be used in personalized outreach.

CRITICAL: When people are tagged in LinkedIn posts, their profile URLs are embedded in the HTML. Look for patterns like:
- href="https://www.linkedin.com/in/..."
- linkedin.com/in/[username]
Extract these URLs for the person_linkedin_url field.

COMPANY DOMAINS - ALWAYS provide a domain guess:
- For well-known companies, use your knowledge (OpenAI → openai.com, Google → google.com)
- For VCs, use pattern: [name].com (Balderton → balderton.com, Speedinvest → speedinvest.com)
- For startups with "AI" in name, try .ai domain (Manex AI → manex.ai)
- For tech startups, try .io or .com (Spherecast → spherecast.io or spherecast.com)
- For catering/events/local businesses, try .com or country TLD
- NEVER leave company_domain empty - always provide your best guess
- Use lowercase, no "www." or "https://"

Focus on entities that could be potential business leads:
- VCs and investors
- Startups and tech companies
- Corporate sponsors
- Event speakers and their companies
- Notable professionals

Skip generic mentions like "LinkedIn", "the event", etc."""
                },
                {
                    "role": "user",
                    "content": f"Extract all companies and people from this LinkedIn post:\n\n{content[:10000]}"
                }
            ],
            response_format=ExtractedPostEntities,
            max_tokens=2000
        )

        extracted = response.choices[0].message.parsed
        log_api_usage("collector", "post_entity_extraction", "gpt-4o", response.usage, {"url": url[:100]})
        post_context = extracted.post_context

        leads = []
        for entity in extracted.entities:
            # Verify/improve company domain
            verified_domain = find_valid_domain(entity.company_name, entity.company_domain)
            if verified_domain != entity.company_domain and verified_domain:
                console.print(f"[dim]Domain verified: {entity.company_name} → {verified_domain}[/dim]")

            # Build context string
            context_parts = [post_context]
            if entity.person_role:
                context_parts.append(f"Role: {entity.person_role}")
            context_parts.append(f"Type: {entity.entity_type}")

            leads.append({
                "date_added": datetime.now().strftime("%Y-%m-%d"),
                "company_name": entity.company_name,
                "company_domain": verified_domain,
                "person_name": entity.person_name,
                "linkedin_url_contact": entity.person_linkedin_url,  # Person's profile URL
                "linkedin_url_post": url,  # Original post URL for reference
                "trigger": entity.trigger,  # Outreach trigger for personalization
                "score": "",  # Filled by ranking agent
                "reasoning": "",  # Filled by ranking agent
                "source": "linkedin_post",
                "status": "pending"
            })

        console.print(f"[green]Extracted {len(leads)} entities from post[/green]")
        return leads

    except Exception as e:
        console.print(f"[red]GPT-4o extraction error: {e}[/red]")
        return []


def process_linkedin_urls() -> int:
    """
    Process LinkedIn post URLs: fetch content, extract entities, add to CSV.

    Uses persistent files: successfully processed URLs are removed from the file,
    failed URLs stay for retry on the next run. Files remain in new/ folder.

    Returns:
        Number of leads added
    """
    console.print("\n[bold cyan]Stream 2: Processing LinkedIn Post URLs[/bold cyan]")

    # Find all .txt files in the new folder
    url_files = [
        f for f in LINKEDIN_URLS_NEW_DIR.iterdir()
        if f.is_file() and f.suffix.lower() == ".txt"
    ]

    if not url_files:
        console.print("[dim]No new LinkedIn URL files to process[/dim]")
        return 0

    total_added = 0

    for url_file in url_files:
        console.print(f"[dim]Processing file: {url_file.name}[/dim]")

        lines = url_file.read_text().strip().split("\n")
        urls = [
            line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#") and "linkedin.com" in line
        ]

        if not urls:
            console.print(f"[dim]No new LinkedIn URLs in {url_file.name}[/dim]")
            continue

        console.print(f"[cyan]Found {len(urls)} LinkedIn post URLs[/cyan]")

        processed_urls = set()
        all_leads = []
        for url in urls:
            console.print(f"[dim]Fetching: {url[:60]}...[/dim]")

            # Fetch post content
            content = fetch_linkedin_post_content(url)

            if content:
                # Extract entities using GPT-4o
                leads = extract_entities_from_post(url, content)
                all_leads.extend(leads)
                processed_urls.add(url)
            else:
                console.print(f"[yellow]Could not fetch: {url[:50]}... (will retry next run)[/yellow]")

        if all_leads:
            added = save_to_master_csv(all_leads)
            total_added += added

        # Remove processed URLs from file, keep comments, blanks, and failed URLs
        remaining_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                remaining_lines.append(line)
            elif stripped not in processed_urls:
                remaining_lines.append(line)
        url_file.write_text("\n".join(remaining_lines).rstrip() + "\n")

        if processed_urls:
            console.print(f"[dim]Removed {len(processed_urls)} processed URLs from {url_file.name}[/dim]")

    return total_added


# =============================================================================
# STREAM 3: Manual Contact URLs (txt with: linkedin_url, company_name, trigger)
# =============================================================================

def process_manual_contacts() -> int:
    """
    Process manual contact .txt files from the 'new' folder.

    Format: one contact per line, comma-separated:
        linkedin_url, company_name, trigger

    Lines starting with # are comments and preserved.
    Successfully processed lines are removed from the file.

    Returns:
        Number of contacts added
    """
    console.print("\n[bold cyan]Stream 3: Processing Manual Contacts[/bold cyan]")

    MANUAL_CONTACTS_NEW_DIR.mkdir(parents=True, exist_ok=True)
    MANUAL_CONTACTS_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    txt_files = [
        f for f in MANUAL_CONTACTS_NEW_DIR.iterdir()
        if f.is_file() and f.suffix.lower() == ".txt"
    ]

    if not txt_files:
        console.print("[dim]No manual contact files to process[/dim]")
        return 0

    total_added = 0

    for txt_file in txt_files:
        console.print(f"[dim]Processing file: {txt_file.name}[/dim]")

        lines = txt_file.read_text().strip().split("\n")

        leads = []
        processed_lines = set()

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            parts = [p.strip() for p in stripped.split(",", 2)]
            if len(parts) < 2:
                console.print(f"[yellow]Skipping malformed line {i+1}: {stripped[:60]}[/yellow]")
                continue

            linkedin_url = parts[0]
            company_name = parts[1]
            trigger = parts[2] if len(parts) > 2 else "Manual contact upload"

            if not linkedin_url or not company_name:
                continue

            verified_domain = find_valid_domain(company_name, "")

            leads.append({
                "date_added": datetime.now().strftime("%Y-%m-%d"),
                "company_name": company_name,
                "company_domain": verified_domain,
                "person_name": "",
                "linkedin_url_contact": linkedin_url,
                "linkedin_url_post": "",
                "trigger": trigger,
                "score": "",
                "reasoning": "",
                "source": "manual_contact",
                "status": "pending"
            })
            processed_lines.add(i)

        if leads:
            console.print(f"[cyan]Found {len(leads)} contacts in {txt_file.name}[/cyan]")
            added = save_to_master_csv(leads)
            total_added += added

        # Remove processed lines, keep comments and blank lines
        remaining = [line for i, line in enumerate(lines) if i not in processed_lines]
        txt_file.write_text("\n".join(remaining) + "\n")

        if processed_lines:
            console.print(f"[dim]Removed {len(processed_lines)} processed contacts from {txt_file.name}[/dim]")

    return total_added


# =============================================================================
# Watch Mode (File System Monitoring)
# =============================================================================

class ImageHandler(FileSystemEventHandler):
    """Watch for new images in the screenshots directory."""

    def on_created(self, event):
        if event.is_directory:
            return

        path = Path(event.src_path)
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            console.print(f"\n[bold yellow]New screenshot detected: {path.name}[/bold yellow]")
            process_new_screenshots()


def run_watch_mode():
    """Run collector in watch mode for continuous monitoring."""
    console.print("[bold magenta]Starting Watch Mode[/bold magenta]")
    console.print(f"[dim]Watching: {IMAGES_NEW_DIR}[/dim]")
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    # Initial collection
    run_collection()

    # Set up file watcher
    observer = Observer()
    observer.schedule(ImageHandler(), str(IMAGES_NEW_DIR), recursive=False)
    observer.start()

    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        console.print("\n[yellow]Watch mode stopped[/yellow]")

    observer.join()


# =============================================================================
# Main Collection Run
# =============================================================================

def run_collection():
    """Run a single collection cycle across all streams."""
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Sales Agent - Collector[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    console.print("=" * 60)

    ensure_files_exist()

    total_added = 0

    # Stream 1: Screenshots
    total_added += process_new_screenshots()

    # Stream 2: LinkedIn URLs
    total_added += process_linkedin_urls()

    # Stream 3: Manual Contacts
    total_added += process_manual_contacts()

    # Summary
    console.print("\n" + "-" * 40)
    df = load_master_csv()
    pending = len(df[df["status"] == "pending"])

    table = Table(title="Collection Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("New leads added", str(total_added))
    table.add_row("Total in master CSV", str(len(df)))
    table.add_row("Pending for ranking", str(pending))

    console.print(table)

    return total_added


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TUM Sales Agent Collector")
    parser.add_argument("--watch", action="store_true", help="Run in watch mode")
    args = parser.parse_args()

    if args.watch:
        run_watch_mode()
    else:
        run_collection()
