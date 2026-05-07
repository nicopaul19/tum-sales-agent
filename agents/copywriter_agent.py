"""
Copywriter Agent — Generates personalized outreach messages for Notion contacts.

For each contact (linked to an account), generates 4 messages using GPT-4o:
1. LinkedIn 1st Cold — initial connection message
2. LinkedIn FU message — follow-up if no reply
3. Cold Email Body — formal email body
4. Cold Email Subject — email subject line

Messages are written directly to the contact's Notion page.

Usage:
    python -m agents.copywriter_agent                           # all contacts missing messages
    python -m agents.copywriter_agent --campaign Workflow_0902  # only contacts from this campaign
    python -m agents.copywriter_agent --dry-run                 # preview without writing to Notion
    python -m agents.copywriter_agent --force                    # regenerate ALL messages (overwrite existing)
"""
import re
import sys
import random
import argparse
from datetime import datetime
from pathlib import Path

import requests
from openai import OpenAI
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import OPENAI_API_KEY, NOTION_DB_CONTACTS_ID, NOTION_DB_ACCOUNTS_ID, DEFAULT_CAMPAIGN_SENDER
from utils.api_logger import log_api_usage
from utils.notion_client import (
    ensure_contact_outreach_properties,
    get_contacts_for_copywriting,
    get_accounts_for_copywriting,
    update_contact_outreach,
    update_account_outreach,
)
from utils.preflight import run_preflight

console = Console()

# Load the outreach skill prompt
SKILL_PATH = Path(__file__).parent.parent / "data" / "prompts" / "outreach_skill.md"
LEARNINGS_PATH = Path(__file__).parent.parent / "data" / "prompts" / "outreach_learnings.md"

# Career page paths to try when fetching job openings
CAREERS_PATHS = ["/careers", "/jobs", "/karriere", "/stellenangebote", "/join", "/open-positions", "/join-us"]

# Job titles that trigger careers page fetching (checked via substring match)
CAREERS_FETCH_TITLES = {
    "talent", "recruit", "hr", "human resource", "people", "hiring",
    "founder", "ceo", "cto", "coo", "cmo", "chief",
    "marketing", "brand", "communications", "pr ",
}


def resolve_campaign_sender(sender: str = "", interactive: bool = True) -> str:
    """Resolve the human sender for this campaign."""
    sender = (sender or "").strip() or (DEFAULT_CAMPAIGN_SENDER or "").strip()
    if sender:
        return sender
    if interactive:
        sender = console.input("[bold]Who will execute this campaign? Full sender name: [/bold]").strip()
        if sender:
            return sender
    raise ValueError("Campaign sender is required. Pass --sender \"Full Name\" or set DEFAULT_CAMPAIGN_SENDER in .env.")


def _sender_first_name(sender: str) -> str:
    return (sender or "").strip().split()[0] if (sender or "").strip() else "there"

# A/B Test variant addenda — injected into the system prompt
VARIANT_ADDENDA = {
    "A": (
        "\n\n## A/B TEST VARIANT A — VALUE PROP FRAMING\n"
        "When describing TUM Social AI's work with partners, use this framing:\n"
        "\"We're developing AI solutions with partners like UN Women\"\n"
        "Keep it concise and understated. Let the partner name do the heavy lifting."
    ),
    "B": (
        "\n\n## A/B TEST VARIANT B — VALUE PROP FRAMING\n"
        "When describing TUM Social AI's work with partners, use this framing:\n"
        "\"We're multiplying the impact of our non-profit partners like UN Women in over 50 countries through custom AI tools\"\n"
        "Emphasize the scale and tangible impact. Make the reader feel the reach."
    ),
}


class OutreachMessages(BaseModel):
    """Structured output for the 4 outreach messages."""
    linkedin_first_cold: str = Field(
        description="Cold LinkedIn connection message. Max 75 words. Personalized with trigger."
    )
    linkedin_follow_up: str = Field(
        description="Follow-up LinkedIn message if no reply after 3-5 days. Max 60 words. MUST reference the same hook/topic from the cold message. Only use facts ALREADY mentioned in the cold message or provided in the contact context. NEVER invent new projects, partnerships, awards, or achievements."
    )
    cold_email_subject: str = Field(
        description="Cold email subject line. Max 8 words. Lowercase, descriptive, not clickbait."
    )
    cold_email_body: str = Field(
        description="Cold email body. Max 100 words. Mobile-optimized short paragraphs."
    )


def load_skill_prompt() -> str:
    """Load the outreach skill prompt from file."""
    if SKILL_PATH.exists():
        return SKILL_PATH.read_text(encoding="utf-8")
    else:
        console.print(f"[red]Error: Skill prompt not found at {SKILL_PATH}[/red]")
        return ""


def assign_variant() -> str:
    """Randomly assign A/B test variant (50/50 split)."""
    return random.choice(["A", "B"])


