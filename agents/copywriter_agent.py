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
from utils.gmail_client import create_draft as gmail_create_draft
from utils.copywriting_guidance import load_humanized_guidance
from utils.campaign_tracker import load_campaign_guidance, sync_campaign_tracker

console = Console()

# Load the outreach skill prompt. The data/ path is a local override and is
# ignored by Git; prompts/ is the clonable default.
LEGACY_SKILL_PATH = Path(__file__).parent.parent / "data" / "prompts" / "outreach_skill.md"
TRACKED_SKILL_PATH = Path(__file__).parent.parent / "prompts" / "outreach_skill.md"
LEARNINGS_PATH = Path(__file__).parent.parent / "data" / "prompts" / "outreach_learnings.md"

DEFAULT_OUTREACH_SKILL_PROMPT = """
# TUM Social AI Outreach Copywriter

You write like a YC-level Head of Sales: direct, specific, commercially sharp,
and allergic to vague partnership fluff. Write concise cold LinkedIn messages
and follow-ups for TUM Social AI.

TUM Social AI is Germany's first AI-for-Good student initiative at TUM. We have
50+ AI engineers building real AI applications for nonprofits like the UN,
partnering with OpenAI, AWS, Knowunity, and more.

Rules:
- Use the RRR framework: Relevance, Reward, Request.
- Lead with the recipient's job-to-be-done, not a generic compliment.
- Talent personas care about hiring AI, software, data, and engineering talent.
- Partnerships, BD, marketing, and brand personas care about Munich/TUM AI
  ecosystem visibility, co-branded workshops, and campus reach.
- DevRel and technical personas care about real AI builders as users, testers,
  and feedback partners.
- Founder, CEO, and CTO personas care about talent density, product feedback,
  AI ecosystem presence, and credible AI-for-Good positioning.
- Namedrop only when it supports the recipient's benefit: TUM, the UN,
  Entreculturas, OpenAI, AWS, Knowunity, Lovable.
- For generic corporate campaigns, choose ONE partner area and write the ask
  around that area, e.g. hiring, visibility, product feedback, or API/cloud
  credits. Do not list all areas in one email.
- Never use internal labels like company list, Apollo, enrichment, top leads,
  upload-ready, or review CSV.
- Never use vague filler like synergies, collaboration potential, enhance,
  innovative, on your radar, or interesting.
- Keep LinkedIn messages under 75 words and follow-ups under 60 words.
- Use "relevant" in CTAs, never "interesting".
- Do not use em dashes.
""".strip()

# Career page paths to try when fetching job openings
CAREERS_PATHS = ["/careers", "/jobs", "/karriere", "/stellenangebote", "/join", "/open-positions", "/join-us"]

# Countries where German outreach is used (DACH)
DACH_COUNTRIES = {"germany", "austria", "switzerland", "deutschland", "österreich", "schweiz", "liechtenstein", "ch", "at", "de"}

