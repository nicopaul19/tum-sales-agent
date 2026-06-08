"""
Campaign Tracker sync for the Notion CRM.

Builds one Campaign Tracker entry per Account `Campaign ID` and keeps the
campaign-level trigger, target audience, reasoning, A/B stats, and engagement
counts current. The Campaign Tracker database relates only to Accounts.
"""
from __future__ import annotations

import argparse
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

import requests as http_requests
from rich.console import Console
from rich.table import Table

from utils.config import (
    NOTION_TOKEN,
    NOTION_DB_ACCOUNTS_ID,
    NOTION_DB_CONTACTS_ID,
    NOTION_DB_CAMPAIGNS_ID,
)
from utils.notion_client import NOTION_API_VERSION

try:
    from agents.notion_cleanup import STATUS_HIERARCHY
except Exception:
    STATUS_HIERARCHY = [
        "Prospect Qualified",
        "Connect. Request sent",
        "Contact details wrong",
        "Voicemail sent",
        "Nurture",
        "Contacted LinkedIn \U0001f310",
        "Contacted Mail \U0001f4e9",
        "Engaged",
        "Awaiting Callback",
        "Discovery Call Booked",
        "Partnership in Discovery",
        "Partnership next Semesters",
        "Prospect Unqualified",
        "Mentorship confirmed",
        "Partnership started",
        "Partnership finished",
    ]


console = Console()

ENGAGED_INDEX = 7
UNQUALIFIED_INDEX = 12
STALE_DAYS = 30
MAX_RELATION_ACCOUNTS = 100


def _normalize_notion_id(value: str | None) -> str:
    raw = re.sub(r"[^0-9a-fA-F]", "", value or "")
    if len(raw) != 32:
        return value or ""
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def _notion_request(method: str, path: str, body: dict | None = None) -> dict:
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN is not configured")
    url = path if path.startswith("http") else f"https://api.notion.com{path}"
    if method == "GET":
        resp = http_requests.get(url, headers=_headers(), timeout=30)
    elif method == "POST":
        resp = http_requests.post(url, headers=_headers(), json=body or {}, timeout=30)
    elif method == "PATCH":
        resp = http_requests.patch(url, headers=_headers(), json=body or {}, timeout=30)
    else:
        raise ValueError(f"Unsupported Notion method: {method}")
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message", resp.text)
        except Exception:
            message = resp.text
        raise RuntimeError(f"Notion {resp.status_code}: {message}")
    return resp.json()


def _query_database(database_id: str, query_filter: dict | None = None) -> list[dict]:
    pages: list[dict] = []
    cursor = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if query_filter:
            body["filter"] = query_filter
        if cursor:
            body["start_cursor"] = cursor
        data = _notion_request("POST", f"/v1/databases/{database_id}/query", body)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            return pages
        cursor = data.get("next_cursor")


def _find_prop(props: dict, names: Iterable[str]) -> dict:
    for name in names:
        if name in props:
            return props.get(name, {})
    return {}


def _plain_text(prop: dict) -> str:
    prop_type = prop.get("type", "")
    if prop_type == "title":
        return "".join(item.get("plain_text", "") for item in prop.get("title", []))
    if prop_type == "rich_text":
        return "".join(item.get("plain_text", "") for item in prop.get("rich_text", []))
    if prop_type == "select" and prop.get("select"):
        return prop["select"].get("name", "")
    if prop_type == "status" and prop.get("status"):
        return prop["status"].get("name", "")
    if prop_type == "multi_select":
        return ", ".join(item.get("name", "") for item in prop.get("multi_select", []))
    if prop_type == "url":
        return prop.get("url") or ""
    if prop_type == "email":
        return prop.get("email") or ""
    if prop_type == "phone_number":
        return prop.get("phone_number") or ""
    if prop_type == "number":
        value = prop.get("number")
        return "" if value is None else str(value)
    if prop_type == "formula":
        formula = prop.get("formula", {})
        formula_type = formula.get("type")
        value = formula.get(formula_type)
        return "" if value is None else str(value)
    if prop_type == "rollup":
        rollup = prop.get("rollup", {})
        if rollup.get("type") == "array":
            return ", ".join(filter(None, (_plain_text(item) for item in rollup.get("array", []))))
        value = rollup.get(rollup.get("type", ""))
        return "" if value is None else str(value)
    return ""


