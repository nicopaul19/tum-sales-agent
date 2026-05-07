"""
LinkedIn Parser — Parse LinkedIn Connections HTML pages.

Extracts connection lists from manually saved My Network HTML files.
Uses multiple parsing strategies with graceful fallback.

Note: LinkedIn messaging content is loaded via JavaScript and cannot be
extracted from saved HTML. Only connections/network pages are supported.

Standalone test:
    python -m agents.linkedin_parser
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup, Tag
from rich.console import Console
from rich.table import Table

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import LINKEDIN_DUMP_DIR

console = Console()
GENERIC_LINK_TEXT = {
    "message",
    "nachricht",
    "connect",
    "vernetzen",
    "follow",
    "folgen",
}


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class ParsedConnection:
    name: str
    profile_url: str


# =============================================================================
# URL Normalization
# =============================================================================

def _normalize_linkedin_url(url: str) -> str:
    """Normalize a LinkedIn profile URL to canonical form: https://www.linkedin.com/in/{slug}"""
    if not url:
        return ""

    url = url.strip()

    # Extract the /in/slug part
    match = re.search(r'linkedin\.com/in/([^/?#\s]+)', url)
    if match:
        slug = match.group(1).rstrip("/").lower()
        return f"https://www.linkedin.com/in/{slug}"

    # If it's already a relative /in/ path
    match = re.match(r'^/?in/([^/?#\s]+)', url)
    if match:
        slug = match.group(1).rstrip("/").lower()
        return f"https://www.linkedin.com/in/{slug}"

    return url.strip()


def _clean_connection_name(name: str) -> str:
    """Return a plausible contact name, or an empty string for generic action text."""
    name = re.sub(r"\s+", " ", name or "").strip()
    return "" if name.lower() in GENERIC_LINK_TEXT else name


# =============================================================================
# Connections HTML Parser
# =============================================================================

def parse_connections_html(path: Path) -> Tuple[List[ParsedConnection], List[str]]:
    """
    Parse a LinkedIn My Network / Connections HTML file.
    Uses multiple strategies with fallback.
    """
    warnings = []

    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return [], [f"Could not read {path}: {e}"]

    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: data-view-name attributes (2026+ LinkedIn)
    connections, w = _connections_strategy_data_view(soup)
    warnings.extend(w)
    if connections:
        return connections, warnings

    # Strategy 2: Legacy class-based selectors
    connections, w = _connections_strategy_class(soup)
    warnings.extend(w)
    if connections:
        return connections, warnings

    # Strategy 3: Structural fallback
    connections, w = _connections_strategy_structural(soup)
    warnings.extend(w)
    if connections:
        return connections, warnings

    warnings.append(f"No connections found in {path.name} with any parsing strategy")
    return [], warnings


def _connections_strategy_data_view(soup: BeautifulSoup) -> Tuple[List[ParsedConnection], List[str]]:
    """Parse connections using data-view-name attributes (2026+ LinkedIn layout)."""
    connections = []
    seen_urls = set()

    # Each connection is wrapped in a data-view-name="connections-list" container
    containers = soup.find_all(attrs={"data-view-name": "connections-list"})
    if not containers:
        return [], []

    for container in containers:
        # Profile link has data-view-name="connections-profile" and text content
        profile_links = container.find_all("a", attrs={"data-view-name": "connections-profile"})

        url = ""
        name = ""
        for link in profile_links:
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if href and "/in/" in href and not url:
                url = href
            # The link with the longest text has name + headline concatenated
            # The inner <a> without data-view-name has just the name
            if text and not name:
                inner_link = link.select_one("a[href*='/in/']")
                if inner_link:
                    name = inner_link.get_text(strip=True)
                else:
                    name = text

        if not url:
            continue
        profile_url = _normalize_linkedin_url(url)
        if not profile_url or profile_url in seen_urls:
            continue
        seen_urls.add(profile_url)

        name = _clean_connection_name(name)
        if not name:
            name = profile_url.split("/in/")[-1].replace("-", " ").title()

        connections.append(ParsedConnection(name=name, profile_url=profile_url))

    return connections, []


