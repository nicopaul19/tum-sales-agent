"""
Create Impact Hack 2026 outreach drafts for selected Notion contacts.

Scope:
- Contacts linked to accounts in the configured target campaigns.
- Account status exactly Prospect Qualified or Contacted*.
- Writes subject/body to Notion contact outreach fields.
- Creates or updates Gmail drafts without attachments.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable

import requests
from openai import OpenAI
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import (  # noqa: E402
    DATA_DIR,
    DEFAULT_CAMPAIGN_SENDER,
    NOTION_DB_CONTACTS_ID,
    NOTION_TOKEN,
    OPENAI_API_KEY,
)
from utils.gmail_client import SENDER_ADDRESS, SENDER_DISPLAY, _get_gmail_service  # noqa: E402


console = Console()

TARGET_CAMPAIGNS = {
    "Corporates_AIJobs_2511",
    "Corporates_TUMaffiliation_310925",
    "Workflow_0902",
    "Workflow_2002",
    "Workflow_2003",
}

ALLOWED_ACCOUNT_STATUSES = {
    "Prospect Qualified",
    "Contacted LinkedIn 🌐",
    "Contacted Mail 📩",
    "Contacted Email 📧",
}

EVENT_FACTS = """
Impact Hack 2026 is an AI-for-Good hackathon in Munich on 27-28 June 2026.
It will host 100-150 participants at TUM Munich, Lecture Hall 1100 and Immatrikulationshalle.
The organizers are TUM Social AI, TUM.ai, NamibAI, and Studierendenforum im Toenissteiner Kreis.
OpenAI and Lovable are already supporting the event with ChatGPT Pro, API credits, and Lovable credits.
The hackathon brings students from TUM, LMU, HM and beyond, across CS, engineering, law, policy, social sciences, and business.
Challenges focus on solving real-world problems for nonprofit partners like the United Nations and VENRO, covering humanitarian crises, development cooperation, conservation, animal welfare in Africa, and Women in AI.
Partner opportunities include keynote stage visibility, challenge tracks, booth space, CV access, social media presence, tech-stack/API/cloud-credit support, and sponsorship.
""".strip()

HACKATHON_TRIGGER = (
    "Upcoming Impact Hackathon in Munich on 27-28 June under the motto "
    "\"Code. Collaborate. Create Impact.\" Expected 100-150 students from "
    "TUM, LMU and HM; organized by TUM Social AI, TUM.ai, NamibAI and "
    "Studierendenforum, with OpenAI and Lovable supporting the tech stack. "
    "Challenges focus on real-world problems for nonprofit partners like the United Nations and VENRO."
)


class EmailDraft(BaseModel):
    subject: str = Field(description="Short email subject, no placeholders.")
    body: str = Field(description="Plain-text email body, ready to send.")


@dataclass
class ContactRecord:
    page_id: str
    name: str
    email: str
    job_title: str
    account_id: str
    account_status: str
    campaigns: list[str]
    company_name: str = ""
    website: str = ""
    industry: str = ""
    mission: str = ""
    company_description: str = ""
    trigger: str = ""
    account_type: str = ""
    city: str = ""
    country: str = ""
    campaign_sender: str = ""
    account_owner: str = ""
    sender_name: str = ""
    existing_subject: str = ""
    existing_body: str = ""
    duplicate_key: str = ""
    duplicate_of: str = ""
    generated_subject: str = ""
    generated_body: str = ""
    notion_written: bool = False
    gmail_draft_id: str = ""
    gmail_action: str = ""
    error: str = ""


def notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def notion_request(method: str, url: str, **kwargs) -> requests.Response:
    for attempt in range(8):
        resp = requests.request(method, url, headers=notion_headers(), timeout=30, **kwargs)
        if resp.status_code not in {429, 500, 502, 503, 504}:
            time.sleep(0.35)
            return resp
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            wait = int(retry_after)
        else:
            wait = min(2 ** attempt, 20)
        console.print(f"[yellow]Notion {resp.status_code}, retrying in {wait}s[/yellow]")
        time.sleep(wait)
    return resp


def query_database(db_id: str) -> list[dict]:
    results: list[dict] = []
    cursor = None
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = notion_request("POST", url, json=body)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            return results
        cursor = data.get("next_cursor")


def fetch_page(page_id: str) -> dict:
    resp = notion_request("GET", f"https://api.notion.com/v1/pages/{page_id}")
    resp.raise_for_status()
    return resp.json()


def plain_title(props: dict) -> str:
    for prop in props.values():
        if prop.get("type") == "title":
            return "".join(part.get("plain_text", "") for part in prop.get("title", []))
    return ""


def rich_text(props: dict, name: str) -> str:
    prop = props.get(name, {})
    if prop.get("type") == "rich_text":
        return "".join(part.get("plain_text", "") for part in prop.get("rich_text", []))
    if prop.get("type") == "formula" and prop.get("formula", {}).get("type") == "string":
        return prop["formula"].get("string") or ""
    return ""


def people_names_from_array(people: list[dict]) -> list[str]:
    names: list[str] = []
    for person in people:
        name = person.get("name") or person.get("person", {}).get("email", "")
        if name:
            names.append(name)
    return names


def people_value(props: dict, name: str) -> str:
    prop = props.get(name, {})
    if prop.get("type") != "people":
        return ""
    return ", ".join(people_names_from_array(prop.get("people", [])))


def rollup_people_value(props: dict, name: str) -> str:
    prop = props.get(name, {})
    if prop.get("type") != "rollup":
        return ""
    names: list[str] = []
    for item in prop.get("rollup", {}).get("array", []):
        if item.get("type") == "people":
            names.extend(people_names_from_array(item.get("people", [])))
        elif item.get("type") == "rich_text":
            names.extend(rt.get("plain_text", "") for rt in item.get("rich_text", []))
    return ", ".join(name for name in names if name)


def email_value(props: dict, name: str) -> str:
    prop = props.get(name, {})
    return prop.get("email") or "" if prop.get("type") == "email" else ""


def url_value(props: dict, name: str) -> str:
    prop = props.get(name, {})
    return prop.get("url") or "" if prop.get("type") == "url" else ""


def select_value(props: dict, name: str) -> str:
    prop = props.get(name, {})
    return prop.get("select", {}).get("name", "") if prop.get("type") == "select" and prop.get("select") else ""


def multi_select_values(props: dict, name: str) -> list[str]:
    prop = props.get(name, {})
    if prop.get("type") != "multi_select":
        return []
    return [item.get("name", "") for item in prop.get("multi_select", []) if item.get("name")]


def rollup_campaigns(props: dict) -> list[str]:
    prop = props.get("Campaign ID", {})
    names: list[str] = []
    if prop.get("type") != "rollup":
        return names
    for item in prop.get("rollup", {}).get("array", []):
        if item.get("type") == "multi_select":
            names.extend(ms.get("name", "") for ms in item.get("multi_select", []))
        elif item.get("type") == "select" and item.get("select"):
            names.append(item["select"].get("name", ""))
        elif item.get("type") == "rich_text":
            names.extend(rt.get("plain_text", "") for rt in item.get("rich_text", []))
    return sorted({name for name in names if name})


def rollup_account_status(props: dict) -> str:
    prop = props.get("Account Status", {})
    if prop.get("type") != "rollup":
        return ""
    for item in prop.get("rollup", {}).get("array", []):
        if item.get("type") == "status" and item.get("status"):
            return item["status"].get("name", "")
    return ""


def first_relation_id(props: dict, name: str) -> str:
    prop = props.get(name, {})
    if prop.get("type") == "relation" and prop.get("relation"):
        return prop["relation"][0].get("id", "")
    return ""


def account_context(account_page: dict) -> dict[str, str]:
    props = account_page.get("properties", {})
    countries = multi_select_values(props, "Country")
    return {
        "company_name": plain_title(props),
        "website": url_value(props, "Website URL*"),
        "industry": rich_text(props, "Industry (Corporates)"),
        "mission": rich_text(props, "Mission*"),
        "company_description": rich_text(props, "Company Description"),
        "trigger": rich_text(props, "Trigger Event"),
        "account_type": select_value(props, "Account Type*"),
        "city": select_value(props, "City"),
        "country": countries[0] if countries else "",
        "campaign_sender": rich_text(props, "Campaign Sender"),
        "account_owner": people_value(props, "Owner*"),
    }


def load_contacts(
    limit: int = 0,
    target_campaigns: set[str] | None = None,
    allowed_account_statuses: set[str] | None = ALLOWED_ACCOUNT_STATUSES,
) -> list[ContactRecord]:
    pages = query_database(NOTION_DB_CONTACTS_ID)
    account_cache: dict[str, dict[str, str]] = {}
    contacts: list[ContactRecord] = []
    target_campaigns = target_campaigns or TARGET_CAMPAIGNS

    for page in pages:
        props = page.get("properties", {})
        campaigns = sorted(target_campaigns.intersection(rollup_campaigns(props)))
        account_status = rollup_account_status(props)
        if not campaigns:
            continue
        if allowed_account_statuses is not None and account_status not in allowed_account_statuses:
            continue

        account_id = first_relation_id(props, "Accounts")
        context = {}
        if account_id:
            if account_id not in account_cache:
                account_cache[account_id] = account_context(fetch_page(account_id))
            context = account_cache[account_id]

        contact_campaign_sender = rich_text(props, "Campaign Sender").strip()
        contact_account_owner = (
            rollup_people_value(props, "Account Owner").strip()
            or people_value(props, "Contact Owner").strip()
        )
        context_sender = context.get("campaign_sender", "").strip()
        context_owner = context.get("account_owner", "").strip()
        contact_context = dict(context)
        contact_context.pop("campaign_sender", None)
        contact_context.pop("account_owner", None)

        record = ContactRecord(
            page_id=page.get("id", ""),
            name=plain_title(props).strip(),
            email=email_value(props, "Email").strip(),
            job_title=rich_text(props, "Job Title").strip(),
            account_id=account_id,
            account_status=account_status,
            campaigns=campaigns,
            existing_subject=rich_text(props, "Cold Email Subject"),
            existing_body=rich_text(props, "Cold Email Body"),
            campaign_sender=contact_campaign_sender or context_sender,
            account_owner=contact_account_owner or context_owner,
            **contact_context,
        )
        record.sender_name = (
            record.account_owner
            or record.campaign_sender
            or DEFAULT_CAMPAIGN_SENDER
            or "Felix Laumann"
        ).strip()
        key_base = (record.email or f"no-email:{record.page_id}").lower()
        record.duplicate_key = f"{key_base}|{record.account_id or record.company_name.lower()}"
        contacts.append(record)
        if limit and len(contacts) >= limit:
            break

    return contacts


def first_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        return ""
    return clean.split()[0].strip(",")


def persona_hint(job_title: str, campaigns: Iterable[str]) -> str:
    title = (job_title or "").lower()
    campaign_set = set(campaigns)
    if any(term in title for term in ["talent", "recruit", "people", "hr", "employer"]):
        return "Talent/recruiting: emphasize CV access, booth/workshop options, and direct visibility among TUM/LMU/HM builders."
    if any(term in title for term in ["brand", "marketing", "communication", "community", "events"]):
        return "Brand/marketing: emphasize keynote stage, booth, social media, and campus ecosystem visibility."
    if any(term in title for term in ["sustain", "csr", "impact", "esg"]):
        return "Impact/CSR: emphasize AI-for-Good, nonprofit challenges, Women in AI, and measurable social-impact visibility."
    if any(term in title for term in ["engineer", "product", "developer", "cto", "technical"]):
        return "Product/engineering: emphasize OpenAI/Lovable stack, challenge track/workshop, and technical students as power users."
    if any(term in title for term in ["founder", "ceo", "coo", "cmo", "chief", "partner", "business development", "bd"]):
        return "Leadership/partnerships: emphasize strategic partner/sponsor positioning in Munich's AI-for-Good ecosystem."
    if "Corporates_AIJobs_2511" in campaign_set:
        return "AI jobs campaign: emphasize access to AI engineering talent and recruiting visibility."
    if "Corporates_TUMaffiliation_310925" in campaign_set:
        return "TUM affiliation campaign: emphasize TUM ecosystem credibility and campus presence."
    return "General corporate partner: emphasize a concrete partnership/sponsorship angle and ecosystem visibility."


def short_company_name(company_name: str) -> str:
    cleaned = " ".join((company_name or "your team").split())
    if " - " in cleaned:
        cleaned = cleaned.split(" - ", 1)[0].strip()
    suffixes = [
        " GmbH & Co. KG",
        " International GmbH",
        " Deutschland GmbH",
        " Germany GmbH",
        " GmbH",
        " AG",
        " SE",
        " Inc.",
        " Inc",
        " Ltd.",
        " Ltd",
        " LLC",
    ]
    for suffix in suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break
    if len(cleaned) > 42:
        clipped = cleaned[:42].rsplit(" ", 1)[0].strip()
        cleaned = clipped or cleaned[:42].strip()
    return cleaned or "your team"


def template_angle(record: ContactRecord) -> str:
    company = short_company_name(record.company_name)
    title = (record.job_title or "").lower()
    campaign_set = set(record.campaigns)

    if any(term in title for term in ["talent", "recruit", "people", "hr", "employer"]) or "Corporates_AIJobs_2511" in campaign_set:
        return (
            f"For {company}, this could be a strong talent access opportunity: meet AI and "
            "engineering talents early and position the team as an exciting tech brand on campus."
        )
    if any(term in title for term in ["brand", "marketing", "communication", "community", "events"]):
        return (
            f"For {company}, this could be a concrete brand visibility opportunity: keynote-stage presence, "
            "booth conversations and social reach with Munich's technical student crowd."
        )
    if any(term in title for term in ["sustain", "csr", "impact", "esg"]):
        return (
            f"For {company}, this could be a strong CSR and impact play: visible support for "
            "AI-for-Good, Women in AI and student teams tackling real nonprofit challenges."
        )
    if any(term in title for term in ["engineer", "product", "developer", "cto", "technical"]):
        return (
            f"For {company}, this could be a sharp tech-community play: a workshop or challenge "
            "track with builders using OpenAI, Lovable and modern AI tooling under real hackathon pressure."
        )
    if any(term in title for term in ["founder", "ceo", "coo", "cmo", "chief", "partner", "business development", "bd"]):
        return (
            f"For {company}, this could be a clean Munich ecosystem play: stage visibility, "
            "challenge-track ownership and access to a very relevant TUM/LMU/HM crowd."
        )
    if "Corporates_TUMaffiliation_310925" in campaign_set:
        return (
            f"For {company}, this could be a natural TUM ecosystem play: stage visibility, "
            "a partner role and direct access to Munich's university builder crowd."
        )
    return (
        f"For {company}, this could be a strong partnership play: stage and booth visibility, "
        "a challenge track and direct access to Munich's university builder crowd."
    )


def generate_template_email(record: ContactRecord, sender: str = "") -> EmailDraft:
    company = short_company_name(record.company_name)
    greeting = first_name(record.name) or f"{company} team"
    sender_name = (sender or record.sender_name or DEFAULT_CAMPAIGN_SENDER or "Felix Laumann").strip()
    body = (
        f"Hi {greeting},\n\n"
        f"are you currently trying to get more visibility in the Munich University Eco-System for {company}? "
        "We have our first Impact Hackathon coming up in Munich on 27-28 June under the motto "
        "\"Code. Collaborate. Create Impact.\" We expect 100-150 students from TUM, LMU and HM, "
        "with TUM Social AI, TUM.ai, NamibAI and Studierendenforum behind it, plus OpenAI and Lovable "
        "already supporting the tech stack. The challenges will be focused on solving real-world problems "
        "for our nonprofit partners like the United Nations or VENRO.\n\n"
        f"{template_angle(record)}\n\n"
        "We are looking for sponsors and tech-stack/API/cloud-credit partners. In return we can offer "
        "keynote-stage visibility, setting up a booth at the Immatrikulationshalle, exclusive CV access "
        "and social media reach inside Munich's AI ecosystem.\n\n"
        "Would a partner slot be relevant for your team?\n\n"
        "Best\n"
        f"{sender_name}"
    )
    return EmailDraft(subject=f"Impact Hackathon x {company}: Collaboration Opportunity", body=body)


def prompt_for(record: ContactRecord, sender: str) -> str:
    greeting = first_name(record.name) or f"{record.company_name} team"
    contacted_note = (
        "This account has already been contacted. Make it feel like a concrete, fresh follow-up triggered by Impact Hack, not a first intro."
        if record.account_status.startswith("Contacted")
        else "This is a first outbound email for a qualified prospect."
    )
    company_bits = "\n".join(
        f"- {label}: {value}"
        for label, value in [
            ("Company", record.company_name),
            ("Website", record.website),
            ("Industry", record.industry),
            ("Mission", record.mission),
            ("Company description", record.company_description),
            ("Existing trigger", record.trigger),
            ("Location", ", ".join(part for part in [record.city, record.country] if part)),
        ]
        if value
    )
    contact_bits = "\n".join(
        f"- {label}: {value}"
        for label, value in [
            ("Contact", record.name),
            ("Greeting first name", greeting),
            ("Job title", record.job_title),
            ("Account status", record.account_status),
            ("Campaigns", ", ".join(record.campaigns)),
        ]
        if value
    )
    return f"""