def _title_from_props(props: dict) -> str:
    for prop in props.values():
        if prop.get("type") == "title":
            return _plain_text(prop)
    return ""


def _names(prop: dict) -> list[str]:
    prop_type = prop.get("type", "")
    if prop_type == "multi_select":
        return [item.get("name", "") for item in prop.get("multi_select", []) if item.get("name")]
    if prop_type in {"select", "status"}:
        name = _plain_text(prop)
        return [name] if name else []
    if prop_type == "rollup":
        rollup = prop.get("rollup", {})
        values: list[str] = []
        if rollup.get("type") == "array":
            for item in rollup.get("array", []):
                values.extend(_names(item))
        else:
            text = _plain_text(prop)
            values.extend([part.strip() for part in text.split(",") if part.strip()])
        return list(dict.fromkeys(values))
    text = _plain_text(prop)
    return [text] if text else []


def _relation_ids(prop: dict) -> list[str]:
    if prop.get("type") != "relation":
        return []
    return [item.get("id", "") for item in prop.get("relation", []) if item.get("id")]


def _number(prop: dict) -> float | None:
    if prop.get("type") == "number":
        return prop.get("number")
    return None


def _truncate(text: str, limit: int = 1900) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _rich_text(text: str) -> dict:
    text = _truncate(text)
    return {"rich_text": [{"text": {"content": text}}]} if text else {"rich_text": []}


def _title(text: str) -> dict:
    return {"title": [{"text": {"content": _truncate(text, 1900)}}]}


def _date(value: str | None) -> dict:
    return {"date": {"start": value}} if value else {"date": None}


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _title_property(database: dict) -> str:
    for name, prop in database.get("properties", {}).items():
        if prop.get("type") == "title":
            return name
    return "Name"


def ensure_campaign_tracker_schema(dry_run: bool = False) -> str:
    """Ensure Campaign Tracker has the properties needed for CRM reporting."""
    campaigns_db = _normalize_notion_id(NOTION_DB_CAMPAIGNS_ID)
    accounts_db = _normalize_notion_id(NOTION_DB_ACCOUNTS_ID)
    if not campaigns_db:
        raise RuntimeError("NOTION_DB_CAMPAIGNS_ID is not configured")
    if not accounts_db:
        raise RuntimeError("NOTION_DB_ACCOUNTS_ID is not configured")

    database = _notion_request("GET", f"/v1/databases/{campaigns_db}")
    existing = database.get("properties", {})
    missing: dict[str, dict] = {}

    wanted = {
        "Campaign ID": {"rich_text": {}},
        "Campaign Type": {
            "select": {
                "options": [
                    {"name": "Strategic Partnerships", "color": "blue"},
                    {"name": "Social Partnerships", "color": "green"},
                    {"name": "Mixed/Other", "color": "gray"},
                ]
            }
        },
        "Campaign Trigger": {"rich_text": {}},
        "Target Audience": {"rich_text": {}},
        "Targeting Reasoning": {"rich_text": {}},
        "Outreach Summary": {"rich_text": {}},
        "CRM Notes": {"rich_text": {}},
        "Accounts Count": {"number": {"format": "number"}},
        "Contacts Count": {"number": {"format": "number"}},
        "Engaged Contacts": {"number": {"format": "number"}},
        "Not Engaged Contacts": {"number": {"format": "number"}},
        "Pending Contacts": {"number": {"format": "number"}},
        "Failed Contacts": {"number": {"format": "number"}},
        "Success Rate": {"number": {"format": "percent"}},
        "A Total": {"number": {"format": "number"}},
        "A Engaged": {"number": {"format": "number"}},
        "A Not Engaged": {"number": {"format": "number"}},
        "A Success Rate": {"number": {"format": "percent"}},
        "B Total": {"number": {"format": "number"}},
        "B Engaged": {"number": {"format": "number"}},
        "B Not Engaged": {"number": {"format": "number"}},
        "B Success Rate": {"number": {"format": "percent"}},
        "A/B Winner": {
            "select": {
                "options": [
                    {"name": "A", "color": "green"},
                    {"name": "B", "color": "blue"},
                    {"name": "Inconclusive", "color": "yellow"},
                    {"name": "No Data", "color": "gray"},
                ]
            }
        },
        "First Seen": {"date": {}},
        "Last Synced": {"date": {}},
        "Sync Status": {
            "select": {
                "options": [
                    {"name": "Synced", "color": "green"},
                    {"name": "Needs Review", "color": "yellow"},
                ]
            }
        },
    }

    for name, spec in wanted.items():
        if name not in existing:
            missing[name] = spec

    if "Accounts" not in existing:
        missing["Accounts"] = {"relation": {"database_id": accounts_db, "single_property": {}}}

    if missing and dry_run:
        console.print(f"[yellow]Dry run - would add {len(missing)} Campaign Tracker properties[/yellow]")
    elif missing:
        _notion_request("PATCH", f"/v1/databases/{campaigns_db}", {"properties": missing})
        console.print(f"[green]Campaign Tracker schema ensured ({len(missing)} property/properties added)[/green]")
    else:
        console.print("[green]Campaign Tracker schema already ready[/green]")

    return _title_property(database)