def _connections_strategy_class(soup: BeautifulSoup) -> Tuple[List[ParsedConnection], List[str]]:
    """Parse connections using legacy LinkedIn class selectors (pre-2026)."""
    connections = []
    seen_urls = set()

    # LinkedIn connection cards
    cards = soup.select(
        ".mn-connection-card, "
        ".mn-connection-card__link, "
        "[class*='connection-card'], "
        ".search-result__info, "
        "[class*='invitation-card']"
    )

    if not cards:
        return [], []

    for card in cards:
        # Name
        name_el = card.select_one(
            ".mn-connection-card__name, "
            "[class*='connection-card__name'], "
            "[class*='actor-name'], "
            "h3, span.name"
        )
        name = _clean_connection_name(name_el.get_text(strip=True) if name_el else "")
        if not name:
            continue

        # Profile URL
        link_el = card if card.name == "a" and card.get("href") else card.select_one("a[href*='/in/']")
        if not link_el or not link_el.get("href"):
            continue
        profile_url = _normalize_linkedin_url(link_el["href"])
        if not profile_url or profile_url in seen_urls:
            continue
        seen_urls.add(profile_url)

        connections.append(ParsedConnection(name=name, profile_url=profile_url))

    return connections, []


def _connections_strategy_structural(soup: BeautifulSoup) -> Tuple[List[ParsedConnection], List[str]]:
    """Structural fallback: find all /in/ links as connections."""
    connections = []
    seen_urls = set()
    warnings = []

    profile_links = soup.find_all("a", href=re.compile(r'/in/[^/]+'))

    for link in profile_links:
        href = link.get("href", "")
        profile_url = _normalize_linkedin_url(href)
        if not profile_url or profile_url in seen_urls:
            continue

        # Skip message/action buttons. Do this before marking the URL as seen,
        # because LinkedIn often repeats the same profile URL on the real name link.
        aria_label = link.get("aria-label", "")
        link_text = _clean_connection_name(link.get_text(" ", strip=True))
        if not link_text and aria_label.lower() in GENERIC_LINK_TEXT:
            continue

        name = link_text
        if not name or len(name) > 100 or len(name) < 2:
            parent = link.parent
            if parent:
                name_el = parent.select_one("h3, h4, span, strong")
                name = _clean_connection_name(name_el.get_text(" ", strip=True) if name_el else "")
        if not name or len(name) < 2:
            continue

        seen_urls.add(profile_url)
        connections.append(ParsedConnection(name=name, profile_url=profile_url))

    if connections:
        warnings.append(f"Used structural fallback parser, found {len(connections)} connections")

    return connections, warnings


# =============================================================================
# Main Entry Point
# =============================================================================

def parse_connections(
    connections_path: Optional[Path] = None,
) -> Tuple[List[ParsedConnection], List[str]]:
    """
    Parse LinkedIn connections HTML from the linkedin dump directory.

    Args:
        connections_path: Path to connections HTML file (auto-detected if None)

    Returns:
        (connections_list, warnings)
    """
    dump_dir = LINKEDIN_DUMP_DIR

    if not dump_dir.exists():
        return [], [f"LinkedIn dump directory does not exist: {dump_dir}"]

    # Auto-detect file if not specified
    if connections_path is None:
        candidates = (
            list(dump_dir.glob("*connection*"))
            + list(dump_dir.glob("*network*"))
            + list(dump_dir.glob("*inbox*"))
        )
        html_candidates = [f for f in candidates if f.suffix.lower() in (".html", ".htm")]
        if html_candidates:
            connections_path = max(html_candidates, key=lambda f: f.stat().st_mtime)
            console.print(f"[dim]Auto-detected connections file: {connections_path.name}[/dim]")
        else:
            return [], ["No HTML file found in linkedin_dump/ (expected *network* or *connection* pattern)"]

    if not connections_path.exists():
        return [], [f"Connections file not found: {connections_path}"]

    console.print(f"[cyan]Parsing connections: {connections_path.name}[/cyan]")
    return parse_connections_html(connections_path)


# =============================================================================
# Standalone Test
# =============================================================================

if __name__ == "__main__":
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]LinkedIn Parser — Standalone Test[/bold magenta]")
    console.print("=" * 60)

    connections, warnings = parse_connections()

    # Warnings
    if warnings:
        console.print(f"\n[yellow]Warnings ({len(warnings)}):[/yellow]")
        for w in warnings:
            console.print(f"  [yellow]- {w}[/yellow]")

    # Connections
    if connections:
        table = Table(title=f"Connections ({len(connections)})")
        table.add_column("Name", max_width=30)
        table.add_column("Profile URL", max_width=50)

        for c in connections[:20]:
            table.add_row(c.name[:30], c.profile_url[:50])

        if len(connections) > 20:
            console.print(f"[dim]  ... and {len(connections) - 20} more[/dim]")
        console.print(table)
    else:
        console.print("[dim]No connections found[/dim]")

    console.print(f"\n[bold green]Parse complete: {len(connections)} connections[/bold green]")