def load_learnings_prompt() -> str:
    """Load outreach learnings from the feedback agent, if available."""
    if LEARNINGS_PATH.exists():
        content = LEARNINGS_PATH.read_text(encoding="utf-8").strip()
        if content:
            return f"\n\n## LEARNINGS FROM PAST OUTREACH (apply these)\n{content}"
    return ""


def fetch_careers_context(website: str) -> str:
    """
    Fetch careers/jobs page content from company website for hook context.

    Tries common career page paths and returns stripped text if found.
    Best-effort: silently returns empty string on any failure.
    """
    if not website:
        return ""

    domain = website.rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"

    for path in CAREERS_PATHS:
        try:
            resp = requests.get(
                f"{domain}{path}", timeout=5, allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; TUMSocialAI/1.0)"}
            )
            if resp.status_code >= 400:
                continue
            content = resp.content[:10000].decode("utf-8", errors="ignore")
            # Strip scripts, styles, then all HTML tags
            text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            if text and len(text) > 100:
                return text[:2000]
        except Exception:
            continue

    return ""


def build_contact_prompt(contact: dict) -> str:
    """
    Build the user prompt for GPT-4o with all available contact + account context.

    Args:
        contact: Dict with contact and account fields from Notion.

    Returns:
        Formatted prompt string.
    """
    person_name = contact.get("person_name", "Unknown")
    first_name = person_name.split()[0] if person_name and person_name != "Unknown" else "there"
    job_title = contact.get("job_title", "")
    email = contact.get("email", "")
    sender_full_name = contact.get("campaign_sender") or DEFAULT_CAMPAIGN_SENDER or ""
    sender_first_name = _sender_first_name(sender_full_name)

    company_name = contact.get("company_name", "Unknown")
    industry = contact.get("industry", "")
    mission = contact.get("mission", "")
    trigger = contact.get("trigger", "")
    account_type = contact.get("account_type", "")
    city = contact.get("city", "")
    country = contact.get("country", "")
    website = contact.get("website", "")
    company_description = contact.get("company_description", "")
    latest_funding = contact.get("latest_funding", "")
    employees = contact.get("employees")

    lines = [
        "Generate 4 outreach messages for this contact.\n",
        "## SENDER",
        f"- Full name: {sender_full_name}",
        f"- First name for LinkedIn sign-off: {sender_first_name}",
        "",
        "## CONTACT",
        f"- Name: {person_name}",
        f"- First name (use in greeting): {first_name}",
    ]
    if job_title:
        lines.append(f"- Job title: {job_title}")
    if email:
        lines.append(f"- Email: {email}")

    lines.append("\n## COMPANY")
    lines.append(f"- Company: {company_name}")
    if account_type:
        lines.append(f"- Type: {account_type}")
    if industry:
        lines.append(f"- Industry: {industry}")
    if website:
        lines.append(f"- Website: {website}")
    if city or country:
        location = ", ".join(filter(None, [city, country]))
        lines.append(f"- Location: {location}")
    if employees:
        lines.append(f"- Employees: ~{int(employees)}")
    if latest_funding:
        lines.append(f"- Latest funding: {latest_funding}")
    if mission:
        lines.append(f"- Mission/Description: {mission}")
    if company_description:
        lines.append(f"- Company description: {company_description}")

    lines.append("\n## TRIGGER (why we are reaching out)")
    if trigger:
        lines.append(f"- {trigger}")
    else:
        lines.append("- No specific trigger available. Use company context and industry to craft a relevant hook.")

    # Inject careers page context if available (fetched for relevant personas)
    careers_context = contact.get("careers_context")
    if careers_context:
        lines.append("\n## CAREERS PAGE CONTEXT (from their website)")
        lines.append(f"- {careers_context[:1500]}")
        lines.append("- Use this to craft a specific, relevant hook if their job openings match our talent pool")

    lines.append("\n## LANGUAGE")
    lines.append("- Write ALL messages in English")

    lines.append("\n## INSTRUCTIONS")
    lines.append("- Follow the OUTREACH SKILL rules exactly")
    lines.append("- Each message MUST reference something specific about this company or contact")
    lines.append("- LinkedIn 1st Cold: max 75 words")
    lines.append("- LinkedIn FU: max 60 words, SAME hook/topic as cold message. Only reference facts from the cold message or from the CONTACT/COMPANY context above. NEVER invent new projects, partnerships, awards, or achievements that aren't explicitly provided.")
    lines.append("- Cold Email Subject: MUST exactly follow this format: 'COMPANY_NAME || TUM Social AI: TRIGGER_TOPIC'")
    lines.append(f"- Cold Email Body: max 100 words, sign with ONLY '{sender_full_name}'. Do NOT include titles or links.")
    lines.append(f"- LinkedIn sign-off: 'Best, {sender_first_name}'")
    lines.append("- Be SPECIFIC about what this company does — never use generic terms like 'IT services' or 'technology sector'")

    return "\n".join(lines)


