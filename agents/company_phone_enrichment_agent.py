"""
Company Phone Enrichment Agent.

Finds missing company phone numbers for early-stage Accounts in Notion by
scanning official websites, especially Impressum/Kontakt/contact pages.

Usage:
    python -m agents.company_phone_enrichment_agent --dry-run
    python -m agents.company_phone_enrichment_agent
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests as http_requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table

# Add parent to path for imports.
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import LOGS_DIR, NOTION_TOKEN
from utils.notion_client import _notion_api_headers

try:
    from agents.collector import (
        find_valid_domain,
        lookup_domain_with_llm,
        search_company_domain_ddg,
        verify_domain_exists,
    )
except Exception:  # pragma: no cover - website-property fallback is optional.
    find_valid_domain = None
    lookup_domain_with_llm = None
    search_company_domain_ddg = None
    verify_domain_exists = None

try:
    import phonenumbers
except Exception:  # pragma: no cover - fallback keeps the agent runnable.
    phonenumbers = None


console = Console()

DEFAULT_DATABASE_ID = "291a0c6e61688124bc56c8fbbf8e06c3"

NOTION_DELAY = 0.35
MAX_RETRIES = 3
DEFAULT_TIMEOUT = 8
DEFAULT_PAGES_PER_ACCOUNT = 12

TARGET_STATUSES = {
    "Prospect Qualified",
    "Connect. Request sent",
    "Contact details wrong",
    "Voicemail sent",
    "Nurture",
    "Contacted LinkedIn 🌐",
    "Contacted Mail 📩",
}

COUNTRY_TO_REGION = {
    "austria": "AT",
    "belgium": "BE",
    "canada": "CA",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "namibia": "NA",
    "netherlands": "NL",
    "south korea": "KR",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "tanzania": "TZ",
    "tansania": "TZ",
    "uganda": "UG",
    "united kingdom": "GB",
    "uk": "GB",
    "usa": "US",
    "united states": "US",
    "australia": "AU",
}

REGION_CALLING_CODES = {
    "AT": "43",
    "AU": "61",
    "BE": "32",
    "CA": "1",
    "CH": "41",
    "DE": "49",
    "ES": "34",
    "FI": "358",
    "FR": "33",
    "GB": "44",
    "KR": "82",
    "NA": "264",
    "NL": "31",
    "SE": "46",
    "TZ": "255",
    "UG": "256",
    "US": "1",
}

EU_REGIONS = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "ES",
    "FI",
    "FR",
    "GR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
}

PREFERRED_REGIONS_WITHOUT_COUNTRY = EU_REGIONS | {"CH", "GB"}

PARKED_HOST_KEYWORDS = (
    "domains.atom.com",
    "atom.com",
    "sedo.com",
    "hugedomains.com",
    "dan.com",
    "afternic.com",
    "parkingcrew.net",
    "godaddy.com",
)

PARKED_TEXT_PATTERNS = (
    "domain is for sale",
    "buy this domain",
    "get this domain",
    "lease to own",
    "make an offer",
    "domain marketplace",
)

DIRECT_PATHS = [
    "/impressum",
    "/impressum/",
    "/kontakt",
    "/kontakt/",
    "/contact",
    "/contact/",
    "/contact-us",
    "/contact-us/",
    "/legal-notice",
    "/legal-notice/",
    "/legal",
    "/legal/",
    "/imprint",
    "/imprint/",
    "/de/impressum",
    "/de/kontakt",
    "/en/imprint",
    "/en/contact",
    "/unternehmen/impressum",
    "/unternehmen/kontakt",
    "/about",
    "/about-us",
]

LINK_KEYWORDS = {
    "impressum": 50,
    "imprint": 45,
    "kontakt": 42,
    "contact": 40,
    "telefon": 35,
    "telephone": 35,
    "phone": 35,
    "legal notice": 28,
    "legal-notice": 28,
    "legal": 18,
    "standort": 16,
    "locations": 16,
    "office": 14,
    "about": 8,
}

PHONE_CONTEXT_KEYWORDS = (
    "tel",
    "telefon",
    "telephone",
    "phone",
    "fon",
    "zentrale",
    "office",
    "kontakt",
    "contact",
    "call",
)

NON_PHONE_CONTEXT_KEYWORDS = (
    "iban",
    "bic",
    "bank",
    "spendenkonto",
    "konto",
    "ust-id",
    "ust id",
    "umsatzsteuer",
    "tax id",
    "vat",
    "handelsregister",
    "vereinsregister",
    "registergericht",
    "amtsgericht",
)

PHONE_REGEX = re.compile(
    r"""
    (?:
        (?:\+|00)\s?\d{1,3}(?:[\s()./-]*\d){5,14}\d
        |
        \(?0\d{1,5}\)?(?:[\s()./-]*\d){4,12}\d
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


@dataclass
class Account:
    page_id: str
    notion_url: str
    name: str
    status: str
    countries: list[str]
    city: str
    region: str
    website_urls: list[str]
    existing_phone: str


@dataclass
class FetchedPage:
    url: str
    text: str
    soup: BeautifulSoup


@dataclass
class PhoneCandidate:
    normalized: str
    raw: str
    source_url: str
    context: str
    from_tel_link: bool
    detected_region: str
    score: int = 0


def notion_request(method: str, url: str, json_body: Optional[dict] = None) -> Optional[dict]:
    """Make a Notion API request with retry and rate limiting."""
    headers = _notion_api_headers()

    for attempt in range(MAX_RETRIES):
        time.sleep(NOTION_DELAY)
        try:
            if method == "GET":
                resp = http_requests.get(url, headers=headers, timeout=30)
            elif method == "POST":
                resp = http_requests.post(url, headers=headers, json=json_body or {}, timeout=30)
            elif method == "PATCH":
                resp = http_requests.patch(url, headers=headers, json=json_body or {}, timeout=30)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code in (429, 502, 503):
                wait = (attempt + 1) * 2
                console.print(f"[yellow]Notion {resp.status_code}; retrying in {wait}s[/yellow]")
                time.sleep(wait)
                continue

            try:
                message = resp.json().get("message", resp.text[:200])
            except Exception:
                message = resp.text[:200]
            console.print(f"[red]Notion API error {resp.status_code}: {message}[/red]")
            return None
        except Exception as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep((attempt + 1) * 2)
                continue
            console.print(f"[red]Notion request failed: {exc}[/red]")
            return None

    return None


def fetch_accounts(database_id: str) -> list[dict]:
    """Fetch all pages from the target Notion database."""
    if not NOTION_TOKEN:
        console.print("[red]Error: NOTION_TOKEN not configured[/red]")
        return []

    results: list[dict] = []
    start_cursor = None
    url = f"https://api.notion.com/v1/databases/{database_id}/query"

    while True:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        data = notion_request("POST", url, body)
        if not data:
            break

        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return results


def extract_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(item.get("plain_text", "") for item in prop.get("title", [])).strip()
    return ""


def extract_status(page: dict) -> str:
    prop = page.get("properties", {}).get("Status", {})
    if prop.get("type") == "status" and prop.get("status"):
        return prop["status"].get("name", "") or ""
    return ""


def extract_phone(page: dict, prop_name: str = "Company Phone Number") -> str:
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "phone_number":
        return prop.get("phone_number") or ""
    return ""


def extract_url(page: dict, prop_name: str) -> str:
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "url":
        return prop.get("url") or ""
    return ""


def extract_formula_text(page: dict, prop_name: str) -> str:
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") != "formula":
        return ""
    formula = prop.get("formula") or {}
    ftype = formula.get("type")
    if ftype == "string":
        return formula.get("string") or ""
    if ftype == "number" and formula.get("number") is not None:
        return str(formula.get("number"))
    return ""


def extract_multi_select(page: dict, prop_name: str) -> list[str]:
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") != "multi_select":
        return []
    return [item.get("name", "") for item in prop.get("multi_select", []) if item.get("name")]


def extract_select(page: dict, prop_name: str) -> str:
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "select" and prop.get("select"):
        return prop["select"].get("name", "") or ""
    return ""


def country_to_region(country: str) -> str:
    return COUNTRY_TO_REGION.get(country.strip().lower(), "")


def choose_region(countries: Iterable[str]) -> str:
    """Prefer Germany, then EU regions, then the first mapped country."""
    mapped = [country_to_region(country) for country in countries]
    mapped = [region for region in mapped if region]
    if "DE" in mapped:
        return "DE"
    for region in mapped:
        if region in EU_REGIONS:
            return region
    return mapped[0] if mapped else ""


def infer_region_from_text(name: str, city: str, website_urls: list[str]) -> str:
    text = f"{name} {city}".lower()
    hints = [
        ("CH", ("switzerland", "swiss", "suisse", "schweiz", "winterthur", "zurich", "zürich", "geneva", "genève", "nyon", "vaud", "bern", "basel", "moudon")),
        ("DE", ("germany", "deutschland", "berlin", "munich", "münchen", "bonn", "frankfurt", "hamburg", "cologne", "köln", "duesseldorf", "düsseldorf", "stuttgart", "mannheim", "darmstadt", "potsdam", "karlsruhe", "rosenheim", "osnabrueck", "osnabrück")),
        ("AT", ("austria", "österreich", "osterreich", "vienna", "wien")),
        ("FR", ("france", "paris", "lyon", "marseille")),
        ("ES", ("spain", "españa", "espana", "madrid", "barcelona", "valencia")),
        ("NL", ("netherlands", "nederland", "amsterdam", "rotterdam", "delft")),
        ("BE", ("belgium", "belgique", "brussels", "bruxelles")),
        ("SE", ("sweden", "stockholm")),
        ("FI", ("finland", "helsinki")),
        ("GB", ("united kingdom", "london", "cardiff", "edinburgh")),
    ]
    for region, tokens in hints:
        if any(token in text for token in tokens):
            return region

    tld_regions = {
        ".de": "DE",
        ".ch": "CH",
        ".at": "AT",
        ".fr": "FR",
        ".es": "ES",
        ".nl": "NL",
        ".be": "BE",
        ".se": "SE",
        ".fi": "FI",
        ".uk": "GB",
    }
    for raw_url in website_urls:
        host = normalize_host(urlparse(ensure_url(raw_url)).netloc)
        for suffix, region in tld_regions.items():
            if host.endswith(suffix):
                return region
    return ""


def ensure_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"https://{raw.lstrip('/')}"


def normalize_host(host: str) -> str:
    host = host.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_same_site(candidate_url: str, base_host: str) -> bool:
    parsed = urlparse(candidate_url)
    host = normalize_host(parsed.netloc)
    base = normalize_host(base_host)
    return host == base or host.endswith(f".{base}")


def build_account(page: dict) -> Account:
    countries = extract_multi_select(page, "Country")
    city = extract_select(page, "City")
    website_urls = []

    website = extract_url(page, "Website URL*")
    if website:
        website_urls.append(ensure_url(website))

    domain = extract_formula_text(page, "Domain*")
    if domain and "." in domain:
        website_urls.append(ensure_url(domain))

    deduped = []
    seen = set()
    for url in website_urls:
        if url and url not in seen:
            deduped.append(url)
            seen.add(url)

    return Account(
        page_id=page.get("id", ""),
        notion_url=page.get("url", ""),
        name=extract_title(page),
        status=extract_status(page),
        countries=countries,
        city=city,
        region=choose_region(countries) or infer_region_from_text(extract_title(page), city, deduped),
        website_urls=deduped,
        existing_phone=extract_phone(page),
    )


def is_target_account(account: Account, force: bool = False) -> bool:
    if not account.name:
        return False
    if account.existing_phone and not force:
        return False
    return account.status in TARGET_STATUSES


def clean_company_name_for_search(name: str) -> str:
    name = re.sub(r"(?i)^\s*temp\s*:\s*", "", name or "").strip()
    return name


def guessed_domain_for_company(name: str, region: str) -> str:
    clean = re.sub(r"[^a-z0-9]", "", name.lower())
    if not clean:
        return ""
    if region == "DE":
        return f"{clean}.de"
    return f"{clean}.com"


def discover_website_urls(account: Account) -> list[str]:
    """Best-effort website fallback for accounts missing Website URL*/Domain*."""
    if account.website_urls or not find_valid_domain:
        return []

    search_name = clean_company_name_for_search(account.name)
    try:
        if lookup_domain_with_llm and verify_domain_exists:
            llm_domain = lookup_domain_with_llm(search_name)
            if llm_domain and verify_domain_exists(llm_domain):
                return [ensure_url(llm_domain)]

        if search_company_domain_ddg:
            ddg_domain = search_company_domain_ddg(search_name)
            if ddg_domain:
                return [ensure_url(ddg_domain)]

        guessed = guessed_domain_for_company(search_name, account.region)
        domain = find_valid_domain(search_name, guessed)
    except Exception as exc:
        console.print(f"[dim]  Website fallback failed for {account.name}: {exc}[/dim]")
        return []

    if not domain or "." not in domain or " " in domain:
        return []
    return [ensure_url(domain)]


def fetch_page(session: http_requests.Session, url: str, timeout: int) -> Optional[FetchedPage]:
    try:
        resp = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        if resp.status_code >= 500:
            return None
        content_type = resp.headers.get("content-type", "").lower()
        if content_type and "html" not in content_type and "text" not in content_type:
            return None
        html = resp.text[:500_000]
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        if is_parked_domain_page(resp.url, text):
            return None
        return FetchedPage(url=resp.url, text=re.sub(r"\s+", " ", text), soup=soup)
    except Exception:
        return None


def is_parked_domain_page(url: str, text: str) -> bool:
    host = normalize_host(urlparse(url).netloc)
    if any(keyword in host for keyword in PARKED_HOST_KEYWORDS):
        return True
    lower = text.lower()[:5000]
    return any(pattern in lower for pattern in PARKED_TEXT_PATTERNS)


def link_keyword_score(value: str) -> int:
    value = value.lower()
    return max((score for keyword, score in LINK_KEYWORDS.items() if keyword in value), default=0)


def discover_candidate_urls(homepage: FetchedPage, website_url: str, pages_per_account: int) -> list[str]:
    parsed = urlparse(homepage.url or website_url)
    base_host = parsed.netloc or urlparse(website_url).netloc
    origin = f"{parsed.scheme or 'https'}://{base_host}"

    ranked: list[tuple[int, str]] = []
    for path in DIRECT_PATHS:
        ranked.append((link_keyword_score(path) + 5, urljoin(origin, path)))

    for anchor in homepage.soup.find_all("a"):
        href = anchor.get("href") or ""
        text = anchor.get_text(" ", strip=True)
        joined = urljoin(homepage.url, href)
        if not joined.startswith(("http://", "https://")):
            continue
        if not is_same_site(joined, base_host):
            continue
        score = link_keyword_score(f"{href} {text}")
        if score > 0:
            ranked.append((score, joined))

    deduped: list[str] = []
    seen = {homepage.url.rstrip("/")}
    for _, url in sorted(ranked, key=lambda item: item[0], reverse=True):
        clean = url.split("#", 1)[0].rstrip("/")
        if clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
        if len(deduped) >= pages_per_account:
            break

    return deduped


def strip_phone_noise(raw: str) -> str:
    raw = re.sub(r"(?i)\b(?:ext|extension|durchwahl|dw)\.?\s*\d+\b", "", raw)
    raw = raw.replace("(0)", "")
    return raw.strip(" \t\r\n:;,.|")


def fallback_region_for_number(e164: str) -> str:
    digits = e164.lstrip("+")
    matches = sorted(REGION_CALLING_CODES.items(), key=lambda item: len(item[1]), reverse=True)
    for region, code in matches:
        if digits.startswith(code):
            return region
    return ""


def normalize_phone_number(raw: str, default_region: str) -> tuple[str, str]:
    """Return (E.164 phone number, detected region)."""
    raw = strip_phone_noise(raw)
    if not raw:
        return "", ""

    if phonenumbers:
        try:
            region = None if raw.lstrip().startswith(("+", "00")) else (default_region or None)
            parsed = phonenumbers.parse(raw, region)
            if phonenumbers.is_valid_number(parsed) or phonenumbers.is_possible_number(parsed):
                e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
                detected_region = phonenumbers.region_code_for_number(parsed) or fallback_region_for_number(e164)
                if not detected_region:
                    return "", ""
                return e164, detected_region
        except Exception:
            pass

    compact = re.sub(r"[^\d+]", "", raw)
    if compact.startswith("00"):
        compact = f"+{compact[2:]}"
    if compact.startswith("+"):
        digits = re.sub(r"\D", "", compact)
        if 8 <= len(digits) <= 15:
            e164 = f"+{digits}"
            return e164, fallback_region_for_number(e164)

    digits = re.sub(r"\D", "", compact)
    code = REGION_CALLING_CODES.get(default_region, "")
    if code and digits.startswith("0") and 7 <= len(digits) <= 14:
        e164 = f"+{code}{digits.lstrip('0')}"
        return e164, default_region

    return "", ""


def is_fax_context(context: str, start_offset: int = 0) -> bool:
    del start_offset
    return bool(re.search(r"(?i)\b(?:fax|telefax)\b", context[:40]))


def context_has_phone_keyword(context: str) -> bool:
    lower = context.lower()
    return any(keyword in lower for keyword in PHONE_CONTEXT_KEYWORDS)


def source_has_contact_signal(source_url: str) -> bool:
    lower = source_url.lower()
    return any(
        token in lower
        for token in ("impressum", "kontakt", "contact", "imprint", "legal-notice")
    )


def context_is_likely_non_phone(context: str) -> bool:
    lower = context.lower()
    if "iban" in lower or "bic" in lower:
        return True
    if re.search(r"\b20\d{2}[-./]\d{1,2}[-./]\d{1,2}\b", lower):
        return True
    if re.search(r"\b\d{1,2}[./-]\d{1,2}[./-]20\d{2}\b", lower):
        return True
    if re.search(r"\b20\d{2}[-./]\d{1,2}[-./]\d{1,2}\s+\d{1,2}:\d{2}", lower):
        return True
    has_non_phone_signal = any(keyword in lower for keyword in NON_PHONE_CONTEXT_KEYWORDS)
    return has_non_phone_signal and not context_has_phone_keyword(lower)


def extract_candidates_from_page(page: FetchedPage, account_region: str) -> list[PhoneCandidate]:
    candidates: list[PhoneCandidate] = []

    for anchor in page.soup.find_all("a"):
        href = anchor.get("href") or ""
        if not href.lower().startswith("tel:"):
            continue
        raw = href.split(":", 1)[1].split("?", 1)[0]
        normalized, detected_region = normalize_phone_number(raw, account_region)
        if normalized:
            context = anchor.get_text(" ", strip=True) or raw
            candidates.append(
                PhoneCandidate(
                    normalized=normalized,
                    raw=raw,
                    source_url=page.url,
                    context=context[:200],
                    from_tel_link=True,
                    detected_region=detected_region,
                )
            )

    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.\w+\b", " ", page.text)
    for match in PHONE_REGEX.finditer(text):
        raw = match.group(0)
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 80)
        context = text[start:end].strip()
        near_context = text[max(0, match.start() - 45):min(len(text), match.end() + 45)]
        if is_fax_context(text[max(0, match.start() - 20):match.start() + 20]):
            continue
        if context_is_likely_non_phone(near_context):
            continue
        if not source_has_contact_signal(page.url) and not context_has_phone_keyword(near_context):
            continue
        normalized, detected_region = normalize_phone_number(raw, account_region)
        if not normalized:
            continue
        candidates.append(
            PhoneCandidate(
                normalized=normalized,
                raw=raw,
                source_url=page.url,
                context=context[:240],
                from_tel_link=False,
                detected_region=detected_region,
            )
        )

    return candidates