def fetch_accounts() -> list[dict]:
    accounts_db = _normalize_notion_id(NOTION_DB_ACCOUNTS_ID)
    if not accounts_db:
        return []
    pages = _query_database(accounts_db)
    accounts = []
    for page in pages:
        props = page.get("properties", {})
        campaigns = _names(_find_prop(props, ["Campaign ID"]))
        if not campaigns:
            continue
        accounts.append({
            "id": page.get("id", ""),
            "created_time": page.get("created_time", ""),
            "last_edited_time": page.get("last_edited_time", ""),
            "name": _title_from_props(props),
            "campaign_ids": campaigns,
            "trigger": _plain_text(_find_prop(props, ["Trigger Event", "Trigger Event (Corporates)"])),
            "mission": _plain_text(_find_prop(props, ["Mission*", "Mission"])),
            "status": _plain_text(_find_prop(props, ["Status"])),
            "account_type": _plain_text(_find_prop(props, ["Account Type*"])),
            "industry": _plain_text(_find_prop(props, ["Industry (Corporates)", "Industry"])),
            "work_area": _plain_text(_find_prop(props, ["Work Area NGO"])),
            "company_description": _plain_text(_find_prop(props, ["Company Description"])),
            "latest_funding": _plain_text(_find_prop(props, ["Latest Funding"])),
            "employees": _number(_find_prop(props, ["# Employees"])),
            "suspect_name": _plain_text(_find_prop(props, ["[Suspect] Contact Name"])),
            "suspect_email": _plain_text(_find_prop(props, ["[Suspect] Contact Email"])),
            "suspect_title": _plain_text(_find_prop(props, ["[Suspect] Job Title"])),
            "linkedin_first": _plain_text(_find_prop(props, ["LinkedIn 1st Cold"])),
            "email_body": _plain_text(_find_prop(props, ["Cold Email Body"])),
            "email_subject": _plain_text(_find_prop(props, ["Cold Email Subject Text", "Cold Email Subject"])),
            "ab_variant": _plain_text(_find_prop(props, ["AB Variant"])),
        })
    return accounts


def fetch_contacts() -> list[dict]:
    contacts_db = _normalize_notion_id(NOTION_DB_CONTACTS_ID)
    if not contacts_db:
        return []
    try:
        pages = _query_database(contacts_db)
    except Exception as e:
        console.print(f"[yellow]Contacts DB unavailable for campaign tracker sync: {e}[/yellow]")
        return []

    contacts = []
    for page in pages:
        props = page.get("properties", {})
        account_ids = _relation_ids(_find_prop(props, ["Accounts", "Account"]))
        if not account_ids:
            continue
        contacts.append({
            "id": page.get("id", ""),
            "created_time": page.get("created_time", ""),
            "last_edited_time": page.get("last_edited_time", ""),
            "name": _title_from_props(props),
            "account_ids": account_ids,
            "campaign_ids": _names(_find_prop(props, ["Campaign ID"])),
            "job_title": _plain_text(_find_prop(props, ["Job Title", "Title"])),
            "account_status": _plain_text(_find_prop(props, ["Account Status"])),
            "ab_variant": _plain_text(_find_prop(props, ["AB Variant"])),
            "linkedin_first": _plain_text(_find_prop(props, ["LinkedIn 1st Cold"])),
            "email_body": _plain_text(_find_prop(props, ["Cold Email Body"])),
            "email_subject": _plain_text(_find_prop(props, ["Cold Email Subject", "Cold Email Subject Text"])),
        })
    return contacts