Create one personalized corporate partnership outreach email.

EVENT FACTS:
{EVENT_FACTS}

CONTACT:
{contact_bits}

ACCOUNT:
{company_bits or "- No extra account context available."}

PERSONA ANGLE:
{persona_hint(record.job_title, record.campaigns)}

SENDER:
- Sign off exactly with: {sender}

RULES:
- Plain English.
- Subject should be short, specific, and usually include "Impact Hack" plus the company name.
- Body max 115 words including greeting and sign-off.
- Start with "Hi {greeting},".
- Keep it lean, concrete, and excited.
- Use namedropping naturally: TUM, TUM.ai, TUM Social AI, OpenAI, Lovable, NamibAI, Studierendenforum, TUM/LMU/HM, UN/VENRO, Women in AI.
- Include at least one company/persona-specific sentence.
- CTA should ask whether partnering, sponsoring, or taking a concrete event role is relevant for their team.
- No emojis. No em dash or en dash. No placeholders. Do not invent company facts.
- {contacted_note}
""".strip()


def generate_email(client: OpenAI, record: ContactRecord, sender: str, model: str) -> EmailDraft:
    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You write concise, high-energy but credible partnership emails for TUM Social AI. "
                    "Return only the requested structured fields. Do not use em dashes, en dashes, emojis, or fake facts."
                ),
            },
            {"role": "user", "content": prompt_for(record, sender)},
        ],
        response_format=EmailDraft,
        max_tokens=700,
    )
    draft = completion.choices[0].message.parsed
    draft.subject = draft.subject.replace("—", "-").replace("–", "-").strip()
    draft.body = draft.body.replace("—", "-").replace("–", "-").strip()
    return draft


def update_notion_contact(record: ContactRecord) -> bool:
    properties = {
        "LinkedIn 1st Cold": {"rich_text": []},
        "LinkedIn FU message": {"rich_text": []},
        "Cold Email Subject": {"rich_text": [{"text": {"content": record.generated_subject[:2000]}}]},
        "Cold Email Body": {"rich_text": [{"text": {"content": record.generated_body[:2000]}}]},
        "AB Variant": {"select": None},
    }
    resp = notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{record.page_id}",
        json={"properties": properties},
    )
    if resp.status_code != 200:
        record.error = f"Notion update failed: {resp.status_code} {resp.text[:200]}"
        return False
    return True


def update_notion_account_trigger(account_id: str) -> bool:
    if not account_id:
        return True
    resp = notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{account_id}",
        json={
            "properties": {
                "Trigger Event": {"rich_text": [{"text": {"content": HACKATHON_TRIGGER[:2000]}}]},
            }
        },
    )
    return resp.status_code == 200


def raw_message(record: ContactRecord) -> str:
    msg = MIMEMultipart("mixed")
    msg["Subject"] = record.generated_subject
    msg["From"] = f"{SENDER_DISPLAY} <{SENDER_ADDRESS}>"
    msg["To"] = record.email
    msg.attach(MIMEText(record.generated_body, "plain", "utf-8"))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


def create_gmail_draft(service, record: ContactRecord) -> str:
    if not record.email or "@" not in record.email:
        return ""

    draft = service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw_message(record)}},
    ).execute()
    return draft.get("id", "")


def update_gmail_draft(service, draft_id: str, record: ContactRecord) -> str:
    if not draft_id:
        return create_gmail_draft(service, record)
    draft = service.users().drafts().update(
        userId="me",
        id=draft_id,
        body={"id": draft_id, "message": {"raw": raw_message(record)}},
    ).execute()
    return draft.get("id", draft_id)


def load_draft_ids_from_audit(path: str) -> dict[str, str]:
    if not path:
        return {}
    draft_ids: dict[str, str] = {}
    with Path(path).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            page_id = row.get("contact_page_id", "")
            draft_id = row.get("gmail_draft_id", "")
            if page_id and draft_id:
                draft_ids[page_id] = draft_id
    return draft_ids


def write_audit(records: list[ContactRecord], dry_run: bool) -> Path:
    reports_dir = DATA_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    suffix = "dry_run" if dry_run else "live"
    path = reports_dir / f"impact_hack_drafts_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{suffix}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "contact_page_id",
                "name",
                "email",
                "company_name",
                "job_title",
                "account_status",
                "campaigns",
                "subject",
                "body",
                "notion_written",
                "gmail_draft_id",
                "gmail_action",
                "duplicate_of",
                "sender_name",
                "error",
            ],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "contact_page_id": record.page_id,
                    "name": record.name,
                    "email": record.email,
                    "company_name": record.company_name,
                    "job_title": record.job_title,
                    "account_status": record.account_status,
                    "campaigns": "; ".join(record.campaigns),
                    "subject": record.generated_subject,
                    "body": record.generated_body,
                    "notion_written": record.notion_written,
                    "gmail_draft_id": record.gmail_draft_id,
                    "gmail_action": record.gmail_action,
                    "duplicate_of": record.duplicate_of,
                    "sender_name": record.sender_name,
                    "error": record.error,
                }
            )
    return path


def show_summary(records: list[ContactRecord], audit_path: Path) -> None:
    table = Table(title="Impact Hack Draft Batch")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Contact records", str(len(records)))
    table.add_row("Generated", str(sum(1 for r in records if r.generated_body)))
    table.add_row("Notion written", str(sum(1 for r in records if r.notion_written)))
    table.add_row("Gmail drafts", str(sum(1 for r in records if r.gmail_draft_id)))
    table.add_row("Missing email", str(sum(1 for r in records if not r.email or "@" not in r.email)))
    table.add_row("Duplicates skipped in Gmail", str(sum(1 for r in records if r.duplicate_of)))
    table.add_row("Errors", str(sum(1 for r in records if r.error)))
    table.add_row("Audit CSV", str(audit_path))
    console.print(table)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Impact Hack 2026 outreach drafts")
    parser.add_argument("--dry-run", action="store_true", help="Generate only, do not write to Notion or Gmail")
    parser.add_argument("--limit", type=int, default=0, help="Limit records for testing")
    parser.add_argument("--sender", default="", help="Optional override sender for every draft")
    parser.add_argument("--campaign", action="append", default=[], help="Campaign ID to process; repeatable")
    parser.add_argument("--campaigns", default="", help="Comma-separated campaign IDs to process")
    parser.add_argument("--generator", choices=["template", "openai"], default="template")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--no-gmail", action="store_true", help="Skip Gmail draft creation")
    parser.add_argument("--no-notion", action="store_true", help="Skip Notion field updates")
    parser.add_argument("--no-account-trigger", action="store_true", help="Skip updating account Trigger Event")
    parser.add_argument("--all-statuses", action="store_true", help="Include every account status in the target campaign")
    parser.add_argument(
        "--update-drafts-from-audit",
        default="",
        help="Existing audit CSV whose Gmail draft IDs should be updated in place",
    )
    args = parser.parse_args()

    if not NOTION_TOKEN or not NOTION_DB_CONTACTS_ID:
        raise RuntimeError("Missing Notion configuration")
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")

    requested_campaigns = set(args.campaign)
    if args.campaigns:
        requested_campaigns.update(c.strip() for c in args.campaigns.split(",") if c.strip())

    allowed_statuses = None if args.all_statuses else ALLOWED_ACCOUNT_STATUSES
    records = load_contacts(
        limit=args.limit,
        target_campaigns=requested_campaigns or None,
        allowed_account_statuses=allowed_statuses,
    )
    console.print(f"[cyan]Loaded {len(records)} eligible contact records[/cyan]")
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=30.0) if args.generator == "openai" else None

    generated_by_key: dict[str, ContactRecord] = {}
    for index, record in enumerate(records, 1):
        console.print(f"[bold]({index}/{len(records)}) {record.name or '(no name)'} @ {record.company_name}[/bold]")
        try:
            source = generated_by_key.get(record.duplicate_key)
            if source:
                record.generated_subject = source.generated_subject
                record.generated_body = source.generated_body
                record.duplicate_of = source.page_id
            else:
                if args.generator == "openai":
                    draft = generate_email(client, record, args.sender, args.model)
                else:
                    draft = generate_template_email(record, args.sender if args.sender else "")
                record.generated_subject = draft.subject
                record.generated_body = draft.body
                generated_by_key[record.duplicate_key] = record
                time.sleep(0.1)

            console.print(f"  [cyan]{record.generated_subject}[/cyan]")
            if not args.dry_run and not args.no_notion:
                if not args.no_account_trigger:
                    update_notion_account_trigger(record.account_id)
                record.notion_written = update_notion_contact(record)
        except Exception as exc:
            record.error = str(exc)
            console.print(f"  [red]{record.error}[/red]")

    if not args.dry_run and not args.no_gmail:
        service = _get_gmail_service()
        if not service:
            console.print("[red]Could not initialize Gmail service[/red]")
        else:
            existing_draft_ids = load_draft_ids_from_audit(args.update_drafts_from_audit)
            drafted_keys: set[str] = set()
            for record in records:
                if record.error or not record.generated_body:
                    continue
                if not record.email or "@" not in record.email:
                    continue
                if record.duplicate_key in drafted_keys:
                    continue
                try:
                    existing_id = existing_draft_ids.get(record.page_id, "")
                    if existing_id:
                        record.gmail_draft_id = update_gmail_draft(service, existing_id, record)
                        record.gmail_action = "updated"
                    else:
                        record.gmail_draft_id = create_gmail_draft(service, record)
                        record.gmail_action = "created"
                    drafted_keys.add(record.duplicate_key)
                    console.print(
                        f"  [green]Gmail draft {record.gmail_action} {record.gmail_draft_id[:8]} for {record.email}[/green]"
                    )
                except Exception as exc:
                    record.error = f"{record.error}; Gmail: {exc}" if record.error else f"Gmail: {exc}"
                    console.print(f"  [red]{record.error}[/red]")

    audit_path = write_audit(records, args.dry_run)
    show_summary(records, audit_path)
    console.print(json.dumps({"audit_csv": str(audit_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