def score_candidate(candidate: PhoneCandidate, account_region: str) -> int:
    score = 0
    source = candidate.source_url.lower()
    context = candidate.context.lower()

    if candidate.from_tel_link:
        score += 45
    if "impressum" in source:
        score += 45
    if "kontakt" in source:
        score += 38
    if "contact" in source:
        score += 35
    if "imprint" in source or "legal-notice" in source:
        score += 30
    if context_has_phone_keyword(context):
        score += 24
    if re.search(r"(?i)\b(?:fax|telefax)\b", context):
        score -= 30

    if account_region and candidate.detected_region == account_region:
        score += 55
    elif candidate.detected_region == "DE":
        score += 26
    elif candidate.detected_region in EU_REGIONS:
        score += 18
    elif candidate.detected_region in PREFERRED_REGIONS_WITHOUT_COUNTRY:
        score += 18
    elif not account_region and candidate.detected_region not in PREFERRED_REGIONS_WITHOUT_COUNTRY:
        score -= 80

    expected_code = REGION_CALLING_CODES.get(account_region)
    if expected_code and candidate.normalized.startswith(f"+{expected_code}"):
        score += 35

    digits = re.sub(r"\D", "", candidate.normalized)
    if not (8 <= len(digits) <= 15):
        score -= 100

    return score