def generate_outreach(contact: dict, skill_prompt: str, client: OpenAI, variant: str = "") -> OutreachMessages:
    """
    Call GPT-4o to generate 4 outreach messages for a contact.

    Args:
        contact: Dict with contact + account context.
        skill_prompt: The outreach skill system prompt.
        client: OpenAI client instance.
        variant: A/B test variant ("A" or "B") to inject into prompt.

    Returns:
        OutreachMessages with the 4 generated messages.
    """
    user_prompt = build_contact_prompt(contact)

    # Build full system prompt: skill + variant addendum + learnings
    full_system_prompt = skill_prompt
    if variant in VARIANT_ADDENDA:
        full_system_prompt += VARIANT_ADDENDA[variant]
    learnings = load_learnings_prompt()
    if learnings:
        full_system_prompt += learnings

    response = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        response_format=OutreachMessages,
        max_tokens=2000,
    )

    result = response.choices[0].message.parsed

    log_api_usage(
        "copywriter_agent", "generate_outreach", "gpt-4o",
        response.usage,
        {
            "contact": contact.get("person_name", ""),
            "company": contact.get("company_name", ""),
            "variant": variant,
            "sender": contact.get("campaign_sender", ""),
        }
    )

    return result


def run_copywriter(
    campaign_id: str = "",
    dry_run: bool = False,
    force: bool = False,
    sender: str = "",
    interactive: bool = True,
):
    """
    Generate outreach messages for contacts missing them.

    Args:
        campaign_id: Only process contacts from this campaign (empty = all).
        dry_run: If True, generate messages but don't write to Notion.
        force: If True, regenerate messages even for contacts that already have them.
    """
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Sales Agent - Copywriter Agent[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    if dry_run:
        console.print("[yellow]DRY RUN — messages will NOT be written to Notion[/yellow]")
    if force:
        console.print("[yellow]FORCE MODE — overwriting existing messages[/yellow]")
    console.print("=" * 60)

    # Validate
    if not OPENAI_API_KEY:
        console.print("[red]Error: OPENAI_API_KEY not configured[/red]")
        return
    if not NOTION_DB_CONTACTS_ID and not NOTION_DB_ACCOUNTS_ID:
        console.print("[red]Error: NOTION_DB_CONTACTS_ID or NOTION_DB_ACCOUNTS_ID not configured[/red]")
        return
    try:
        campaign_sender = resolve_campaign_sender(sender, interactive=interactive)
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        return
    console.print(f"[cyan]Campaign sender: {campaign_sender}[/cyan]")

    # Load skill prompt
    skill_prompt = load_skill_prompt()
    if not skill_prompt:
        return

    # Check for learnings file
    if LEARNINGS_PATH.exists():
        console.print(f"[cyan]Learnings file found — will inject into prompts[/cyan]")

    contacts_available = True
    prop_map = None

    # Preflight validation
    accounts_db_id = NOTION_DB_ACCOUNTS_ID or ""
    if accounts_db_id and NOTION_DB_CONTACTS_ID:
        console.print("\n[cyan]Ensuring outreach properties on Contacts DB...[/cyan]")
        contacts_available = ensure_contact_outreach_properties(NOTION_DB_CONTACTS_ID)
        console.print("\n[cyan]Running preflight validation...[/cyan]")
        preflight = run_preflight(accounts_db_id, NOTION_DB_CONTACTS_ID)
        if not preflight.success:
            contact_only_errors = preflight.errors and all(
                "Contacts" in err or "Contacts DB" in err or "Failed to fetch Contacts" in err
                for err in preflight.errors
            )
            if contact_only_errors:
                contacts_available = False
                console.print("[yellow]Contacts DB unavailable — falling back to Accounts DB outreach fields.[/yellow]")
            else:
                console.print("\n[bold red]Preflight found errors — properties listed above are missing from your Notion databases.[/bold red]")
                console.print("[yellow]Proceeding may cause API errors or missing data for those fields.[/yellow]")
                answer = "no"
                if interactive:
                    answer = console.input("\n[bold]Continue anyway? (y/N): [/bold]").strip().lower()
                if answer not in ("y", "yes"):
                    console.print("[red]Aborted.[/red]")
                    return
                console.print("[yellow]Continuing with available mappings...[/yellow]\n")
        prop_map = preflight.prop_map

    # Fetch contacts needing messages
    filter_label = f"campaign={campaign_id}" if campaign_id else "all campaigns"
    mode_label = "all contacts" if force else "contacts without outreach messages"
    console.print(f"\n[cyan]Fetching {mode_label} ({filter_label})...[/cyan]")
    if contacts_available and NOTION_DB_CONTACTS_ID:
        contacts = get_contacts_for_copywriting(NOTION_DB_CONTACTS_ID, campaign_id, force=force, prop_map=prop_map)
    else:
        contacts = get_accounts_for_copywriting(NOTION_DB_ACCOUNTS_ID, campaign_id, force=force, prop_map=prop_map)

    if not contacts:
        console.print("[green]All contacts already have outreach messages. Nothing to do.[/green]")
        return

    console.print(f"[cyan]Found {len(contacts)} contacts needing messages[/cyan]")

    # Initialize OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Process each contact
    generated = 0
    written = 0
    errors = 0

    variant_counts = {"A": 0, "B": 0}

    for i, contact in enumerate(contacts, 1):
        person = contact.get("person_name", "?")
        company = contact.get("company_name", "?")
        contact["campaign_sender"] = contact.get("campaign_sender") or campaign_sender
        variant = assign_variant()
        variant_counts[variant] += 1
        console.print(f"\n[bold]({i}/{len(contacts)}) {person} @ {company} [dim]variant {variant}[/dim][/bold]")

        # Fetch careers page for relevant personas (talent, HR, founder, marketing, etc.)
        job_title_lower = contact.get("job_title", "").lower()
        should_fetch_careers = any(kw in job_title_lower for kw in CAREERS_FETCH_TITLES)
        if should_fetch_careers and contact.get("website"):
            console.print(f"  [dim]Fetching careers page for {company}...[/dim]")
            careers_text = fetch_careers_context(contact["website"])
            if careers_text:
                contact["careers_context"] = careers_text
                console.print(f"  [dim]Found careers content ({len(careers_text)} chars)[/dim]")
            else:
                console.print(f"  [dim]No careers page found[/dim]")

        try:
            messages = generate_outreach(contact, skill_prompt, client, variant=variant)
            generated += 1

            # Preview
            console.print(Panel(
                f"[cyan]LinkedIn 1st Cold:[/cyan]\n{messages.linkedin_first_cold}\n\n"
                f"[cyan]LinkedIn FU:[/cyan]\n{messages.linkedin_follow_up}\n\n"
                f"[cyan]Email Subject:[/cyan] {messages.cold_email_subject}\n\n"
                f"[cyan]Email Body:[/cyan]\n{messages.cold_email_body}",
                title=f"{person} @ {company} (Variant {variant})",
                border_style="dim"
            ))

            if not dry_run:
                if contact.get("contact_storage") == "account":
                    ok = update_account_outreach(
                        account_page_id=contact["account_page_id"],
                        linkedin_first=messages.linkedin_first_cold,
                        linkedin_fu=messages.linkedin_follow_up,
                        email_body=messages.cold_email_body,
                        email_subject=messages.cold_email_subject,
                        ab_variant=variant,
                        prop_map=prop_map,
                    )
                else:
                    ok = update_contact_outreach(
                        contact_page_id=contact["contact_page_id"],
                        linkedin_first=messages.linkedin_first_cold,
                        linkedin_fu=messages.linkedin_follow_up,
                        email_body=messages.cold_email_body,
                        email_subject=messages.cold_email_subject,
                        ab_variant=variant,
                        prop_map=prop_map,
                    )
                if ok:
                    written += 1
                    console.print(f"  [green]Written to Notion[/green]")
                else:
                    errors += 1
            else:
                console.print(f"  [yellow]Dry run — skipped writing[/yellow]")

        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")
            errors += 1

    # Summary
    console.print("\n" + "=" * 40)
    summary = Table(title="Copywriter Summary")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="green")
    summary.add_row("Contacts processed", str(len(contacts)))
    summary.add_row("Messages generated", str(generated))
    summary.add_row("Written to Notion", str(written))
    summary.add_row("Errors", str(errors))
    summary.add_row("Variant A / B", f"{variant_counts['A']} / {variant_counts['B']}")
    if campaign_id:
        summary.add_row("Campaign filter", campaign_id)
    summary.add_row("Campaign sender", campaign_sender)
    console.print(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate outreach messages for Notion contacts")
    parser.add_argument("--campaign", default="", help="Only process contacts from this campaign (e.g. Workflow_0902)")
    parser.add_argument("--dry-run", action="store_true", help="Preview messages without writing to Notion")
    parser.add_argument("--force", action="store_true", help="Regenerate ALL messages (overwrite existing)")
    parser.add_argument("--sender", default="", help="Full name of the teammate executing this campaign")
    parser.add_argument("--no-input", action="store_true", help="Fail instead of prompting for missing sender/confirmations")
    args = parser.parse_args()

    run_copywriter(
        campaign_id=args.campaign,
        dry_run=args.dry_run,
        force=args.force,
        sender=args.sender,
        interactive=not args.no_input,
    )