def _account_to_contact(account: dict) -> dict:
    return {
        "id": f"account:{account['id']}",
        "storage": "account",
        "created_time": account.get("created_time", ""),
        "last_edited_time": account.get("last_edited_time", ""),
        "name": account.get("suspect_name") or account.get("name", ""),
        "contact_name": account.get("suspect_name") or account.get("name", ""),
        "company_name": account.get("name", ""),
        "account_ids": [account.get("id", "")],
        "campaign_ids": account.get("campaign_ids", []),
        "job_title": account.get("suspect_title", ""),
        "account_status": account.get("status", ""),
        "ab_variant": account.get("ab_variant", ""),
        "linkedin_first": account.get("linkedin_first", ""),
        "email_body": account.get("email_body", ""),
        "email_subject": account.get("email_subject", ""),
    }


def _has_account_level_outreach(account: dict) -> bool:
    return bool(
        account.get("suspect_name")
        or account.get("suspect_email")
        or account.get("email_body")
        or account.get("linkedin_first")
    )


def _status_index(status: str) -> int:
    try:
        return STATUS_HIERARCHY.index(status)
    except ValueError:
        return -1


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_outcome(contact: dict) -> str:
    status = contact.get("account_status", "")
    idx = _status_index(status)
    if idx >= ENGAGED_INDEX and idx != UNQUALIFIED_INDEX:
        return "success"
    if idx == UNQUALIFIED_INDEX:
        return "failure"
    if status.startswith("Contacted"):
        edited = _parse_dt(contact.get("account_last_edited") or contact.get("last_edited_time", ""))
        if edited and (datetime.now(timezone.utc) - edited).days > STALE_DAYS:
            return "failure"
    return "skip"


def _persona_label(title: str) -> str:
    title = (title or "").lower()
    if any(term in title for term in ("talent", "recruit", "hiring", "people", "human resource", "hr")):
        return "Recruiting/People"
    if any(term in title for term in ("marketing", "brand", "communications", "growth", "pr ")):
        return "Marketing/Comms"
    if any(term in title for term in ("partnership", "business development", "bd", "alliances", "campus", "university", "community", "ecosystem")):
        return "Partnerships/BD/Campus"
    if any(term in title for term in ("developer relations", "devrel", "cto", "engineering", "product", "technology")):
        return "Technical/Product"
    if any(term in title for term in ("founder", "ceo", "chief", "c-suite", "managing director")):
        return "Founders/C-suite"
    if title:
        return "Other named contacts"
    return "Account-level outreach"


def _campaign_type(campaign_id: str, accounts: list[dict]) -> str:
    if campaign_id.lower().startswith("ngo"):
        return "Social Partnerships"
    account_types = {a.get("account_type", "") for a in accounts if a.get("account_type")}
    if account_types and account_types <= {"NGO"}:
        return "Social Partnerships"
    if any(a.get("account_type") == "NGO" for a in accounts) and any(a.get("account_type") != "NGO" for a in accounts):
        return "Mixed/Other"
    return "Strategic Partnerships"


