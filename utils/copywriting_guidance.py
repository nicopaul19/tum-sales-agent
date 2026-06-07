"""
Copywriting guidance helpers shared by outreach agents.

Loads department-specific best-practice examples from the Notion copywriting
database and provides a compact humanizer pass adapted from blader/humanizer.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Optional

import requests

from utils.config import NOTION_TOKEN

NOTION_API_VERSION = "2022-06-28"
DEFAULT_BEST_PRACTICES_DB_ID = "04303f37-bd94-4c69-801d-f21f3c73483c"
BEST_PRACTICES_DB_ID = (
    os.getenv("NOTION_DB_COPYWRITING_BEST_PRACTICES_ID")
    or os.getenv("COPYWRITING_BEST_PRACTICES_DB_ID")
    or DEFAULT_BEST_PRACTICES_DB_ID
)

HUMANIZER_PROMPT = """
## HUMANIZER PASS
Before returning final outreach copy, silently do a second-pass edit based on the blader/humanizer skill:
- Remove AI-sounding filler and inflated wording: crucial, pivotal, enhance, foster, showcase, valuable, vibrant, innovative, synergy, align with, at its core, the real question is.
- Prefer simple human phrasing over press-release phrasing. Use "is", "has", and concrete nouns instead of "serves as", "stands as", "boasts", or abstract "landscape" language.
- Cut formulaic structures: not just X but Y, rule-of-three lists, "let's explore", "here's what you need to know", and generic upbeat conclusions.
- Avoid vague significance claims and superficial "-ing" add-ons such as "highlighting", "underscoring", "reflecting", "showcasing", or "fostering".
- Keep the rhythm natural: vary sentence length, leave one clear thought per sentence, and make the message sound written by a specific human to a specific recipient.
- Do not use em dashes, en dashes, emojis, bold formatting, or chatbot artifacts.
- Preserve every factual constraint from the brief. Never invent company, NGO, partner, funding, hiring, or event facts.
""".strip()


def _normalize_notion_id(value: str) -> str:
    compact = re.sub(r"[^0-9a-fA-F]", "", value or "")
    if len(compact) != 32:
        return value
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }


def _plain_text(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype in {"title", "rich_text"}:
        return "".join(t.get("plain_text", "") for t in prop.get(ptype, [])).strip()
    if ptype == "select":
        selected = prop.get("select")
        return (selected or {}).get("name", "").strip()
    if ptype == "multi_select":
        return ", ".join(item.get("name", "") for item in prop.get("multi_select", [])).strip()
    if ptype == "date":
        date = prop.get("date")
        return (date or {}).get("start", "").strip()
    return ""


@lru_cache(maxsize=8)
def load_best_practices_prompt(department: str, max_items: int = 20) -> str:
    """Return a prompt block with human-reviewed Notion best practices."""
    if not NOTION_TOKEN or not BEST_PRACTICES_DB_ID:
        return ""

    db_id = _normalize_notion_id(BEST_PRACTICES_DB_ID)
    payload = {
        "page_size": min(max_items, 100),
        "sorts": [{"property": "Processed on", "direction": "descending"}],
    }
    if department:
        payload["filter"] = {
            "property": "Department",
            "select": {"equals": department},
        }

    try:
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=_headers(),
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            return ""
        rows = resp.json().get("results", [])
    except Exception:
        return ""

    examples = []
    for row in rows[:max_items]:
        props = row.get("properties", {})
        best = _plain_text(props.get("Best practice writing"))
        reason = _plain_text(props.get("Reason for improvement"))
        bad = _plain_text(props.get("Bad (AI) writing snippet"))
        status = _plain_text(props.get("Processed status"))

        if not best and not reason:
            continue
        if status and status.lower() not in {"processed", "approved", "done"} and not best:
            continue

        parts = []
        if reason:
            parts.append(f"Lesson: {reason}")
        if bad:
            parts.append(f"Avoid: {bad}")
        if best:
            parts.append(f"Prefer: {best}")
        examples.append(" | ".join(parts))

    if not examples:
        return ""

    dept_label = department or "all departments"
    lines = [
        f"## HUMAN-REVIEWED COPYWRITING BEST PRACTICES ({dept_label})",
        "These are reusable lessons from the Notion copywriting improvement database. Apply the lesson, but do not copy an example unless the same context genuinely fits.",
    ]
    lines.extend(f"{i}. {example}" for i, example in enumerate(examples, 1))
    return "\n\n" + "\n".join(lines)


@lru_cache(maxsize=8)
def load_humanized_guidance(department: str) -> str:
    """Return all live copywriting guidance to append to a system prompt."""
    guidance = "\n\n" + HUMANIZER_PROMPT
    best_practices = load_best_practices_prompt(department)
    if best_practices:
        guidance += best_practices
    return guidance