# Campaign-specific system prompt overrides
# Injected AFTER the skill prompt when a matching campaign is active
CAMPAIGN_OVERRIDES: dict[str, str] = {
    "NGO_180526_InvoiceManagement": """

## CAMPAIGN OVERRIDE — NGO Invoice Management (NGO_180526_InvoiceManagement)

This campaign targets NGOs that distribute funds to LOCAL PARTNER ORGANIZATIONS in developing countries,
which means they receive many invoices from those local partners and must validate/approve them.

WE ARE OFFERING: TUM Social AI is building an AI invoice inspection & management tool together with
Entreculturas (a Jesuit NGO active in 50+ countries, $650M+ combined budget). We want to test and
improve this tool with more NGOs that have a similar operational model.

KEY MESSAGING RULES for this campaign:
- This is an EMAIL-ONLY outreach channel. Do NOT write LinkedIn messages — but still fill the
  linkedin_first_cold and linkedin_follow_up fields with a shortened version of the email body.
- Lead with: we're building an AI invoice inspection tool WITH Entreculturas and want to test it
  with other NGOs that receive invoices from local partner organizations.
- Frame it as: "we want to improve our AI models together with your organization" — collaborative,
  not a product pitch.
- Promise: once the model is production-ready, we will deploy it for them at ZERO COST because
  we are a pro-bono student initiative from TUM.
- Never say "we'll sell you" or imply a commercial relationship. It's purely pro-bono.
- Keep it SHORT: email body max 80 words. Subject max 8 words.
- CTA: "Would this be relevant for your team?" — always end with this relevance question.
- If the NGO has no individual contact (person_name is empty or generic), address the email to
  "Liebes [Org] Team" (German) or "Dear [Org] team" (English). Do NOT use "Hi there".
- Entreculturas reference: mention Entreculturas as the existing partner we're building this with.
  Frame it as proof of concept: "we're already piloting this with Entreculturas."
""",
}

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
        "\"Our 50+ AI engineers build real AI applications for nonprofits like the UN, "
        "partnering with OpenAI, AWS, and more.\"\n"
        "Keep it concise and tie the proof to one concrete partner area."
    ),
    "B": (
        "\n\n## A/B TEST VARIANT B — VALUE PROP FRAMING\n"
        "When describing TUM Social AI's work with partners, use this framing:\n"
        "\"Our 50+ AI engineers build real AI applications for nonprofits like the UN, "
        "partnering with OpenAI, AWS, and more.\"\n"
        "Then ask around one area, e.g. hiring, visibility, product feedback, or API/cloud credits."
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
        description="Cold email subject line. Max 7 words. Concrete value, not clickbait or internal trigger labels."
    )
    cold_email_body: str = Field(
        description="Cold email body. Max 90 words. RRR structure: relevance, reward, concrete request."
    )


BANNED_CUSTOMER_COPY_TERMS = [
    "company list",
    "list appearance",
    "top leads",
    "apollo",
    "enrichment",
    "upload-ready",
    "review csv",
    "joining forces",
    "enhance",
    "enhanc",
    "capabilities",
    "explore synergies",
    "synergy",
    "synergies",
    "collaboration potential",
    "potential collaboration",
    "aligns well",
    "alignment",
    "innovative",
    "on your radar",
    "resonate",
    "mutual engagement",
    "i tried connecting",
    "interesting",
    "at its core",
    "the real question",
    "stands as",
    "serves as",
    "boasts",
    "showcasing",
    "underscoring",
    "highlighting",
    "fostering",
    "pivotal",
    "crucial",
    "groundbreaking",
    "gamechanger",
]


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def validate_outreach(messages: OutreachMessages, include_linkedin: bool = False) -> list[str]:
    """Return human-readable quality issues for generated outreach copy."""
    fields = {
        "cold_email_subject": messages.cold_email_subject,
        "cold_email_body": messages.cold_email_body,
    }
    if include_linkedin:
        fields["linkedin_first_cold"] = messages.linkedin_first_cold
        fields["linkedin_follow_up"] = messages.linkedin_follow_up
    issues: list[str] = []

    for field, text in fields.items():
        lowered = (text or "").lower()
        if "—" in text or "–" in text or " -- " in text:
            issues.append(f"{field} contains an em/en dash or double-hyphen aside")
        for term in BANNED_CUSTOMER_COPY_TERMS:
            if term in lowered:
                issues.append(f"{field} contains banned phrase: {term}")
        if "not just" in lowered and " but " in lowered:
            issues.append(f"{field} uses formulaic 'not just X but Y' structure")

    subject = messages.cold_email_subject or ""
    subject_lower = subject.lower()
    if "||" in subject or "tum social ai:" in subject_lower:
        issues.append("cold_email_subject uses stale rigid subject format")
    if word_count(subject) > 7:
        issues.append("cold_email_subject is longer than 7 words")

    email_lower = (messages.cold_email_body or "").lower()
    if "short call" not in email_lower:
        issues.append("cold_email_body must ask for a short call")
    if "next monday" not in email_lower and "next tuesday" not in email_lower and "next week" not in email_lower:
        issues.append("cold_email_body must anchor the call to next Monday, Tuesday, or next week")
    if "relevant" not in email_lower:
        issues.append("cold_email_body CTA must use relevant")
    if word_count(messages.cold_email_body) > 100:
        issues.append("cold_email_body is longer than 100 words")

    return issues