def choose_best_candidate(candidates: list[PhoneCandidate], account_region: str) -> Optional[PhoneCandidate]:
    deduped: dict[str, PhoneCandidate] = {}
    for candidate in candidates:
        candidate.score = score_candidate(candidate, account_region)
        current = deduped.get(candidate.normalized)
        if not current or candidate.score > current.score:
            deduped[candidate.normalized] = candidate

    if not deduped:
        return None

    ranked = sorted(deduped.values(), key=lambda item: item.score, reverse=True)
    best = ranked[0]
    if best.score < 20:
        return None
    return best


def find_company_phone(
    account: Account,
    session: http_requests.Session,
    pages_per_account: int,
    timeout: int,
) -> Optional[PhoneCandidate]:
    all_candidates: list[PhoneCandidate] = []

    for website_url in account.website_urls:
        homepage = fetch_page(session, website_url, timeout)
        if not homepage and website_url.startswith("https://"):
            homepage = fetch_page(session, website_url.replace("https://", "http://", 1), timeout)
        if not homepage:
            continue

        pages = [homepage]
        for candidate_url in discover_candidate_urls(homepage, website_url, pages_per_account):
            fetched = fetch_page(session, candidate_url, timeout)
            if fetched:
                pages.append(fetched)

        for fetched_page in pages:
            all_candidates.extend(extract_candidates_from_page(fetched_page, account.region))

        best = choose_best_candidate(all_candidates, account.region)
        if best and (
            best.detected_region == account.region
            or best.detected_region == "DE"
            or "impressum" in best.source_url.lower()
            or "kontakt" in best.source_url.lower()
        ):
            return best

    return choose_best_candidate(all_candidates, account.region)


