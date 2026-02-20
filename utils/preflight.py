"""
Preflight Validator — checks Notion DB schemas before any import runs.

Catches property name mismatches (typos, missing stars, plural/singular)
before they cause silent API failures or data loss.

Usage:
    from utils.preflight import run_preflight

    result = run_preflight(accounts_db_id, contacts_db_id)
    if not result.success:
        sys.exit(1)
    # Pass result.prop_map and result.status_map to notion_client functions
"""
import difflib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests as http_requests
from rich.console import Console

from utils.config import NOTION_TOKEN

console = Console()

# Notion API config
NOTION_API_VERSION = "2022-06-28"


def _notion_api_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }


# ---------------------------------------------------------------------------
# Expected properties for each database
# ---------------------------------------------------------------------------
# These are ALL property names used by upload_agent, copywriter_agent,
# ranking_agent, and notion_cleanup when writing to Notion.
# Format: {property_name: property_type}

EXPECTED_ACCOUNTS_PROPERTIES: Dict[str, str] = {
    # Core fields (used by upload_agent create/update)
    "Organization*": "title",
    "Status": "status",
    "Campaign ID": "multi_select",
    "Cleaned Name*": "rich_text",
    "Website URL*": "url",
    "Company LinkedIn": "url",
    "City": "select",
    "Country": "multi_select",
    "Company Phone Number": "phone_number",
    "Industry (Corporates)": "rich_text",
    "Trigger Event": "rich_text",
    "Mission*": "rich_text",
    "Lead Score": "number",
    "# Employees": "number",
    "Latest Funding": "rich_text",
    "Funding Amount": "number",
    "Apollo Account ID": "rich_text",
    # Read by get_existing_accounts / copywriter context
    "Account Type*": "select",
    "Company Description": "rich_text",
}

EXPECTED_CONTACTS_PROPERTIES: Dict[str, str] = {
    # Core fields (used by create_contact_in_notion)
    "Contact Name": "title",
    "LinkedIn": "url",
    "Email": "email",
    "Accounts": "relation",
    "Job Title": "rich_text",
    "Phone": "phone_number",
    "Apollo Contact ID": "rich_text",
    # Outreach fields (used by copywriter_agent)
    "LinkedIn 1st Cold": "rich_text",
    "LinkedIn FU message": "rich_text",
    "Cold Email Body": "rich_text",
    "Cold Email Subject": "rich_text",
    "AB Variant": "select",
    # Rollups (read-only, used by copywriter filtering)
    "Campaign ID": "rollup",
    "Account Status": "rollup",
}


# ---------------------------------------------------------------------------
# PreflightResult
# ---------------------------------------------------------------------------

@dataclass
class PreflightResult:
    """Result of a preflight validation run."""
    success: bool
    prop_map: Dict[str, str] = field(default_factory=dict)
    status_map: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema fetching
# ---------------------------------------------------------------------------