def load_skill_prompt() -> str:
    """Load the outreach skill prompt from file."""
    for path in (LEGACY_SKILL_PATH, TRACKED_SKILL_PATH):
        if path.exists():
            return path.read_text(encoding="utf-8")
    console.print(
        f"[yellow]Skill prompt not found at {LEGACY_SKILL_PATH} or {TRACKED_SKILL_PATH}; "
        "using built-in fallback prompt.[/yellow]"
    )
    return DEFAULT_OUTREACH_SKILL_PROMPT


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


def persona_strategy(job_title: str) -> str:
    """Return concrete sales guidance for the contact's likely job-to-be-done."""
    title = (job_title or "").lower()
    if any(term in title for term in ("talent", "recruit", "hiring", "people", "human resource", "hr")):
        return (
            "Persona: Talent / Recruiting / People\n"
            "- Relevance: hiring AI, software, data, or engineering talent, ideally from current open roles if careers context is provided.\n"
            "- Reward: direct access to 50+ AI engineers at TUM, plus workshop/employer-branding access before they graduate.\n"
            "- Subject direction: 'TUM AI Talent x COMPANY'."
        )
    if any(term in title for term in ("developer relations", "devrel", "community", "field cto")):
        return (
            "Persona: Developer Relations / Technical Community\n"
            "- Relevance: getting real AI builders to use, test, and talk about their developer product or platform.\n"
            "- Reward: 50+ TUM AI engineers building real AI applications for nonprofits, credible power users, workshop audience, and feedback loop.\n"
            "- Subject direction: 'AI Builders x COMPANY' or 'TUM AI Builders x COMPANY'."
        )
    if any(term in title for term in ("partnership", "business development", "bd", "alliances", "site acquisition")):
        return (
            "Persona: Partnerships / Business Development\n"
            "- Relevance: Munich/TUM AI ecosystem access, co-branded events, campus visibility, and technical student network reach.\n"
            "- Reward: TUM Social AI has 50+ AI engineers building real AI applications for nonprofits like the UN, partnering with OpenAI, AWS, and more.\n"
            "- Subject direction: 'Munich AI Ecosystem x COMPANY'."
        )
    if any(term in title for term in ("marketing", "brand", "communications", "growth", "pr ")):
        return (
            "Persona: Marketing / Brand / Communications\n"
            "- Relevance: more visibility in Munich's technical university and AI ecosystem.\n"
            "- Reward: campus presence at TUM, co-branded workshops/events, and association with credible AI-for-Good work with nonprofits like the UN.\n"
            "- Subject direction: 'Munich AI Visibility x COMPANY'."
        )
    if any(term in title for term in ("cto", "technology", "engineering", "product")):
        return (
            "Persona: CTO / Product / Engineering\n"
            "- Relevance: access to strong AI builders as future hires, power users, or technical feedback partners.\n"
            "- Reward: 50+ TUM AI engineers shipping real AI applications for nonprofits, with OpenAI, AWS, and other technical partners in the ecosystem.\n"
            "- Subject direction: 'TUM AI Builders x COMPANY'."
        )
    if any(term in title for term in ("ceo", "founder", "chief", "c-suite")):
        return (
            "Persona: Founder / CEO / C-suite\n"
            "- Relevance: scaling company presence in the Munich AI ecosystem, hiring density, and credible AI-for-Good positioning.\n"
            "- Reward: TUM Social AI gives access to 50+ AI engineers, TUM campus visibility, and proof through partners like OpenAI, AWS, Knowunity, and nonprofits like the UN.\n"
            "- Subject direction: 'COMPANY x TUM Social AI'."
        )
    return (
        "Persona: General strategic partnerships\n"
        "- Relevance: choose the strongest concrete JTBD from company context: talent, ecosystem visibility, product feedback, or AI-for-Good credibility.\n"
        "- Reward: TUM campus access, 50+ AI engineers, and named proof from OpenAI, AWS, Knowunity, and nonprofits like the UN.\n"
        "- Subject direction: use the specific value, not a generic trigger."
    )