def update_company_phone(page_id: str, phone: str) -> bool:
    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = notion_request(
        "PATCH",
        url,
        {"properties": {"Company Phone Number": {"phone_number": phone}}},
    )
    return data is not None


def log_record(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def render_summary_table(rows: list[dict]) -> None:
    table = Table(title="Company Phone Enrichment")
    table.add_column("Company", max_width=34)
    table.add_column("Status", max_width=22)
    table.add_column("Country", max_width=18)
    table.add_column("Phone", style="green")
    table.add_column("Result", max_width=16)

    for row in rows[-30:]:
        table.add_row(
            row.get("company", "")[:34],
            row.get("status", "")[:22],
            ", ".join(row.get("countries", []))[:18],
            row.get("phone", ""),
            row.get("result", ""),
        )

    console.print(table)


def run(
    database_id: str,
    dry_run: bool,
    limit: int,
    pages_per_account: int,
    timeout: int,
    force: bool,
) -> int:
    console.print("[bold magenta]Company Phone Enrichment Agent[/bold magenta]")
    if dry_run:
        console.print("[yellow]DRY RUN - Notion will not be updated[/yellow]")

    pages = fetch_accounts(database_id)
    accounts = [build_account(page) for page in pages]
    target_accounts = [account for account in accounts if is_target_account(account, force=force)]
    if limit:
        target_accounts = target_accounts[:limit]

    skipped_existing = sum(1 for account in accounts if account.existing_phone and account.status in TARGET_STATUSES)
    skipped_status = sum(1 for account in accounts if not account.existing_phone and account.status not in TARGET_STATUSES)
    missing_website_property = sum(1 for account in target_accounts if not account.website_urls)

    console.print(f"[cyan]Fetched accounts:[/cyan] {len(accounts)}")
    console.print(f"[cyan]Skipped with existing phone in target statuses:[/cyan] {skipped_existing}")
    console.print(f"[cyan]Skipped outside target statuses without phone:[/cyan] {skipped_status}")
    console.print(f"[cyan]Accounts to scan:[/cyan] {len(target_accounts)}")
    if missing_website_property:
        console.print(f"[yellow]Accounts to scan with no website URL/domain property:[/yellow] {missing_website_property}")

    session = http_requests.Session()
    log_path = LOGS_DIR / f"company_phone_enrichment_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"

    stats = {
        "updated": 0,
        "found_dry_run": 0,
        "not_found": 0,
        "no_website": 0,
        "failed_update": 0,
    }
    rows: list[dict] = []

    for idx, account in enumerate(target_accounts, start=1):
        console.print(f"[dim]{idx}/{len(target_accounts)} Scanning {account.name}[/dim]")
        base_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "page_id": account.page_id,
            "notion_url": account.notion_url,
            "company": account.name,
            "status": account.status,
            "countries": account.countries,
            "region": account.region,
            "websites": account.website_urls,
        }

        discovered_urls = []
        if not account.website_urls:
            discovered_urls = discover_website_urls(account)
            account.website_urls.extend(discovered_urls)

        if not account.website_urls:
            stats["no_website"] += 1
            record = {**base_record, "result": "no_website", "phone": ""}
            rows.append(record)
            log_record(log_path, record)
            continue

        if discovered_urls:
            base_record["websites"] = account.website_urls
            base_record["website_fallback"] = discovered_urls

        candidate = find_company_phone(account, session, pages_per_account, timeout)
        if not candidate:
            stats["not_found"] += 1
            record = {**base_record, "result": "not_found", "phone": ""}
            rows.append(record)
            log_record(log_path, record)
            continue

        if dry_run:
            stats["found_dry_run"] += 1
            result = "would_update"
            ok = True
        else:
            ok = update_company_phone(account.page_id, candidate.normalized)
            result = "updated" if ok else "failed_update"
            stats["updated" if ok else "failed_update"] += 1

        record = {
            **base_record,
            "result": result,
            "phone": candidate.normalized,
            "raw_phone": candidate.raw,
            "source_url": candidate.source_url,
            "detected_region": candidate.detected_region,
            "score": candidate.score,
            "context": candidate.context,
        }
        rows.append(record)
        log_record(log_path, record)

        if ok:
            console.print(f"[green]  {result}: {candidate.normalized}[/green] [dim]{candidate.source_url}[/dim]")
        else:
            console.print(f"[red]  Failed to update {account.name}[/red]")

    render_summary_table(rows)
    console.print(
        "[bold green]Done.[/bold green] "
        f"Updated: {stats['updated']}, "
        f"Would update: {stats['found_dry_run']}, "
        f"Not found: {stats['not_found']}, "
        f"No website: {stats['no_website']}, "
        f"Failed updates: {stats['failed_update']}"
    )
    console.print(f"[dim]Log: {log_path}[/dim]")

    return 0 if stats["failed_update"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Company Phone Number for Notion Accounts")
    parser.add_argument("--database-id", default=DEFAULT_DATABASE_ID, help="Notion Accounts database ID")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Notion")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of target accounts to scan")
    parser.add_argument(
        "--pages-per-account",
        type=int,
        default=DEFAULT_PAGES_PER_ACCOUNT,
        help="Maximum non-homepage URLs to scan per account",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout per page in seconds")
    parser.add_argument("--force", action="store_true", help="Re-scan accounts even if phone is already present")
    args = parser.parse_args()

    return run(
        database_id=args.database_id,
        dry_run=args.dry_run,
        limit=args.limit,
        pages_per_account=max(1, args.pages_per_account),
        timeout=max(3, args.timeout),
        force=args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