def fetch_db_schema(database_id: str) -> Optional[Dict[str, dict]]:
    """
    Fetch the property schema of a Notion database.

    Returns:
        Dict mapping property_name → {type, config} or None on error.
    """
    if not NOTION_TOKEN:
        console.print("[red]Preflight: NOTION_TOKEN not configured[/red]")
        return None

    headers = _notion_api_headers()

    try:
        resp = http_requests.get(
            f"https://api.notion.com/v1/databases/{database_id}",
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            console.print(f"[red]Preflight: Failed to fetch DB schema: {resp.status_code} - {resp.json().get('message', '')}[/red]")
            return None

        data = resp.json()
        properties = data.get("properties", {})

        schema = {}
        for prop_name, prop_data in properties.items():
            prop_type = prop_data.get("type", "unknown")
            schema[prop_name] = {
                "type": prop_type,
                "config": prop_data.get(prop_type, {}),
            }

        return schema

    except Exception as e:
        console.print(f"[red]Preflight: Error fetching DB schema: {e}[/red]")
        return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_properties(
    expected: Dict[str, str],
    actual_schema: Dict[str, dict],
    db_label: str,
) -> Tuple[Dict[str, str], List[str], List[str]]:
    """
    Validate expected properties against the actual Notion DB schema.

    Args:
        expected: {property_name: expected_type}
        actual_schema: Schema from fetch_db_schema()
        db_label: Human-readable DB name for logging

    Returns:
        (prop_map, warnings, errors)
        prop_map: {expected_name: actual_name} — identity for exact matches,
                  fuzzy match name for close matches.
    """
    prop_map: Dict[str, str] = {}
    warnings: List[str] = []
    errors: List[str] = []

    actual_names = list(actual_schema.keys())

    for expected_name, expected_type in expected.items():
        if expected_name in actual_schema:
            # Exact match
            actual_type = actual_schema[expected_name]["type"]
            if actual_type == expected_type:
                prop_map[expected_name] = expected_name
                console.print(f"  [dim]{db_label}: \"{expected_name}\" ({expected_type}) ✓[/dim]")
            else:
                # Name matches but type differs — warning
                prop_map[expected_name] = expected_name
                msg = f"{db_label}: \"{expected_name}\" exists but type is '{actual_type}', expected '{expected_type}'"
                warnings.append(msg)
                console.print(f"  [yellow]{msg}[/yellow]")
        else:
            # No exact match — try fuzzy
            close = difflib.get_close_matches(expected_name, actual_names, n=1, cutoff=0.75)
            if close:
                fuzzy_name = close[0]
                actual_type = actual_schema[fuzzy_name]["type"]
                type_ok = actual_type == expected_type

                prop_map[expected_name] = fuzzy_name

                if type_ok:
                    msg = f"{db_label}: \"{expected_name}\" not found — fuzzy matched to \"{fuzzy_name}\" ({actual_type})"
                    warnings.append(msg)
                    console.print(f"  [yellow]{msg}[/yellow]")
                else:
                    msg = f"{db_label}: \"{expected_name}\" fuzzy → \"{fuzzy_name}\" but type '{actual_type}' != expected '{expected_type}'"
                    warnings.append(msg)
                    console.print(f"  [yellow]{msg}[/yellow]")
            else:
                msg = f"{db_label}: \"{expected_name}\" ({expected_type}) NOT FOUND in database"
                errors.append(msg)
                console.print(f"  [red]{msg}[/red]")

    return prop_map, warnings, errors


def validate_status_options(
    actual_schema: Dict[str, dict],
    db_label: str,
) -> Tuple[Dict[str, str], List[str], List[str]]:
    """
    Validate STATUS_HIERARCHY values against the actual Status property options.

    Returns:
        (status_map, warnings, errors)
        status_map: {expected_status: actual_status} — identity or fuzzy match.
    """
    from agents.notion_cleanup import STATUS_HIERARCHY

    status_map: Dict[str, str] = {}
    warnings: List[str] = []
    errors: List[str] = []

    # Find the Status property in the schema
    status_prop = actual_schema.get("Status")
    if not status_prop:
        errors.append(f"{db_label}: No 'Status' property found — cannot validate status options")
        console.print(f"  [red]{errors[-1]}[/red]")
        return status_map, warnings, errors

    if status_prop["type"] != "status":
        errors.append(f"{db_label}: 'Status' property type is '{status_prop['type']}', expected 'status'")
        console.print(f"  [red]{errors[-1]}[/red]")
        return status_map, warnings, errors

    # Extract status option names from the schema config
    config = status_prop.get("config", {})
    option_groups = config.get("options", [])
    # Also check groups (Notion status has groups with options inside)
    groups = config.get("groups", [])

    actual_options = set()
    for opt in option_groups:
        name = opt.get("name", "")
        if name:
            actual_options.add(name)
    for group in groups:
        for opt in group.get("option_ids", []):
            pass  # IDs only, names are in the options list

    actual_option_list = sorted(actual_options)

    if not actual_options:
        warnings.append(f"{db_label}: Could not extract status options from schema (may still work at runtime)")
        console.print(f"  [yellow]{warnings[-1]}[/yellow]")
        # Map everything to identity
        for status in STATUS_HIERARCHY:
            status_map[status] = status
        return status_map, warnings, errors

    console.print(f"  [dim]{db_label}: Found {len(actual_options)} status options[/dim]")

    for expected_status in STATUS_HIERARCHY:
        if expected_status in actual_options:
            status_map[expected_status] = expected_status
            console.print(f"  [dim]  Status \"{expected_status}\" ✓[/dim]")
        else:
            close = difflib.get_close_matches(expected_status, actual_option_list, n=1, cutoff=0.75)
            if close:
                fuzzy = close[0]
                status_map[expected_status] = fuzzy
                msg = f"{db_label}: Status \"{expected_status}\" not found — fuzzy matched to \"{fuzzy}\""
                warnings.append(msg)
                console.print(f"  [yellow]{msg}[/yellow]")
            else:
                # Not found — map to identity (Notion will create it on write, or error)
                status_map[expected_status] = expected_status
                msg = f"{db_label}: Status \"{expected_status}\" NOT FOUND in database options"
                warnings.append(msg)
                console.print(f"  [yellow]{msg}[/yellow]")

    return status_map, warnings, errors


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_preflight(
    accounts_db_id: str,
    contacts_db_id: str,
) -> PreflightResult:
    """
    Run preflight validation against both Notion databases.

    Fetches schemas, validates property names and types, validates status options.

    Args:
        accounts_db_id: Notion Accounts database ID
        contacts_db_id: Notion Contacts database ID

    Returns:
        PreflightResult with success flag, property/status maps, warnings, errors.
    """
    console.print("\n[bold cyan]Preflight Validation[/bold cyan]")
    console.print("=" * 40)

    all_warnings: List[str] = []
    all_errors: List[str] = []
    full_prop_map: Dict[str, str] = {}
    full_status_map: Dict[str, str] = {}

    # --- Accounts DB ---
    console.print("\n[cyan]Checking Accounts database...[/cyan]")
    accounts_schema = fetch_db_schema(accounts_db_id)
    if accounts_schema is None:
        all_errors.append("Failed to fetch Accounts DB schema")
    else:
        prop_map, warns, errs = validate_properties(
            EXPECTED_ACCOUNTS_PROPERTIES, accounts_schema, "Accounts"
        )
        full_prop_map.update(prop_map)
        all_warnings.extend(warns)
        all_errors.extend(errs)

        # Validate status options
        status_map, s_warns, s_errs = validate_status_options(accounts_schema, "Accounts")
        full_status_map.update(status_map)
        all_warnings.extend(s_warns)
        all_errors.extend(s_errs)

    # --- Contacts DB ---
    console.print("\n[cyan]Checking Contacts database...[/cyan]")
    contacts_schema = fetch_db_schema(contacts_db_id)
    if contacts_schema is None:
        all_errors.append("Failed to fetch Contacts DB schema")
    else:
        prop_map, warns, errs = validate_properties(
            EXPECTED_CONTACTS_PROPERTIES, contacts_schema, "Contacts"
        )
        full_prop_map.update(prop_map)
        all_warnings.extend(warns)
        all_errors.extend(errs)

    # --- Summary ---
    console.print("\n" + "-" * 40)
    if all_errors:
        console.print(f"[red]Preflight FAILED: {len(all_errors)} error(s), {len(all_warnings)} warning(s)[/red]")
        for err in all_errors:
            console.print(f"  [red]✗ {err}[/red]")
        for warn in all_warnings:
            console.print(f"  [yellow]⚠ {warn}[/yellow]")
    elif all_warnings:
        console.print(f"[yellow]Preflight PASSED with {len(all_warnings)} warning(s)[/yellow]")
        for warn in all_warnings:
            console.print(f"  [yellow]⚠ {warn}[/yellow]")
    else:
        console.print("[green]Preflight PASSED — all properties match[/green]")

    console.print("")

    return PreflightResult(
        success=len(all_errors) == 0,
        prop_map=full_prop_map,
        status_map=full_status_map,
        warnings=all_warnings,
        errors=all_errors,
    )