def persona_kind(job_title: str) -> str:
    """Classify the contact persona for deterministic RRR email copy."""
    title = (job_title or "").lower()
    if any(term in title for term in ("talent", "recruit", "hiring", "people", "human resource", "hr")):
        return "talent"
    if any(term in title for term in ("developer relations", "devrel", "community", "field cto")):
        return "devrel"
    if any(term in title for term in ("partnership", "business development", "bd", "alliances", "site acquisition")):
        return "partnerships"
    if any(term in title for term in ("marketing", "brand", "communications", "growth", "pr ")):
        return "marketing"
    if any(term in title for term in ("cto", "technology", "engineering", "product")):
        return "technical_exec"
    if any(term in title for term in ("ceo", "founder", "chief", "c-suite")):
        return "executive"
    return "general"


def subject_company_name(company_name: str) -> str:
    """Return a short readable company name for subjects."""
    name = re.sub(r"\s*\([^)]*\)", "", company_name or "").strip()
    name = re.split(r"\s+[–-]\s+", name)[0].strip()
    return name or (company_name or "your team")


def build_rrr_cold_email(contact: dict) -> tuple[str, str]:
    """
    Build a deterministic RRR cold email for corporate outreach.

    The model still writes LinkedIn copy, but email drafts are the highest-risk
    deliverable. This keeps them short, concrete, persona-relevant, and free of
    internal workflow language.
    """
    person_name = contact.get("person_name", "") or ""
    first_name = person_name.split()[0] if person_name else "there"
    company = contact.get("company_name", "") or "your team"
    company_short = subject_company_name(company)
    sender = contact.get("campaign_sender") or DEFAULT_CAMPAIGN_SENDER or ""
    trigger = (contact.get("trigger") or "").lower()
    title = contact.get("job_title", "")
    kind = persona_kind(title)
    careers_context = (contact.get("careers_context") or "").strip()
    proof_line = (
        "Our 50+ AI engineers build real AI applications for nonprofits like the UN, "
        "partnering with OpenAI, AWS, and more."
    )
    request_area = "a partnership"

    if kind == "talent":
        subject = f"TUM AI Talent x {company_short}"
        relevance = (
            f"Are you currently hiring AI, software, or engineering talent at {company_short}?"
            if not careers_context
                else f"I saw {company_short} is hiring technical roles. Is building an early pipeline of AI engineers currently relevant?"
        )
        reward = f"We're TUM Social AI, Germany's first AI-for-Good student initiative at TUM. {proof_line}"
        request_area = "hiring AI talent"
    elif kind == "devrel":
        subject = f"AI Builders x {company_short}"
        relevance = f"Are you looking for more AI builders to use and stress-test {company_short}'s developer platform?"
        reward = f"We're TUM Social AI, Germany's first AI-for-Good student initiative at TUM. {proof_line}"
        request_area = "product feedback from AI builders"
    elif kind in {"partnerships", "marketing"}:
        subject = f"Munich AI Ecosystem x {company_short}"
        relevance = f"Are you trying to build more visibility for {company_short} in the Munich/TUM AI ecosystem?"
        reward = f"We're TUM Social AI, Germany's first AI-for-Good student initiative at TUM. {proof_line}"
        request_area = "campus visibility"
    elif kind in {"technical_exec", "executive"}:
        subject = f"{company_short} x TUM Social AI"
        if "flare" in trigger or "sustainab" in trigger or "ecolog" in trigger:
            relevance = f"Are you trying to connect {company_short}'s sustainability story with AI talent in Europe?"
        else:
            relevance = f"Are you trying to build a stronger TUM/Munich AI talent and feedback loop for {company_short}?"
        reward = f"We're TUM Social AI, Germany's first AI-for-Good student initiative at TUM. {proof_line}"
        request_area = "hiring or visibility"
    else:
        subject = f"{company_short} x TUM Social AI"
        relevance = f"Are talent access or visibility in Munich's AI ecosystem relevant for {company_short} right now?"
        reward = f"We're TUM Social AI, Germany's first AI-for-Good student initiative at TUM. {proof_line}"
        request_area = "hiring or visibility"

    body = (
        f"Hi {first_name},\n\n"
        f"{relevance}\n\n"
        f"{reward}\n\n"
        f"If {request_area} is currently a priority, would a short call next Monday be relevant "
        "to explore how we could set up a partnership together?\n\n"
        f"{sender}"
    )
    return subject, body


