"""
Iterations Client — reads copywriter best-practice iterations from Notion.

Page: Iterations on Strategic Partnerships Copywriter Agent
URL:  https://www.notion.so/Iterations-on-Strategic-Partnersh-Copywriter-Agent-366a0c6e616880f8ba37ffa95d90b2fa

Structure:
  ├─ [callout] Instructions
  ├─ [toggle] Not yet processed ❌
  │     └─ [toggle] <Issue title>
  │           ├─ Bad example: ...
  │           └─ Better version: ...
  └─ [toggle] Processed ✅
        └─ (moved items land here)

Usage:
    from utils.iterations_client import load_iterations, mark_iterations_processed
"""
import time
from datetime import datetime
from typing import Optional

import requests as http_requests
from rich.console import Console

from utils.config import NOTION_TOKEN

console = Console()

NOTION_API_VERSION = "2022-06-28"
ITERATIONS_PAGE_ID = "366a0c6e-6168-80f8-ba37-ffa95d90b2fa"
UNPROCESSED_TOGGLE_ID = "366a0c6e-6168-80fb-ae25-e8ade35c89e2"
PROCESSED_TOGGLE_ID = "366a0c6e-6168-80eb-a465-d8346ca9924c"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }


def _get_block_children(block_id: str) -> list:
    """Fetch all children of a block."""
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    results = []
    start_cursor = None

    while True:
        params = {"page_size": 100}
        if start_cursor:
            params["start_cursor"] = start_cursor

        resp = http_requests.get(url, headers=_headers(), params=params, timeout=30)
        if resp.status_code != 200:
            console.print(f"[red]Notion block fetch error {resp.status_code}: {resp.json().get('message', '')}[/red]")
            return results

        data = resp.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return results


def _extract_plain_text(rich_text_array: list) -> str:
    """Extract plain text from a Notion rich_text array."""
    return "".join(t.get("plain_text", "") for t in rich_text_array)


def _block_to_text(block: dict) -> str:
    """Convert a single Notion block to a plain text string."""
    btype = block.get("type", "")
    content = block.get(btype, {})
    rich_text = content.get("rich_text", [])
    return _extract_plain_text(rich_text)


def load_iterations() -> tuple[str, list[str]]:
    """
    Fetch all unprocessed iterations from the Notion page.

    Returns:
        (prompt_injection, block_ids) where:
          - prompt_injection: formatted string to inject into the copywriter prompt
          - block_ids: list of top-level block IDs inside "Not yet processed ❌"
                       (used later to mark them as processed)
    """
    if not NOTION_TOKEN:
        return "", []

    items = _get_block_children(UNPROCESSED_TOGGLE_ID)
    if not items:
        return "", []

    # Filter out template/empty items
    real_items = [
        b for b in items
        if b.get("type") == "toggle"
        and "template" not in _block_to_text(b).lower()
        and "not yet processed" not in _block_to_text(b).lower()
    ]

    if not real_items:
        console.print("[dim]No new iterations to inject from Notion.[/dim]")
        return "", []

    console.print(f"[cyan]Found {len(real_items)} unprocessed iteration(s) to inject into prompt.[/cyan]")

    sections = []
    block_ids = []

    for item in real_items:
        block_ids.append(item["id"])
        title = _block_to_text(item)

        # Fetch children of this toggle (bad example + improved version)
        children = _get_block_children(item["id"])
        child_lines = []
        for child in children:
            text = _block_to_text(child)
            if text.strip():
                child_lines.append(f"  {text.strip()}")

        section = f"Issue: {title}"
        if child_lines:
            section += "\n" + "\n".join(child_lines)
        sections.append(section)

    prompt_injection = (
        "\n\n---\nRECENT QUALITY ITERATIONS (apply these to avoid past mistakes):\n"
        + "\n\n".join(sections)
        + "\n---"
    )

    return prompt_injection, block_ids


def mark_iterations_processed(block_ids: list[str]) -> bool:
    """
    Move processed iteration blocks into the "Processed ✅" toggle.

    For each block:
    1. Read its full content (title + children)
    2. Append recreated content to "Processed ✅" toggle
    3. Archive the original block

    Args:
        block_ids: List of block IDs from "Not yet processed ❌" to move.

    Returns:
        True if all were moved successfully.
    """
    if not NOTION_TOKEN or not block_ids:
        return True

    success = True
    processed_date = datetime.now().strftime("%Y-%m-%d")

    for block_id in block_ids:
        try:
            # Fetch original block
            resp = http_requests.get(
                f"https://api.notion.com/v1/blocks/{block_id}",
                headers=_headers(),
                timeout=30,
            )
            if resp.status_code != 200:
                console.print(f"[yellow]Could not fetch block {block_id[:8]}: {resp.status_code}[/yellow]")
                success = False
                continue

            block = resp.json()
            title = _block_to_text(block)
            children = _get_block_children(block_id)

            # Build child blocks to recreate
            child_blocks = []
            for child in children:
                ctype = child.get("type", "")
                content = child.get(ctype, {})
                rich_text = content.get("rich_text", [])
                if not rich_text:
                    continue
                child_blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": _extract_plain_text(rich_text)}}]
                    }
                })

            # Append to "Processed ✅" toggle as a new nested toggle
            new_toggle = {
                "object": "block",
                "type": "toggle",
                "toggle": {
                    "rich_text": [{"type": "text", "text": {"content": f"{title} [processed {processed_date}]"}}],
                    "color": "default",
                    "children": child_blocks if child_blocks else [
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "(no details)"}}]}
                        }
                    ]
                }
            }

            append_resp = http_requests.patch(
                f"https://api.notion.com/v1/blocks/{PROCESSED_TOGGLE_ID}/children",
                headers=_headers(),
                json={"children": [new_toggle]},
                timeout=30,
            )
            if append_resp.status_code != 200:
                console.print(f"[yellow]Could not append to Processed toggle: {append_resp.status_code}[/yellow]")
                success = False
                continue

            # Archive the original block
            archive_resp = http_requests.patch(
                f"https://api.notion.com/v1/blocks/{block_id}",
                headers=_headers(),
                json={"archived": True},
                timeout=30,
            )
            if archive_resp.status_code == 200:
                console.print(f"  [green]Iteration '{title[:50]}' moved to Processed ✅[/green]")
            else:
                console.print(f"  [yellow]Appended but couldn't archive original: {archive_resp.status_code}[/yellow]")

            time.sleep(0.35)

        except Exception as e:
            console.print(f"[red]Error processing iteration {block_id[:8]}: {e}[/red]")
            success = False

    return success