def _summarize_trigger(triggers: list[str], outreach_texts: list[str]) -> str:
    cleaned = [_truncate(t, 700) for t in triggers if t.strip()]
    if cleaned:
        counts = Counter(cleaned)
        top, top_count = counts.most_common(1)[0]
        if len(counts) == 1 or top_count >= max(2, len(cleaned) // 2):
            return top
        examples = [trigger for trigger, _count in counts.most_common(3)]
        return "Mixed triggers: " + " | ".join(examples)
    text = " ".join(outreach_texts).lower()
    if "funding" in text:
        return "Recent funding round and expected company growth."
    if "invoice" in text or "rechnung" in text:
        return "NGO invoice inspection and management automation fit."
    if "hackathon" in text:
        return "Upcoming AI-for-Good Hackathon partnership and visibility opportunity."
    return "No explicit trigger found in Account fields; infer from outreach copy and account context."


def _target_audience(contacts: list[dict], accounts: list[dict]) -> str:
    personas = Counter(_persona_label(c.get("job_title", "")) for c in contacts)
    if personas:
        ordered = [f"{label} ({count})" for label, count in personas.most_common()]
        return "; ".join(ordered)
    types = Counter(a.get("account_type", "Unknown") or "Unknown" for a in accounts)
    return "; ".join(f"{label} accounts ({count})" for label, count in types.most_common())


def _target_reasoning(campaign_id: str, trigger: str, audience: str, accounts_count: int) -> str:
    lower = f"{campaign_id} {trigger}".lower()
    base = f"Targets {accounts_count} account(s), focused on {audience}."
    if "funding" in lower and "hackathon" in lower:
        return (
            f"{base} Recent funding suggests growth, hiring, and brand-building pressure. "
            "The outreach proposes talent access and Munich university ecosystem visibility through the AI-for-Good Hackathon."
        )
    if "funding" in lower:
        return f"{base} Recent funding is a timely growth signal, so recruiting, marketing, and partnership teams are likely to care about talent access and ecosystem visibility."
    if "invoice" in lower or "rechnung" in lower or campaign_id.lower().startswith("ngo"):
        return f"{base} These NGOs are relevant because their operating model likely involves local partners, grants, invoices, and back-office work that a pro-bono AI invoice tool can reduce."
    if "hackathon" in lower:
        return f"{base} The campaign uses the upcoming AI-for-Good Hackathon as a concrete reason to discuss sponsorship, visibility, and talent access."
    if "aijobs" in lower or "hiring" in lower or "jobs" in lower:
        return f"{base} The campaign is built around active hiring and gives companies access to AI/software talent from TUM."
    return f"{base} The campaign groups accounts with a shared outreach reason from Account triggers and message copy, then tracks response performance for future copywriter decisions."


def _outreach_summary(contacts: list[dict], winner: str) -> str:
    subjects = [c.get("email_subject", "") for c in contacts if c.get("email_subject")]
    bodies = [c.get("email_body", "") or c.get("linkedin_first", "") for c in contacts if c.get("email_body") or c.get("linkedin_first")]
    subject_summary = ", ".join(item for item, _count in Counter(subjects).most_common(3))
    first_hook = ""
    if bodies:
        first_hook = re.split(r"(?<=[.!?])\s+", bodies[0].strip())[0]
    parts = []
    if subject_summary:
        parts.append(f"Common subject(s): {subject_summary}.")
    if first_hook:
        parts.append(f"Example opening: {first_hook}")
    parts.append(f"A/B winner tracked as: {winner}. Copywriters should keep testing A/B variants and review this campaign before drafting similar outreach.")
    return _truncate(" ".join(parts))


def _ab_stats(contacts: list[dict]) -> tuple[dict, str]:
    stats = {
        "A": {"total": 0, "success": 0, "failure": 0, "skip": 0},
        "B": {"total": 0, "success": 0, "failure": 0, "skip": 0},
        "none": {"total": 0, "success": 0, "failure": 0, "skip": 0},
    }
    for contact in contacts:
        variant = (contact.get("ab_variant") or "").upper()
        bucket = variant if variant in ("A", "B") else "none"
        outcome = contact.get("outcome", "skip")
        stats[bucket]["total"] += 1
        stats[bucket][outcome] += 1

    def resolved_rate(bucket: str) -> float | None:
        resolved = stats[bucket]["success"] + stats[bucket]["failure"]
        if resolved == 0:
            return None
        return stats[bucket]["success"] / resolved

    rate_a = resolved_rate("A")
    rate_b = resolved_rate("B")
    resolved_a = stats["A"]["success"] + stats["A"]["failure"]
    resolved_b = stats["B"]["success"] + stats["B"]["failure"]
    if resolved_a == 0 and resolved_b == 0:
        winner = "No Data"
    elif min(resolved_a, resolved_b) < 3 or rate_a is None or rate_b is None:
        winner = "Inconclusive"
    elif abs(rate_a - rate_b) < 0.05:
        winner = "Inconclusive"
    else:
        winner = "A" if rate_a > rate_b else "B"
    return stats, winner


def build_campaign_records(
    accounts: list[dict],
    contacts: list[dict],
    campaign_filter: str = "",
) -> list[dict]:
    account_by_id = {a["id"]: a for a in accounts}
    campaigns: dict[str, dict] = defaultdict(lambda: {"accounts": [], "contacts": []})
    for account in accounts:
        for campaign in account.get("campaign_ids", []):
            if campaign_filter and campaign != campaign_filter:
                continue
            campaigns[campaign]["accounts"].append(account)

    contact_ids_by_campaign: dict[str, set[str]] = defaultdict(set)
    real_contact_account_ids = {aid for contact in contacts for aid in contact.get("account_ids", [])}
    for contact in contacts:
        account_ids = [aid for aid in contact.get("account_ids", []) if aid in account_by_id]
        campaign_names = set(contact.get("campaign_ids", []))
        for account_id in account_ids:
            campaign_names.update(account_by_id[account_id].get("campaign_ids", []))
        for campaign in campaign_names:
            if campaign_filter and campaign != campaign_filter:
                continue
            if contact["id"] in contact_ids_by_campaign[campaign]:
                continue
            account = account_by_id.get(account_ids[0]) if account_ids else {}
            enriched = {
                **contact,
                "storage": "contact",
                "campaign_id": campaign,
                "campaign_ids": sorted(campaign_names),
                "contact_name": contact.get("name", ""),
                "company_name": account.get("name", ""),
                "account_status": contact.get("account_status") or account.get("status", ""),
                "account_last_edited": account.get("last_edited_time", ""),
            }
            campaigns[campaign]["contacts"].append(enriched)
            contact_ids_by_campaign[campaign].add(contact["id"])

    for campaign, bucket in campaigns.items():
        for account in bucket["accounts"]:
            if account["id"] in real_contact_account_ids:
                continue
            if not _has_account_level_outreach(account):
                continue
            pseudo = _account_to_contact(account)
            pseudo["campaign_id"] = campaign
            pseudo["campaign_ids"] = [campaign]
            pseudo["account_last_edited"] = account.get("last_edited_time", "")
            bucket["contacts"].append(pseudo)

    records = []
    for campaign, bucket in sorted(campaigns.items()):
        campaign_accounts = bucket["accounts"]
        if not campaign_accounts:
            continue
        campaign_contacts = bucket["contacts"]
        for contact in campaign_contacts:
            contact["outcome"] = classify_outcome(contact)
        successes = sum(1 for c in campaign_contacts if c["outcome"] == "success")
        failures = sum(1 for c in campaign_contacts if c["outcome"] == "failure")
        pending = sum(1 for c in campaign_contacts if c["outcome"] == "skip")
        not_engaged = failures + pending
        stats, winner = _ab_stats(campaign_contacts)
        triggers = [a.get("trigger", "") for a in campaign_accounts]
        outreach_texts = [
            " ".join(filter(None, [c.get("email_subject", ""), c.get("email_body", ""), c.get("linkedin_first", "")]))
            for c in campaign_contacts
        ]
        trigger = _summarize_trigger(triggers, outreach_texts)
        audience = _target_audience(campaign_contacts, campaign_accounts)
        first_seen_values = sorted(a.get("created_time", "") for a in campaign_accounts if a.get("created_time"))
        relation_note = ""
        if len(campaign_accounts) > MAX_RELATION_ACCOUNTS:
            relation_note = f" Only the first {MAX_RELATION_ACCOUNTS} accounts are linked because Notion relation updates are capped in this sync."
        records.append({
            "campaign_id": campaign,
            "accounts": campaign_accounts,
            "contacts": campaign_contacts,
            "accounts_count": len(campaign_accounts),
            "contacts_count": len(campaign_contacts),
            "engaged_contacts": successes,
            "failed_contacts": failures,
            "pending_contacts": pending,
            "not_engaged_contacts": not_engaged,
            "success_rate": _percent(successes, len(campaign_contacts)),
            "ab_stats": stats,
            "ab_winner": winner,
            "campaign_type": _campaign_type(campaign, campaign_accounts),
            "trigger": trigger,
            "target_audience": audience,
            "targeting_reasoning": _target_reasoning(campaign, trigger, audience, len(campaign_accounts)),
            "outreach_summary": _outreach_summary(campaign_contacts, winner),
            "crm_notes": _truncate(
                "Connected only to Accounts. Contacts and meeting notes should be read through the related Accounts CRM graph."
                + relation_note
            ),
            "first_seen": first_seen_values[0] if first_seen_values else None,
            "last_synced": datetime.now(timezone.utc).isoformat(),
            "sync_status": "Synced" if trigger and campaign_contacts else "Needs Review",
        })
    return records


def _existing_campaign_pages(title_prop: str) -> dict[str, str]:
    campaigns_db = _normalize_notion_id(NOTION_DB_CAMPAIGNS_ID)
    pages = _query_database(campaigns_db)
    existing = {}
    for page in pages:
        name = _plain_text(page.get("properties", {}).get(title_prop, {})).strip()
        if name:
            existing[name] = page.get("id", "")
    return existing


def _record_properties(record: dict, title_prop: str) -> dict:
    stats = record["ab_stats"]
    a_total = stats["A"]["total"]
    b_total = stats["B"]["total"]
    a_not = stats["A"]["failure"] + stats["A"]["skip"]
    b_not = stats["B"]["failure"] + stats["B"]["skip"]
    relation_accounts = [{"id": account["id"]} for account in record["accounts"][:MAX_RELATION_ACCOUNTS]]
    return {
        title_prop: _title(record["campaign_id"]),
        "Campaign ID": _rich_text(record["campaign_id"]),
        "Campaign Type": {"select": {"name": record["campaign_type"]}},
        "Accounts": {"relation": relation_accounts},
        "Campaign Trigger": _rich_text(record["trigger"]),
        "Target Audience": _rich_text(record["target_audience"]),
        "Targeting Reasoning": _rich_text(record["targeting_reasoning"]),
        "Outreach Summary": _rich_text(record["outreach_summary"]),
        "CRM Notes": _rich_text(record["crm_notes"]),
        "Accounts Count": {"number": record["accounts_count"]},
        "Contacts Count": {"number": record["contacts_count"]},
        "Engaged Contacts": {"number": record["engaged_contacts"]},
        "Not Engaged Contacts": {"number": record["not_engaged_contacts"]},
        "Pending Contacts": {"number": record["pending_contacts"]},
        "Failed Contacts": {"number": record["failed_contacts"]},
        "Success Rate": {"number": record["success_rate"]},
        "A Total": {"number": a_total},
        "A Engaged": {"number": stats["A"]["success"]},
        "A Not Engaged": {"number": a_not},
        "A Success Rate": {"number": _percent(stats["A"]["success"], a_total)},
        "B Total": {"number": b_total},
        "B Engaged": {"number": stats["B"]["success"]},
        "B Not Engaged": {"number": b_not},
        "B Success Rate": {"number": _percent(stats["B"]["success"], b_total)},
        "A/B Winner": {"select": {"name": record["ab_winner"]}},
        "First Seen": _date(record["first_seen"]),
        "Last Synced": _date(record["last_synced"]),
        "Sync Status": {"select": {"name": record["sync_status"]}},
    }


def upsert_campaign_records(records: list[dict], title_prop: str) -> tuple[int, int]:
    campaigns_db = _normalize_notion_id(NOTION_DB_CAMPAIGNS_ID)
    existing = _existing_campaign_pages(title_prop)
    created = 0
    updated = 0
    for record in records:
        props = _record_properties(record, title_prop)
        page_id = existing.get(record["campaign_id"])
        if page_id:
            _notion_request("PATCH", f"/v1/pages/{page_id}", {"properties": props})
            updated += 1
        else:
            _notion_request(
                "POST",
                "/v1/pages",
                {"parent": {"database_id": campaigns_db}, "properties": props},
            )
            created += 1
        time.sleep(0.12)
    return created, updated


def _print_records(records: list[dict], title: str = "Campaign Tracker Sync") -> None:
    table = Table(title=title)
    table.add_column("Campaign", style="cyan")
    table.add_column("Type")
    table.add_column("Accounts", justify="right")
    table.add_column("Contacts", justify="right")
    table.add_column("Engaged", justify="right")
    table.add_column("A/B")
    table.add_column("Trigger")
    for record in records:
        table.add_row(
            record["campaign_id"],
            record["campaign_type"],
            str(record["accounts_count"]),
            str(record["contacts_count"]),
            str(record["engaged_contacts"]),
            record["ab_winner"],
            _truncate(record["trigger"], 80),
        )
    console.print(table)


def sync_campaign_tracker(campaign_id: str = "", dry_run: bool = False) -> list[dict]:
    """Backfill or update Campaign Tracker entries from Accounts and Contacts."""
    if not NOTION_DB_CAMPAIGNS_ID:
        console.print("[yellow]NOTION_DB_CAMPAIGNS_ID not configured; skipping campaign tracker sync.[/yellow]")
        return []
    title_prop = ensure_campaign_tracker_schema(dry_run=dry_run)
    console.print("[cyan]Fetching Accounts and Contacts for Campaign Tracker...[/cyan]")
    accounts = fetch_accounts()
    contacts = fetch_contacts()
    records = build_campaign_records(accounts, contacts, campaign_filter=campaign_id)
    if not records:
        console.print("[yellow]No campaign records found to sync.[/yellow]")
        return []
    _print_records(records, title="Campaign Tracker Preview" if dry_run else "Campaign Tracker Sync")
    if dry_run:
        return records
    created, updated = upsert_campaign_records(records, title_prop)
    console.print(f"[green]Campaign Tracker synced: {created} created, {updated} updated[/green]")
    return records


def load_campaign_guidance(campaign_id: str = "", limit: int = 5) -> str:
    """Return campaign tracker context for copywriters before drafting."""
    if not NOTION_DB_CAMPAIGNS_ID or not NOTION_TOKEN:
        return ""
    try:
        campaigns_db = _normalize_notion_id(NOTION_DB_CAMPAIGNS_ID)
        database = _notion_request("GET", f"/v1/databases/{campaigns_db}")
        title_prop = _title_property(database)
        pages = _query_database(campaigns_db)
    except Exception as e:
        console.print(f"[yellow]Could not load Campaign Tracker guidance: {e}[/yellow]")
        return ""

    def page_summary(page: dict) -> dict:
        props = page.get("properties", {})
        return {
            "name": _plain_text(props.get(title_prop, {})),
            "trigger": _plain_text(props.get("Campaign Trigger", {})),
            "audience": _plain_text(props.get("Target Audience", {})),
            "reasoning": _plain_text(props.get("Targeting Reasoning", {})),
            "summary": _plain_text(props.get("Outreach Summary", {})),
            "contacts": _plain_text(props.get("Contacts Count", {})),
            "engaged": _plain_text(props.get("Engaged Contacts", {})),
            "not_engaged": _plain_text(props.get("Not Engaged Contacts", {})),
            "winner": _plain_text(props.get("A/B Winner", {})),
            "last_edited": page.get("last_edited_time", ""),
        }

    summaries = [page_summary(page) for page in pages]
    exact = next((item for item in summaries if campaign_id and item["name"] == campaign_id), None)
    selected = [exact] if exact else []
    if not selected:
        selected = sorted(summaries, key=lambda item: item.get("last_edited", ""), reverse=True)[:limit]
    if not selected:
        return ""

    lines = [
        "\n\n## CAMPAIGN TRACKER CONTEXT (consult before drafting)",
        "Always run A/B testing: assign and write AB Variant A or B, then keep the Campaign Tracker synced so feedback can compare versions.",
    ]
    if campaign_id and not exact:
        lines.append(f"No existing Campaign Tracker entry found for {campaign_id}; compare against recent similar campaigns below.")
    for item in selected:
        lines.extend([
            f"\nCampaign: {item['name']}",
            f"- Trigger: {item['trigger'] or 'not captured yet'}",
            f"- Target audience: {item['audience'] or 'not captured yet'}",
            f"- Reasoning: {item['reasoning'] or 'not captured yet'}",
            f"- Performance: {item['engaged'] or 0}/{item['contacts'] or 0} engaged, {item['not_engaged'] or 0} not engaged",
            f"- A/B winner: {item['winner'] or 'No Data'}",
            f"- Outreach notes: {item['summary'] or 'none yet'}",
        ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Notion Campaign Tracker from Accounts and Contacts")
    parser.add_argument("--campaign", default="", help="Only sync one Campaign ID")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Notion")
    args = parser.parse_args()
    sync_campaign_tracker(campaign_id=args.campaign, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