def build_contact_prompt(contact: dict, campaign_id: str = "", campaign_guidance: str = "") -> str:
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

    lines.append("\n## PERSONA-SPECIFIC STRATEGY")
    lines.append(persona_strategy(job_title))

    if campaign_guidance:
        lines.append(campaign_guidance)

    lines.append("\n## LANGUAGE")
    contact_country_lower = (country or "").lower().strip()
    if campaign_id == "NGO_180526_InvoiceManagement" and contact_country_lower in DACH_COUNTRIES:
        lines.append("- Write ALL messages in GERMAN (Deutsch)")
        lines.append("- Use formal 'Sie' for NGO and institutional contacts")
        lines.append("- Sign off emails with 'Viele Grüße' followed by sender name")
    else:
        lines.append("- Write ALL messages in English")

    lines.append("\n## INSTRUCTIONS")
    lines.append("- Follow the OUTREACH SKILL rules exactly")
    lines.append("- Always preserve A/B testing: this contact has an assigned A/B variant. Use the active variant instructions and write the AB Variant property when saving.")
    lines.append("- Act like a YC-level Head of Sales: concise, commercially sharp, persona-relevant, and allergic to vague partnership language")
    lines.append("- Use the RRR framework: Relevance (persona JTBD or trigger), Reward (proof/namedrops tied to their benefit), Request (short call next week with a concrete day anchor)")
    lines.append("- Each message MUST reference something specific about this company or contact")
    lines.append("- Prioritize careers page openings over weak triggers like 'company list' or 'list appearance'")
    lines.append("- If the trigger says 'company list', 'list appearance', or similar internal wording, ignore that phrase and infer the strongest persona-based hook from job title, careers context, company description, and industry")
    lines.append("- LinkedIn 1st Cold: max 75 words")
    lines.append("- LinkedIn FU: max 60 words, SAME hook/topic as cold message. Only reference facts from the cold message or from the CONTACT/COMPANY context above. NEVER invent new projects, partnerships, awards, or achievements that aren't explicitly provided.")
    lines.append("- Cold Email Subject: max 7 words. Use a concrete value pattern like 'TUM AI Talent x COMPANY' or 'Munich AI Ecosystem x COMPANY'. NEVER use 'Company List', 'List Appearance', or 'Collaboration Potential'.")
    lines.append(f"- Cold Email Body: max 90 words, sign with ONLY '{sender_full_name}'. Do NOT include titles or links.")
    lines.append("- Cold Email CTA: ask for a short call next week with a concrete anchor, e.g. next Monday or Tuesday. Use 'relevant'; never use 'interesting'.")
    lines.append(f"- LinkedIn sign-off: 'Best, {sender_first_name}'")
    lines.append("- Be SPECIFIC about what this company does — never use generic terms like 'IT services' or 'technology sector'")
    lines.append("- Never write: 'joining forces', 'enhance your tech strategies', 'explore synergies', 'collaboration potential', 'aligns well' without a concrete reason, or 'I tried connecting over LinkedIn'")
    lines.append("- Use only concrete partner assets: talent pipeline, TUM campus visibility, Munich AI ecosystem, technical workshops, API/cloud-credit partnership, product feedback from AI builders, CSR/AI-for-Good credibility")

    return "\n".join(lines)


