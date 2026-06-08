"""
Apollo Enrichment Agent - repeatable review flow around Apollo people enrichment.

This agent does not call Apollo directly. Connector/API calls are made by Codex
or the Apollo UI, then this script turns search reviews and enrichment payloads
into deterministic CSV artifacts:

1. Batch manifest for approved Apollo people enrichment.
2. Human review CSV with email/mobile readiness.
3. Strict upload-ready CSV for the Notion upload agent.

Usage:
    python -m agents.apollo_enrichment_agent
    python -m agents.apollo_enrichment_agent --mcp-json apollo_bulk_match.jsonl
    python -m agents.apollo_enrichment_agent --apollo-csv apollo_export.csv
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import (
    APOLLO_CONTACT_REVIEW_CSV,
    APOLLO_ENRICHED_REVIEW_CSV,
    APOLLO_ENRICHMENT_BATCHES_JSON,
    APOLLO_READY_CSV,
    APOLLO_UPLOAD_READY_CSV,
    COMPANY_BLOCKLIST,
    KNOWN_DOMAIN_CORRECTIONS,
)
from utils.notion_client import clean_value, contact_phone_safe

console = Console()

APOLLO_EXPORT_COLUMNS = [
    "First Name",
    "Last Name",
    "Email",
    "Email Status",
    "Title",
    "Person Linkedin Url",
    "Work Direct Phone",
    "Mobile Phone",
    "Home Phone",
    "Company Name",
    "Company Name for Emails",
    "Website",
    "Company Linkedin Url",
    "City",
    "Company Country",
    "Company Phone",
    "Industry",
    "# Employees",
    "Latest Funding",
    "Latest Funding Amount",
    "Trigger",
    "Mission (reasoning)",
    "Lead Score",
    "Apollo Account Id",
    "Apollo Contact Id",
]

REVIEW_COLUMNS = [
    "export_rank",
    "candidate_priority",
    "company_name",
    "company_domain",
    "person_name",
    "title",
    "email",
    "email_status",
    "linkedin_url",
    "apollo_person_id",
    "contact_source",
    "lead_contact_status",
    "mobile_phone_required",
    "mobile_phone_status",
    "safe_contact_phone",
    "ready_for_notion_upload",
    "upload_blocker",
    "trigger",
    "score",
    "reasoning",
    "notes",
]

SENIOR_TITLE_RE = re.compile(
    r"\b("
    r"chief|cmo|chro|vp|svp|evp|vice president|head|global head|director|"
    r"senior director|sr\.?\s+director|executive director|lead|principal"
    r")\b",
    re.IGNORECASE,
)
SENIOR_MANAGER_RE = re.compile(r"\b(senior|sr\.?|global|regional|emea)\s+manager\b", re.IGNORECASE)
MARKETING_OR_RECRUITING_RE = re.compile(
    r"\b("
    r"marketing|growth|brand|communications?|demand generation|demand gen|"
    r"recruit(?:ing|ment)?|talent acquisition|talent|people|people\s*&\s*culture|"
    r"human resources|hr|employer brand(?:ing)?|staffing|"
    r"partnerships?|business development|bizdev|bd|alliances?|"
    r"campus relations?|university relations?|ecosystem|community"
    r")\b",
    re.IGNORECASE,
)


def normalize_domain(value: str) -> str:
    """Normalize website/domain strings for joining ranking, review, and Apollo data."""
    value = clean_value(value).lower().strip()
    if not value or value == "nan":
        return ""
    match = re.search(r"(?:https?://)?(?:www\.)?([^/\s]+)", value)
    domain = match.group(1).rstrip("/") if match else value.rstrip("/")
    return KNOWN_DOMAIN_CORRECTIONS.get(domain, domain)


def is_excluded_company(company_name: str = "", company_domain: str = "") -> bool:
    """Return True for companies that should not enter Apollo campaign artifacts."""
    name = clean_value(company_name).lower().strip()
    domain = normalize_domain(company_domain)
    for blocked in COMPANY_BLOCKLIST:
        blocked = clean_value(blocked).lower().strip()
        if not blocked:
            continue
        if blocked in name or blocked in domain:
            return True
    return False


def filter_excluded_companies(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    if df.empty:
        return df, 0
    keep = []
    removed = 0
    for _, row in df.iterrows():
        name = first_value(row, ["company_name", "Company Name", "organization_name", "Organization Name"])
        domain = first_value(row, ["company_domain", "Website", "website", "website_url", "Company Website"])
        if is_excluded_company(name, domain):
            removed += 1
            continue
        keep.append(row)
    return (pd.DataFrame(keep) if keep else pd.DataFrame(columns=df.columns), removed)


def truthy(value: Any) -> bool:
    value = clean_value(value).strip().lower()
    return value in {"1", "true", "yes", "y", "email_present"}


def first_value(row: pd.Series, candidates: Iterable[str]) -> str:
    for col in candidates:
        if col in row:
            value = clean_value(row.get(col)).strip()
            if value and value.lower() != "nan":
                return value
    return ""


def split_name(name: str) -> Tuple[str, str]:
    parts = clean_value(name).strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def senior_marketing_or_recruiting_title(title: str) -> bool:
    """True when a title is senior and sits in a strong outreach persona area."""
    title = clean_value(title)
    if not title:
        return False
    is_senior = bool(SENIOR_TITLE_RE.search(title) or SENIOR_MANAGER_RE.search(title))
    is_target_area = bool(MARKETING_OR_RECRUITING_RE.search(title))
    return is_senior and is_target_area


def website_from_domain(domain: str) -> str:
    domain = normalize_domain(domain)
    return f"https://{domain}" if domain else ""


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def selected_review_rows(review_df: pd.DataFrame) -> pd.DataFrame:
    if review_df.empty or "apollo_person_id" not in review_df.columns:
        return pd.DataFrame()
    rows = review_df[review_df["apollo_person_id"].notna()].copy()
    rows["apollo_person_id"] = rows["apollo_person_id"].astype(str).str.strip()
    rows = rows[rows["apollo_person_id"] != ""]
    if "review_status" in rows.columns:
        rows = rows[rows["review_status"].astype(str).str.strip().isin(["selected_for_review", "known_contact"]) | (rows["review_status"].astype(str).str.strip() == "selected_for_review")]
    return rows


def build_enrichment_batches(review_df: pd.DataFrame, batch_size: int) -> Dict[str, Any]:
    rows = selected_review_rows(review_df)
    records: List[Dict[str, Any]] = []
    for _, row in rows.iterrows():
        apollo_id = clean_value(row.get("apollo_person_id")).strip()
        if not apollo_id:
            continue
        records.append(
            {
                "id": apollo_id,
                "company_name": clean_value(row.get("company_name")),
                "company_domain": normalize_domain(row.get("company_domain")),
                "person_name_preview": clean_value(row.get("person_name_preview")),
                "title": clean_value(row.get("title")),
                "mobile_phone_required": senior_marketing_or_recruiting_title(row.get("title")),
            }
        )

    batches = []
    for idx in range(0, len(records), batch_size):
        chunk = records[idx : idx + batch_size]
        batches.append(
            {
                "batch_index": len(batches) + 1,
                "count": len(chunk),
                "details": [{"id": item["id"]} for item in chunk],
                "audit": chunk,
            }
        )

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(APOLLO_CONTACT_REVIEW_CSV),
        "batch_size": batch_size,
        "total_people": len(records),
        "estimated_max_credits": len(records),
        "batches": batches,
    }


def extract_text_payload(obj: Any) -> Any:
    """Unwrap MCP content objects where the useful JSON is inside text."""
    if isinstance(obj, dict) and obj.get("type") == "text" and isinstance(obj.get("text"), str):
        try:
            return json.loads(obj["text"])
        except json.JSONDecodeError:
            return obj
    return obj


def collect_mcp_responses(obj: Any) -> List[Dict[str, Any]]:
    """Recursively collect Apollo bulk_match response dictionaries from JSON."""
    obj = extract_text_payload(obj)
    responses: List[Dict[str, Any]] = []

    if isinstance(obj, dict):
        if "matches" in obj and isinstance(obj.get("matches"), list):
            responses.append(obj)
        result = obj.get("result")
        if isinstance(result, dict):
            responses.extend(collect_mcp_responses(result))
        payload = obj.get("payload")
        if isinstance(payload, dict):
            output = payload.get("output")
            if isinstance(output, str):
                # function_call_output stores a small preamble before the JSON list.
                marker = "Output:\n"
                if marker in output:
                    output = output.split(marker, 1)[1]
                try:
                    responses.extend(collect_mcp_responses(json.loads(output)))
                except json.JSONDecodeError:
                    pass
            responses.extend(collect_mcp_responses(payload))
        content = obj.get("content")
        if isinstance(content, list):
            responses.extend(collect_mcp_responses(content))
        ok = obj.get("Ok")
        if isinstance(ok, dict):
            responses.extend(collect_mcp_responses(ok))
    elif isinstance(obj, list):
        for item in obj:
            responses.extend(collect_mcp_responses(item))

    return responses


def load_mcp_responses(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    responses: List[Dict[str, Any]] = []

    try:
        responses.extend(collect_mcp_responses(json.loads(text)))
    except json.JSONDecodeError:
        pass

    if not responses:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                responses.extend(collect_mcp_responses(json.loads(line)))
            except json.JSONDecodeError:
                continue

    # Deduplicate by request_id when the session log includes both event and output items.
    seen = set()
    unique = []
    for response in responses:
        key = response.get("request_id") or json.dumps(response.get("matches", []), sort_keys=True)[:200]
        if key in seen:
            continue
        seen.add(key)
        unique.append(response)
    return unique


def pick_match_phone(match: Dict[str, Any], company_phone: str) -> Tuple[str, str, str]:
    """Return work direct, mobile, home phones, with mobile kept only when safe."""
    work_direct = clean_value(match.get("direct_phone") or match.get("work_direct_phone"))
    mobile = clean_value(match.get("mobile_phone") or match.get("personal_phone"))
    home = clean_value(match.get("home_phone"))

    for item in match.get("phone_numbers") or []:
        if not isinstance(item, dict):
            continue
        number = clean_value(item.get("raw_number") or item.get("number") or item.get("sanitized_number"))
        kind = clean_value(item.get("type") or item.get("label")).lower()
        if not number:
            continue
        if "mobile" in kind and not mobile:
            mobile = number
        elif not work_direct:
            work_direct = number

    safe_mobile = contact_phone_safe(mobile, company_phone)
    safe_work = contact_phone_safe(work_direct, company_phone)
    safe_home = contact_phone_safe(home, company_phone)
    return safe_work, safe_mobile, safe_home


def match_to_apollo_row(match: Dict[str, Any]) -> Dict[str, Any]:
    org = match.get("organization") or match.get("account") or {}
    primary_phone = org.get("primary_phone") if isinstance(org.get("primary_phone"), dict) else {}
    company_phone = clean_value(org.get("phone") or primary_phone.get("number"))
    work_direct, mobile, home = pick_match_phone(match, company_phone)

    first_name = clean_value(match.get("first_name"))
    last_name = clean_value(match.get("last_name"))
    if not first_name and not last_name:
        first_name, last_name = split_name(match.get("name", ""))

    website = clean_value(org.get("website_url") or org.get("domain") or org.get("primary_domain"))
    domain = normalize_domain(org.get("primary_domain") or org.get("domain") or website)
    if not website and domain:
        website = website_from_domain(domain)

    return {
        "First Name": first_name,
        "Last Name": last_name,
        "Email": clean_value(match.get("email")),
        "Email Status": clean_value(match.get("email_status")),
        "Title": clean_value(match.get("title")),
        "Person Linkedin Url": clean_value(match.get("linkedin_url")),
        "Work Direct Phone": work_direct,
        "Mobile Phone": mobile,
        "Home Phone": home,
        "Company Name": clean_value(org.get("name") or match.get("organization_name")),
        "Company Name for Emails": clean_value(org.get("name") or match.get("organization_name")),
        "Website": website,
        "Company Linkedin Url": clean_value(org.get("linkedin_url")),
        "City": clean_value(org.get("city") or match.get("city")),
        "Company Country": clean_value(org.get("country") or match.get("country")),
        "Company Phone": company_phone,
        "Industry": clean_value(org.get("industry")),
        "# Employees": org.get("estimated_num_employees", ""),
        "Latest Funding": "",
        "Latest Funding Amount": "",
        "Trigger": "",
        "Mission (reasoning)": "",
        "Lead Score": "",
        "Apollo Account Id": clean_value(match.get("account_id") or org.get("id")),
        "Apollo Contact Id": clean_value(match.get("id")),
    }


def mcp_responses_to_apollo_df(responses: List[Dict[str, Any]]) -> Tuple[pd.DataFrame, Dict[str, int]]:
    rows = []
    stats = {
        "requested": 0,
        "matched": 0,
        "missing": 0,
        "credits_consumed": 0,
    }
    for response in responses:
        matches = response.get("matches") or []
        valid_matches = [match for match in matches if isinstance(match, dict)]
        stats["requested"] += int(response.get("total_requested_enrichments") or 0)
        stats["matched"] += len(valid_matches)
        stats["missing"] += int(response.get("missing_records") or 0)
        stats["credits_consumed"] += int(response.get("credits_consumed") or 0)
        rows.extend(match_to_apollo_row(match) for match in valid_matches)

    df = pd.DataFrame(rows)
    for col in APOLLO_EXPORT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[APOLLO_EXPORT_COLUMNS], stats


def ensure_apollo_export_shape(df: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "First Name": ["first_name", "FirstName"],
        "Last Name": ["last_name", "LastName"],
        "Email": ["email", "Email Address"],
        "Email Status": ["email_status", "Email Confidence"],
        "Title": ["title", "Job Title"],
        "Person Linkedin Url": ["linkedin_url", "LinkedIn URL", "Person Linkedin URL"],
        "Company Name": ["company_name", "Company", "Organization Name"],
        "Company Name for Emails": ["company_name_for_emails", "Company"],
        "Website": ["website", "website_url", "Company Website", "company_domain"],
        "Company Linkedin Url": ["company_linkedin", "Company LinkedIn"],
        "City": ["city"],
        "Company Country": ["country", "Company Country"],
        "Company Phone": ["company_phone", "Company Phone Number", "Organization Phone"],
        "Industry": ["industry"],
        "# Employees": ["employees", "Estimated Employees"],
        "Latest Funding": ["latest_funding"],
        "Latest Funding Amount": ["latest_funding_amount", "funding_amount"],
        "Trigger": ["trigger"],
        "Mission (reasoning)": ["reasoning", "mission"],
        "Lead Score": ["score", "lead_score"],
        "Apollo Account Id": ["apollo_account_id", "account_id"],
        "Apollo Contact Id": ["apollo_contact_id", "apollo_person_id", "person_id", "id"],
        "Work Direct Phone": ["work_direct_phone", "direct_phone", "Direct Phone"],
        "Mobile Phone": ["mobile_phone"],
        "Home Phone": ["home_phone"],
    }

    shaped = df.copy()
    for canonical, candidates in aliases.items():
        if canonical in shaped.columns:
            continue
        for candidate in candidates:
            if candidate in shaped.columns:
                shaped[canonical] = shaped[candidate]
                break
        if canonical not in shaped.columns:
            shaped[canonical] = ""

    shaped["Website"] = shaped["Website"].apply(lambda v: website_from_domain(v) if normalize_domain(v) and not clean_value(v).startswith("http") else clean_value(v))

    for _, row in shaped.iterrows():
        company_phone = clean_value(row.get("Company Phone"))
        for col in ["Work Direct Phone", "Mobile Phone", "Home Phone"]:
            safe = contact_phone_safe(clean_value(row.get(col)), company_phone)
            shaped.at[row.name, col] = safe

    return shaped[APOLLO_EXPORT_COLUMNS]


def lead_lookup(lead_df: pd.DataFrame) -> Dict[Tuple[str, str], pd.Series]:
    lookup: Dict[Tuple[str, str], pd.Series] = {}
    if lead_df.empty:
        return lookup
    for _, row in lead_df.iterrows():
        domain = normalize_domain(row.get("company_domain"))
        name = clean_value(row.get("company_name")).lower().strip()
        if domain:
            lookup[("domain", domain)] = row
        if name:
            lookup[("name", name)] = row
    return lookup


def review_lookup(review_df: pd.DataFrame) -> Dict[str, pd.Series]:
    lookup: Dict[str, pd.Series] = {}
    if review_df.empty or "apollo_person_id" not in review_df.columns:
        return lookup
    for _, row in review_df.iterrows():
        apollo_id = clean_value(row.get("apollo_person_id")).strip()
        if apollo_id:
            lookup[apollo_id] = row
    return lookup


def lookup_lead_row(leads: Dict[Tuple[str, str], pd.Series], domain: str, name: str) -> Optional[pd.Series]:
    if domain:
        row = leads.get(("domain", domain))
        if row is not None:
            return row
    if name:
        return leads.get(("name", name))
    return None


def enrich_with_lead_context(apollo_df: pd.DataFrame, lead_df: pd.DataFrame, review_df: pd.DataFrame) -> pd.DataFrame:
    df = ensure_apollo_export_shape(apollo_df)
    leads = lead_lookup(lead_df)
    reviews = review_lookup(review_df)

    for idx, row in df.iterrows():
        apollo_id = clean_value(row.get("Apollo Contact Id")).strip()
        review_row = reviews.get(apollo_id)

        domain = normalize_domain(row.get("Website"))
        name = clean_value(row.get("Company Name")).lower().strip()
        if review_row is not None:
            domain = normalize_domain(review_row.get("company_domain")) or domain
            name = clean_value(review_row.get("company_name")).lower().strip() or name

        lead_row = lookup_lead_row(leads, domain, name)
        if lead_row is not None:
            for dest, src in [
                ("Trigger", "trigger"),
                ("Mission (reasoning)", "reasoning"),
                ("Lead Score", "score"),
            ]:
                if not clean_value(df.at[idx, dest]):
                    df.at[idx, dest] = clean_value(lead_row.get(src))

        if review_row is not None:
            if not clean_value(df.at[idx, "Company Name"]):
                df.at[idx, "Company Name"] = clean_value(review_row.get("company_name"))
            if not clean_value(df.at[idx, "Website"]):
                df.at[idx, "Website"] = website_from_domain(review_row.get("company_domain"))

    return df


def build_skeleton_review(review_df: pd.DataFrame, lead_df: pd.DataFrame) -> pd.DataFrame:
    leads = lead_lookup(lead_df)
    rows = []
    for _, row in review_df.iterrows():
        domain = normalize_domain(row.get("company_domain"))
        name = clean_value(row.get("company_name")).lower().strip()
        lead_row = lookup_lead_row(leads, domain, name)
        title = clean_value(row.get("title"))
        mobile_required = senior_marketing_or_recruiting_title(title)
        rows.append(
            {
                "export_rank": row.get("export_rank", ""),
                "candidate_priority": row.get("candidate_priority", ""),
                "company_name": clean_value(row.get("company_name")),
                "company_domain": domain,
                "person_name": clean_value(row.get("person_name_preview")),
                "title": title,
                "email": "",
                "email_status": "apollo_search_signal" if truthy(row.get("has_email")) else "",
                "linkedin_url": "",
                "apollo_person_id": clean_value(row.get("apollo_person_id")),
                "contact_source": clean_value(row.get("contact_source")),
                "lead_contact_status": clean_value(row.get("lead_contact_status")),
                "mobile_phone_required": mobile_required,
                "mobile_phone_status": "needs_mobile_scrape" if mobile_required else "email_only_ok",
                "safe_contact_phone": "",
                "ready_for_notion_upload": False,
                "upload_blocker": "missing_enriched_email" if clean_value(row.get("review_status")) == "selected_for_review" else clean_value(row.get("review_status")),
                "trigger": clean_value(lead_row.get("trigger")) if lead_row is not None else "",
                "score": clean_value(lead_row.get("score")) if lead_row is not None else "",
                "reasoning": clean_value(lead_row.get("reasoning")) if lead_row is not None else "",
                "notes": clean_value(row.get("notes")),
            }
        )
    return pd.DataFrame(rows, columns=REVIEW_COLUMNS)


def build_enriched_review(apollo_df: pd.DataFrame, lead_df: pd.DataFrame, review_df: pd.DataFrame) -> pd.DataFrame:
    enriched = enrich_with_lead_context(apollo_df, lead_df, review_df)
    reviews = review_lookup(review_df)
    rows = []

    for _, row in enriched.iterrows():
        apollo_id = clean_value(row.get("Apollo Contact Id"))
        review_row = reviews.get(apollo_id)
        title = clean_value(row.get("Title"))
        company_phone = clean_value(row.get("Company Phone"))
        safe_phone = ""
        for col in ["Mobile Phone", "Work Direct Phone", "Home Phone"]:
            safe_phone = contact_phone_safe(clean_value(row.get(col)), company_phone)
            if safe_phone:
                break

        mobile_required = senior_marketing_or_recruiting_title(title)
        has_email = bool(clean_value(row.get("Email")) and "@" in clean_value(row.get("Email")))
        if mobile_required and safe_phone:
            mobile_status = "mobile_captured"
        elif mobile_required:
            mobile_status = "needs_mobile_scrape"
        else:
            mobile_status = "email_only_ok"

        blocker = ""
        if not has_email:
            blocker = "missing_email"
        elif mobile_status == "needs_mobile_scrape":
            blocker = "needs_mobile_scrape"

        ready = has_email and not blocker
        rows.append(
            {
                "export_rank": clean_value(review_row.get("export_rank")) if review_row is not None else "",
                "candidate_priority": clean_value(review_row.get("candidate_priority")) if review_row is not None else "",
                "company_name": clean_value(row.get("Company Name")),
                "company_domain": normalize_domain(row.get("Website")),
                "person_name": " ".join(v for v in [clean_value(row.get("First Name")), clean_value(row.get("Last Name"))] if v).strip(),
                "title": title,
                "email": clean_value(row.get("Email")),
                "email_status": clean_value(row.get("Email Status")),
                "linkedin_url": clean_value(row.get("Person Linkedin Url")),
                "apollo_person_id": apollo_id,
                "contact_source": clean_value(review_row.get("contact_source")) if review_row is not None else "apollo_enrichment",
                "lead_contact_status": clean_value(review_row.get("lead_contact_status")) if review_row is not None else "",
                "mobile_phone_required": mobile_required,
                "mobile_phone_status": mobile_status,
                "safe_contact_phone": safe_phone,
                "ready_for_notion_upload": ready,
                "upload_blocker": blocker,
                "trigger": clean_value(row.get("Trigger")),
                "score": clean_value(row.get("Lead Score")),
                "reasoning": clean_value(row.get("Mission (reasoning)")),
                "notes": clean_value(review_row.get("notes")) if review_row is not None else "",
            }
        )

    return pd.DataFrame(rows, columns=REVIEW_COLUMNS)


def append_unmatched_review_rows(
    review: pd.DataFrame,
    source_review_df: pd.DataFrame,
    lead_df: pd.DataFrame,
) -> pd.DataFrame:
    """Append known/unresolved/review-flag/missing-enrichment rows for full review."""
    if source_review_df.empty:
        return review

    represented_ids = set(review["apollo_person_id"].dropna().astype(str).str.strip()) if "apollo_person_id" in review.columns else set()
    leads = lead_lookup(lead_df)
    extra_rows = []

    for _, row in source_review_df.iterrows():
        apollo_id = clean_value(row.get("apollo_person_id")).strip()
        if apollo_id and apollo_id in represented_ids:
            continue

        status = clean_value(row.get("review_status")).strip()
        title = clean_value(row.get("title"))
        domain = normalize_domain(row.get("company_domain"))
        name = clean_value(row.get("company_name")).lower().strip()
        lead_row = lookup_lead_row(leads, domain, name)
        mobile_required = senior_marketing_or_recruiting_title(title)

        if status == "selected_for_review" and apollo_id:
            blocker = "apollo_enrichment_missing"
        elif status == "known_contact":
            blocker = "known_contact_needs_email"
        elif status == "unresolved":
            blocker = "no_person_found"
        elif status == "review_flag":
            blocker = "review_flag"
        else:
            blocker = status or "not_enriched"

        extra_rows.append(
            {
                "export_rank": clean_value(row.get("export_rank")),
                "candidate_priority": clean_value(row.get("candidate_priority")),
                "company_name": clean_value(row.get("company_name")),
                "company_domain": domain,
                "person_name": clean_value(row.get("person_name_preview")) or clean_value(row.get("person_name")),
                "title": title,
                "email": "",
                "email_status": "apollo_search_signal" if truthy(row.get("has_email")) else "",
                "linkedin_url": "",
                "apollo_person_id": apollo_id,
                "contact_source": clean_value(row.get("contact_source")),
                "lead_contact_status": clean_value(row.get("lead_contact_status")),
                "mobile_phone_required": mobile_required,
                "mobile_phone_status": "needs_mobile_scrape" if mobile_required else "email_only_ok",
                "safe_contact_phone": "",
                "ready_for_notion_upload": False,
                "upload_blocker": blocker,
                "trigger": clean_value(lead_row.get("trigger")) if lead_row is not None else "",
                "score": clean_value(lead_row.get("score")) if lead_row is not None else "",
                "reasoning": clean_value(lead_row.get("reasoning")) if lead_row is not None else "",
                "notes": clean_value(row.get("notes")),
            }
        )

    if not extra_rows:
        return review
    combined = pd.concat([review, pd.DataFrame(extra_rows, columns=REVIEW_COLUMNS)], ignore_index=True)
    sort_cols = [col for col in ["export_rank", "candidate_priority", "person_name"] if col in combined.columns]
    if sort_cols:
        combined = combined.sort_values(sort_cols, kind="stable", na_position="last")
    return combined[REVIEW_COLUMNS]


def upload_ready_from_review(apollo_df: pd.DataFrame, review_df: pd.DataFrame) -> pd.DataFrame:
    ready_ids = set(
        review_df.loc[review_df["ready_for_notion_upload"] == True, "apollo_person_id"]  # noqa: E712
        .dropna()
        .astype(str)
        .str.strip()
    )
    if not ready_ids:
        return pd.DataFrame(columns=APOLLO_EXPORT_COLUMNS)

    shaped = ensure_apollo_export_shape(apollo_df)
    ready = shaped[shaped["Apollo Contact Id"].astype(str).str.strip().isin(ready_ids)].copy()
    for col in ["Work Direct Phone", "Mobile Phone", "Home Phone"]:
        ready[col] = [
            contact_phone_safe(phone, company_phone)
            for phone, company_phone in zip(ready[col], ready["Company Phone"])
        ]
    return ready[APOLLO_EXPORT_COLUMNS]


def write_outputs(
    batches: Dict[str, Any],
    review: pd.DataFrame,
    upload_ready: pd.DataFrame,
    dry_run: bool,
) -> None:
    if dry_run:
        console.print("[yellow]Dry run - no files written[/yellow]")
        return
    APOLLO_ENRICHMENT_BATCHES_JSON.write_text(json.dumps(batches, indent=2), encoding="utf-8")
    review.to_csv(APOLLO_ENRICHED_REVIEW_CSV, index=False)
    upload_ready.to_csv(APOLLO_UPLOAD_READY_CSV, index=False)


def print_summary(
    batches: Dict[str, Any],
    review: pd.DataFrame,
    upload_ready: pd.DataFrame,
    stats: Optional[Dict[str, int]] = None,
) -> None:
    table = Table(title="Apollo Enrichment Flow")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("People in batch manifest", str(batches.get("total_people", 0)))
    table.add_row("Batch count", str(len(batches.get("batches", []))))
    if stats:
        table.add_row("Apollo requested", str(stats.get("requested", 0)))
        table.add_row("Apollo matched", str(stats.get("matched", 0)))
        table.add_row("Apollo missing", str(stats.get("missing", 0)))
        table.add_row("Credits consumed", str(stats.get("credits_consumed", 0)))
    table.add_row("Review rows", str(len(review)))
    if not review.empty and "mobile_phone_status" in review.columns:
        table.add_row("Needs mobile scrape", str((review["mobile_phone_status"] == "needs_mobile_scrape").sum()))
    if not review.empty and "upload_blocker" in review.columns:
        table.add_row("Blocked from upload", str((review["upload_blocker"].astype(str).str.strip() != "").sum()))
    table.add_row("Upload-ready rows", str(len(upload_ready)))
    console.print(table)

    console.print(f"[dim]Batch manifest  -> {APOLLO_ENRICHMENT_BATCHES_JSON.name}[/dim]")
    console.print(f"[dim]Review CSV      -> {APOLLO_ENRICHED_REVIEW_CSV.name}[/dim]")
    console.print(f"[dim]Upload-ready CSV -> {APOLLO_UPLOAD_READY_CSV.name}[/dim]")


def run(
    review_csv: str = "",
    lead_csv: str = "",
    apollo_csv: str = "",
    mcp_json: str = "",
    batch_size: int = 10,
    dry_run: bool = False,
) -> None:
    review_path = Path(review_csv) if review_csv else APOLLO_CONTACT_REVIEW_CSV
    lead_path = Path(lead_csv) if lead_csv else APOLLO_READY_CSV
    review_df = load_csv(review_path)
    lead_df = load_csv(lead_path)

    if review_df.empty:
        console.print(f"[red]No review CSV found/readable: {review_path}[/red]")
        return

    review_df, review_removed = filter_excluded_companies(review_df)
    lead_df, lead_removed = filter_excluded_companies(lead_df)
    removed_total = review_removed + lead_removed
    if removed_total:
        console.print(f"[yellow]Excluded {removed_total} blocked company row(s) before Apollo artifact generation[/yellow]")

    batches = build_enrichment_batches(review_df, batch_size=batch_size)
    stats: Optional[Dict[str, int]] = None
    upload_ready = pd.DataFrame(columns=APOLLO_EXPORT_COLUMNS)

    apollo_df = pd.DataFrame()
    if mcp_json:
        responses = load_mcp_responses(Path(mcp_json))
        apollo_df, stats = mcp_responses_to_apollo_df(responses)
    elif apollo_csv:
        apollo_df = ensure_apollo_export_shape(pd.read_csv(apollo_csv))

    apollo_df, apollo_removed = filter_excluded_companies(apollo_df)
    if apollo_removed:
        console.print(f"[yellow]Excluded {apollo_removed} blocked Apollo enriched row(s)[/yellow]")

    if apollo_df.empty:
        review = build_skeleton_review(review_df, lead_df)
    else:
        apollo_df = enrich_with_lead_context(apollo_df, lead_df, review_df)
        review = build_enriched_review(apollo_df, lead_df, review_df)
        review = append_unmatched_review_rows(review, review_df, lead_df)
        upload_ready = upload_ready_from_review(apollo_df, review)

    write_outputs(batches, review, upload_ready, dry_run=dry_run)
    print_summary(batches, review, upload_ready, stats=stats)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare Apollo enrichment review/upload artifacts")
    parser.add_argument("--review-csv", default="", help="Apollo candidate review CSV")
    parser.add_argument("--lead-csv", default="", help="Ranking joint Apollo-ready CSV")
    parser.add_argument("--apollo-csv", default="", help="Apollo UI export CSV")
    parser.add_argument("--mcp-json", default="", help="Apollo MCP JSON/JSONL/session-log capture")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(
        review_csv=args.review_csv,
        lead_csv=args.lead_csv,
        apollo_csv=args.apollo_csv,
        mcp_json=args.mcp_json,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