def generate_outreach(
    contact: dict,
    skill_prompt: str,
    client: OpenAI,
    variant: str = "",
    campaign_id: str = "",
    campaign_guidance: str = "",
) -> OutreachMessages:
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
    user_prompt = build_contact_prompt(contact, campaign_id=campaign_id, campaign_guidance=campaign_guidance)

    # Build full system prompt: skill + campaign override + variant addendum + learnings
    full_system_prompt = skill_prompt
    if campaign_id and campaign_id in CAMPAIGN_OVERRIDES:
        full_system_prompt += CAMPAIGN_OVERRIDES[campaign_id]
    if variant in VARIANT_ADDENDA:
        full_system_prompt += VARIANT_ADDENDA[variant]
    learnings = load_learnings_prompt()
    if learnings:
        full_system_prompt += learnings
    full_system_prompt += load_humanized_guidance("Strategic Partnerships")

    last_result = None
    quality_feedback = ""
    for attempt in range(1, 4):
        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": full_system_prompt},
                {"role": "user", "content": user_prompt + quality_feedback}
            ],
            response_format=OutreachMessages,
            max_tokens=2000,
        )

        result = response.choices[0].message.parsed
        if campaign_id != "NGO_180526_InvoiceManagement":
            subject, body = build_rrr_cold_email(contact)
            result = OutreachMessages(
                linkedin_first_cold=result.linkedin_first_cold,
                linkedin_follow_up=result.linkedin_follow_up,
                cold_email_subject=subject,
                cold_email_body=body,
            )
        last_result = result
        # Log every attempt, not just the last one — quality-gate retries
        # are real GPT-4o spend and must show up in cost reports.
        log_api_usage(
            "copywriter_agent", "generate_outreach", "gpt-4o",
            response.usage,
            {
                "contact": contact.get("person_name", ""),
                "company": contact.get("company_name", ""),
                "variant": variant,
                "sender": contact.get("campaign_sender", ""),
                "attempt": attempt,
            }
        )
        issues = validate_outreach(result, include_linkedin=True)
        if not issues:
            break
        quality_feedback = (
            "\n\n## QUALITY GATE FAILED\n"
            "Rewrite all four messages and fix these issues before returning final copy:\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + "\nUse concrete RRR copy only. No vague sales filler."
        )
        console.print(f"  [yellow]Quality retry {attempt}/3: {len(issues)} issue(s)[/yellow]")

    result = last_result
    if result is None:
        raise RuntimeError("OpenAI returned no outreach result")

    final_issues = validate_outreach(result, include_linkedin=True)
    if final_issues:
        raise ValueError("Generated copy failed quality gate: " + "; ".join(final_issues))

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
    console.print("[cyan]Humanizer + strategic Notion best-practice guidance enabled[/cyan]")

    # Check for learnings file
    if LEARNINGS_PATH.exists():
        console.print(f"[cyan]Learnings file found — will inject into prompts[/cyan]")

    campaign_guidance = load_campaign_guidance(campaign_id)
    if campaign_guidance:
        console.print(Panel(
            campaign_guidance,
            title="Campaign Tracker guidance",
            border_style="cyan",
        ))

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
        if not dry_run and campaign_id:
            sync_campaign_tracker(campaign_id=campaign_id)
        return

    console.print(f"[cyan]Found {len(contacts)} contacts needing messages[/cyan]")

    # Initialize OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=180.0, max_retries=4)

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
            messages = generate_outreach(
                contact,
                skill_prompt,
                client,
                variant=variant,
                campaign_id=campaign_id,
                campaign_guidance=campaign_guidance,
            )
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
                    # Also create a Gmail draft for team review. A Gmail failure
                    # must not mark the contact as errored: the Notion write
                    # already succeeded and the draft can be recreated later.
                    try:
                        gmail_create_draft(
                            to_email=contact.get("email", ""),
                            subject=messages.cold_email_subject,
                            body=messages.cold_email_body,
                            contact_name=person,
                            company_name=company,
                        )
                    except Exception as gmail_error:
                        console.print(f"  [yellow]Gmail draft skipped: {gmail_error}[/yellow]")
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

    if not dry_run:
        console.print("\n[cyan]Syncing Campaign Tracker after copywriter run...[/cyan]")
        sync_campaign_tracker(campaign_id=campaign_id)


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
