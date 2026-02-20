"""
Ranking Agent - Scores leads and outputs weekly qualified leads.

Logic:
1. Read pending leads from master_input.csv
2. Score each lead using GPT-4o (0-10)
3. All leads with score >= 5 are qualified
4. Save qualified leads to weekly_qualified_leads.csv (for Apollo import)
5. Save rest to backlog.csv
6. Update statuses in master_input.csv

Usage:
    python -m agents.ranking_agent
"""
import smtplib
import sys
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Tuple, Dict, Optional

import pandas as pd
from openai import OpenAI
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from pydantic import BaseModel, Field

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import (
    OPENAI_API_KEY,
    MASTER_CSV,
    QUALIFIED_CSV,
    QUALIFIED_NO_CONTACT_CSV,
    BACKLOG_CSV,
    EXPORTED_ARCHIVE_CSV,
    REQUALIFIED_BLOCKED_CSV,
    TABLES_DIR,
    COMPANY_BLOCKLIST,
    MASTER_CSV_HEADERS,
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    REPORT_RECIPIENT_EMAIL,
    RANKING_REPORT_RECIPIENTS
)
from utils.api_logger import log_api_usage
from utils.notion_client import (
    get_existing_accounts_from_notion,
    get_existing_contacts_from_notion,
    get_existing_contact_emails_from_notion,
    is_status_engaged_or_above,
    update_trigger_in_notion,
    reset_account_status_if_stale,
    create_contact_in_notion,
    get_pipeline_success_companies
)
# Note: Notion upload happens after Apollo enrichment, not in ranking

console = Console()

# ── Student Club Blocklist ──────────────────────────────────────────────────
# Known student clubs, university associations, and student-run organizations.
# Leads matching these are auto-disqualified (score=0) WITHOUT calling GPT-4o.
STUDENT_CLUB_BLOCKLIST = [
    "enactus",          # Enactus (any chapter: Germany, Berlin, Straubing, etc.)
    "thinc!",           # THINC! student club
    "thinc",            # THINC without punctuation
    "start munich",     # START Munich student club
    "start global",     # START Global student club
    "bonding",          # bonding student organization
    "vwi",              # VWI (German Association for Engineering Management, student variant)
    "cdtm",             # Center for Digital Technology and Management (student program)
    "unternehmertum",   # UnternehmerTUM (student/university entrepreneurship center)
    "aiesec",           # AIESEC student organization
    "aisec",            # AIESEC common misspelling
    "180 degrees",      # 180 Degrees Consulting (student consulting)
    "oikos",            # oikos student organization
    "sneep",            # sneep student network
    "market team",      # MTP / Market Team (student marketing org)
    "mtp ",             # MTP prefix
    "junge unternehmer", # Young entrepreneurs student club
    "studentische unternehmensberatung",  # student consulting clubs
    "junior enterprise", # Junior Enterprise student clubs
    "jade ",            # JADE network (Junior Association for Development in Europe)
]

# Patterns that indicate student clubs/associations (case-insensitive)
STUDENT_CLUB_PATTERNS = [
    "hochschulgruppe",       # University group (German)
    "studenteninitiative",   # Student initiative (German)
    "studierendeninitiative", # Student initiative (German, modern)
    "student association",
    "student club",
    "student organization",
    "student organisation",
    "student initiative",
    "student group",
    "student society",
    "student network",
    "studentische initiative",
    "studentische vereinigung",
    "universitätsverein",
    "fachschaft",            # Student council / department group
]


def is_student_club(company_name: str) -> bool:
    """Check if a company is a known student club or university association.

    Uses three strategies:
    1. Exact/partial match against STUDENT_CLUB_BLOCKLIST
    2. Pattern matching for student-related keywords
    3. Pattern matching for 'e.V.' suffix (German registered association indicator)
       combined with student/university keywords
    """
    if not company_name:
        return False
    name_lower = company_name.lower().strip()

    # Strategy 1: Check against blocklist (partial match)
    for blocked in STUDENT_CLUB_BLOCKLIST:
        if blocked in name_lower or name_lower.startswith(blocked):
            return True

    # Strategy 2: Check for student club patterns
    for pattern in STUDENT_CLUB_PATTERNS:
        if pattern in name_lower:
            return True

    # Strategy 3: 'e.V.' + university/student context signals a student association
    if "e.v." in name_lower or name_lower.endswith("e.v") or " ev " in name_lower:
        student_ev_signals = [
            "student", "studier", "uni ", "universit", "hochschul",
            "campus", "alumni", "akadem", "jung", "young",
            "initiative", "verein", "netzwerk", "network",
        ]
        for signal in student_ev_signals:
            if signal in name_lower:
                return True

    # Strategy 4: "Hochschulgruppe" is always a student club regardless of e.V.
    if "hochschulgruppe" in name_lower:
        return True

    return False


def _needs_student_club_verification(company_name: str) -> bool:
    """Check if a company name has patterns that warrant a second-pass student club check.

    This catches borderline cases that slip through the blocklist but have
    suspicious signals (e.g., 'e.V.', 'Student', 'Initiative', 'Alumni').
    """
    if not company_name:
        return False
    name_lower = company_name.lower().strip()
    suspicious_patterns = [
        "e.v.", " ev ", "e.v",
        "student", "studier",
        "hochschul", "universit",
        "initiative", "verein",
        "alumni", "association",
        "jugend", "young", "jung",
        "campus", "akadem",
        "club", "society",
        "netzwerk", "network",
        "gruppe", "group",
    ]
    return any(p in name_lower for p in suspicious_patterns)


def _verify_student_club_with_llm(company_name: str) -> bool:
    """Use GPT-4o-mini to verify if a company is a student club.

    Called as a cheap second-pass check when the company scored >= 5
    but has suspicious name patterns.

    Returns True if the LLM confirms it's a student club.
    """
    if not OPENAI_API_KEY:
        return False

    client = OpenAI(api_key=OPENAI_API_KEY)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a classification assistant. Answer only YES or NO."},
                {"role": "user", "content": (
                    f"Is '{company_name}' a student club, university association, student-run organization, "
                    f"student initiative, or university entrepreneurship program? "
                    f"Examples of what counts as YES: Enactus, AIESEC, bonding, thinc!, START Munich, CDTM, "
                    f"UnternehmerTUM, any Hochschulgruppe, any Studenteninitiative, any student consulting club. "
                    f"Answer only YES or NO."
                )}
            ],
            max_tokens=5,
            temperature=0
        )

        answer = response.choices[0].message.content.strip().upper()
        log_api_usage("ranking_agent", "student_club_verification", "gpt-4o-mini", response.usage, {"company": company_name})
        return answer.startswith("YES")

    except Exception as e:
        console.print(f"[yellow]Student club verification failed for {company_name}: {e}[/yellow]")
        return False


class LeadScore(BaseModel):
    """Structured output from GPT-4o scoring."""
    score: float = Field(ge=0, le=10, description="Score from 0-10")
    reasoning: str = Field(description="Brief reasoning for the score")
    red_flags: List[str] = Field(default_factory=list, description="Any disqualifying factors")


# Scoring criteria prompt
SCORING_PROMPT = """You are the Gatekeeper for TUM Social AI, Germany's leading student AI initiative based in Munich.
We connect ambitious AI students with organizations to build solutions and provide talent.

Your task: Score this lead (0-10) for potential partnership, sponsorship, or talent collaboration.

╔══════════════════════════════════════════════════════════════╗
║  CRITICAL DISQUALIFICATION — MUST CHECK FIRST (Score = 0)   ║
╠══════════════════════════════════════════════════════════════╣
║  WE ARE A STUDENT CLUB. Other student clubs, university     ║
║  associations, and student-run organizations are PEERS,     ║
║  NOT potential leads. They MUST receive score 0.            ║
║                                                              ║
║  Examples that MUST be score 0:                              ║
║  - Enactus (any chapter: Germany, Berlin, Straubing, etc.)  ║
║  - thinc! / THINC!                                          ║
║  - bonding                                                   ║
║  - AIESEC                                                    ║
║  - 180 Degrees Consulting                                    ║
║  - Any "e.V." that is student/university-affiliated         ║
║  - Any "Hochschulgruppe" or "Studenteninitiative"           ║
║  - Any organization described as "student club",             ║
║    "student association", "student initiative",              ║
║    "university group", or similar                            ║
║                                                              ║
║  CONTEXT: We are part of the Munich student ecosystem       ║
║  alongside START Munich, CDTM, UnternehmerTUM, etc.         ║
║  These are our peers, NOT leads. Score them 0.              ║
╚══════════════════════════════════════════════════════════════╝

=== IDEAL CUSTOMER PROFILE (ICP) ===

We provide value to organizations that:
1. Have a mission creating POSITIVE IMPACT (social, humanitarian, ecological, or innovation) — this is the MOST important factor
2. Work with AI or need AI talent
3. Engage with STUDENT ORGANIZATIONS in Germany (strongest conversion signal from our pipeline data)
4. DACH presence is a nice-to-have but NOT a hard requirement — many great partners operate internationally. Do NOT penalize companies just for being global or non-DACH. Only use DACH presence as a minor tiebreaker, never as a primary scoring factor.

{pipeline_success_section}

=== SCORING CRITERIA ===

HIGH SCORE (8-10):
- Companies that are SIMILAR to our proven pipeline successes listed above — same type, same engagement with students, same focus areas. This is the STRONGEST signal.
- AI companies and tech scale-ups (Series A+) that engage with student ecosystems
- Social impact organizations using or needing AI
- Large corporates with AI initiatives and CSR programs
- Innovation hubs, accelerators, and tech ecosystems
- NGOs, foundations, humanitarian organizations
- Companies that collaborate with student organizations — this is a STRONG positive signal for partnership potential (but the student orgs themselves are NOT leads)
- VCs/investors ONLY if they have a strong focus on SOCIAL IMPACT, CLIMATE TECH, or SUSTAINABILITY startups (e.g., impact funds, climate VCs). These are a natural fit for our AI-for-Good mission.

MEDIUM SCORE (5-7):
- Tech companies without clear AI focus but innovation-driven
- Consulting firms with tech/AI practice
- Research institutions and universities
- Government digital/innovation agencies
- Series A startups with clear AI application
- Early-stage AI startups participating in accelerators, pitch events, or hackathons (shows innovation drive)
- General VCs and investors focused on AI/tech but WITHOUT a clear social impact or climate focus — we haven't produced startups yet, so generic VCs have limited sponsorship incentive for now

LOW SCORE (1-4):
- Companies with weak AI/impact connection
- Traditional industries without innovation angle
- B2C consumer companies without tech focus
- Pure financial VCs (fintech-only, crypto, pure SaaS) with no social/climate angle

=== POSITIVE SIGNALS (Boost Score) ===

Look for these indicators that suggest good partnership fit:
- Participation in student-organized events (pitch competitions, hackathons, demo days)
- Collaboration with student organizations (shows openness to the student ecosystem)
- Hiring interns or junior talent (shows openness to student collaboration)
- Presence at Munich/DACH tech events
- Y Combinator, Techstars, or other accelerator participation
- Active in the Munich startup/tech ecosystem

=== AUTOMATIC DISQUALIFICATION (Score 0) ===

RED FLAGS - Immediately filter out:
- STUDENT CLUBS AND UNIVERSITY ASSOCIATIONS (we ARE a student club — see CRITICAL DISQUALIFICATION above)
- Very early stage startups (Pre-seed, Seed stage)
- Companies with NO connection to AI, social impact, OR ecological impact
- Financial institutions (banks, traditional finance)
- Cryptocurrency/Blockchain (unless clear social good application)
- Gambling/Betting companies
- Tobacco/Alcohol companies
- Weapons manufacturers and defense companies (e.g., Helsing, Rheinmetall, KNDS, Hensoldt) — doesn't align with our social impact values
- Military/defense contractors and dual-use weapons technology firms
- Catering, event services, videography (support services, not partners)

=== LEAD INFORMATION ===

Company: {company_name}
Domain: {company_domain}
Contact: {person_name}
Role: {person_role}
Trigger: {trigger}
Context: {context}
Entity Type: {entity_type}
Source: {source}

=== YOUR RESPONSE ===

Provide:
1. Score (0-10) based on ICP fit
2. Brief reasoning (mention impact angle, AI connection, student engagement signals, and similarity to pipeline successes if applicable)
3. Any red flags that caused point deductions or disqualification

Note: Personalized outreach messages will be crafted after Apollo enrichment."""


CAMPAIGN_COOLDOWN_DAYS = 90  # 3 months cooldown between campaigns targeting the same lead


def _get_most_recent_campaign_date(campaign_ids: List[str]) -> Optional[datetime]:
    """Get the most recent Workflow campaign date from campaign IDs.

    Campaign IDs follow the format 'Workflow_DDMM' where DD is day and MM is month.
    Returns the most recent campaign datetime, or None if no valid campaigns found.
    """
    now = datetime.now()
    most_recent = None
    for cid in campaign_ids:
        if not cid.startswith("Workflow_"):
            continue
        date_part = cid.replace("Workflow_", "")
        if len(date_part) != 4:
            continue
        try:
            day = int(date_part[:2])
            month = int(date_part[2:])
            year = now.year
            campaign_date = datetime(year, month, day)
            if campaign_date > now:
                campaign_date = datetime(year - 1, month, day)
            if most_recent is None or campaign_date > most_recent:
                most_recent = campaign_date
        except (ValueError, OverflowError):
            continue
    return most_recent


def _has_active_campaign(campaign_ids: List[str]) -> bool:
    """Check if any Workflow_DDMM campaign started less than 3 months ago.

    Returns True if there's a recent campaign within CAMPAIGN_COOLDOWN_DAYS,
    meaning the lead should NOT be re-qualified yet.
    """
    most_recent = _get_most_recent_campaign_date(campaign_ids)
    if most_recent is None:
        return False
    days_since = (datetime.now() - most_recent).days
    return days_since < CAMPAIGN_COOLDOWN_DAYS


def is_blocklisted(company_name: str) -> bool:
    """Check if a company is in the blocklist."""
    if not company_name:
        return False
    company_lower = company_name.lower().strip()
    return any(blocked.lower() in company_lower for blocked in COMPANY_BLOCKLIST)


def load_exported_archive() -> dict:
    """Load previously exported companies from the archive.

    Returns a dict mapping (key_type, value) → most_recent_export_date (str YYYY-MM-DD).
    key_type is 'name' or 'domain'. Keeps the most recent date if exported multiple times.
    """
    if not EXPORTED_ARCHIVE_CSV.exists():
        return {}
    df = pd.read_csv(EXPORTED_ARCHIVE_CSV)
    archive = {}
    for _, row in df.iterrows():
        name = str(row.get("company_name", "")).lower().strip()
        domain = str(row.get("company_domain", "")).lower().strip()
        date_exported = str(row.get("date_exported", "")).strip()

        # Keep the most recent export date for each company
        if name and name != "nan":
            key = ("name", name)
            if key not in archive or date_exported > archive[key]:
                archive[key] = date_exported
        if domain and domain != "nan":
            key = ("domain", domain)
            if key not in archive or date_exported > archive[key]:
                archive[key] = date_exported
    return archive


def append_to_exported_archive(top_df: pd.DataFrame):
    """Append exported companies to the archive CSV."""
    records = []
    today = datetime.now().strftime("%Y-%m-%d")
    for _, row in top_df.iterrows():
        records.append({
            "company_name": str(row.get("company_name", "")).strip(),
            "company_domain": str(row.get("company_domain", "")).strip(),
            "date_exported": today,
            "score": row.get("score", 0),
        })
    archive_df = pd.DataFrame(records)
    write_header = not EXPORTED_ARCHIVE_CSV.exists()
    archive_df.to_csv(EXPORTED_ARCHIVE_CSV, mode="a", index=False, header=write_header)
    console.print(f"[green]Appended {len(records)} companies to export archive[/green]")


def load_pending_leads() -> pd.DataFrame:
    """Load leads with status='pending' or 'backlog' from master CSV.

    Backlog leads are re-scored each week so they can compete for a spot
    in the weekly qualified leads export.
    """
    if not MASTER_CSV.exists():
        console.print("[red]Error: master_input.csv not found. Run collector first.[/red]")
        return pd.DataFrame()

    df = pd.read_csv(MASTER_CSV)
    # Backward compatibility: add missing columns from current schema
    for col in MASTER_CSV_HEADERS:
        if col not in df.columns:
            df[col] = ""
    eligible = df[df["status"].isin(["pending", "backlog"])]

    pending_count = len(df[df["status"] == "pending"])
    backlog_count = len(df[df["status"] == "backlog"])
    console.print(f"[cyan]Found {len(eligible)} leads to score ({pending_count} pending + {backlog_count} backlog)[/cyan]")
    return eligible


def build_pipeline_success_section() -> str:
    """Build the pipeline success section for the scoring prompt.

    Fetches companies from Workflow campaigns that reached Engaged+ in Notion,
    and formats them as context for GPT-4o to use as lookalike signals.
    """
    successes = get_pipeline_success_companies()
    if not successes:
        return "=== PIPELINE SUCCESS DATA ===\n\nNo pipeline data yet — score based on ICP criteria above."

    lines = []
    for s in successes:
        parts = [s["company_name"]]
        if s.get("account_type"):
            parts.append(f"({s['account_type']})")
        parts.append(f"— {s['status']}")
        if s.get("trigger"):
            parts.append(f"| Trigger: {s['trigger']}")
        lines.append("- " + " ".join(parts))

    return (
        "=== PIPELINE SUCCESS DATA ===\n\n"
        "These companies from our Workflow campaigns have ENGAGED or progressed further in our pipeline.\n"
        "They represent PROVEN good fits. Companies similar to these (same type, same focus areas, same engagement\n"
        "with student organizations in Germany) should receive a SIGNIFICANT score boost (+1 to +2 points).\n\n"
        + "\n".join(lines)
    )


def score_lead(lead: dict, pipeline_section: str = "") -> tuple[float, str, list]:
    """
    Score a single lead using GPT-4o.

    Args:
        lead: Lead dict with company_name, person_name, context, source
        pipeline_section: Pre-built pipeline success context for the prompt

    Returns:
        Tuple of (score, reasoning, red_flags)
    """
    if not OPENAI_API_KEY:
        console.print("[red]Error: OPENAI_API_KEY not configured[/red]")
        return 0, "No API key", []

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Extract role and entity type from context if available
    context = lead.get("context", "No context")
    person_role = ""
    entity_type = ""

    if "Role:" in context:
        parts = context.split("|")
        for part in parts:
            if "Role:" in part:
                person_role = part.replace("Role:", "").strip()
            if "Type:" in part:
                entity_type = part.replace("Type:", "").strip()

    prompt = SCORING_PROMPT.format(
        company_name=lead.get("company_name", "Unknown"),
        company_domain=lead.get("company_domain", "Unknown"),
        person_name=lead.get("person_name", "Unknown"),
        person_role=person_role or "Unknown",
        trigger=lead.get("trigger", "Unknown"),
        context=context,
        entity_type=entity_type or "Unknown",
        source=lead.get("source", "unknown"),
        pipeline_success_section=pipeline_section
    )

    try:
        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a lead qualification expert for a social impact AI initiative."},
                {"role": "user", "content": prompt}
            ],
            response_format=LeadScore,
            max_tokens=500
        )

        result = response.choices[0].message.parsed
        log_api_usage("ranking_agent", "lead_scoring", "gpt-4o", response.usage, {"company": lead.get("company_name", "")})

        return result.score, result.reasoning, result.red_flags

    except Exception as e:
        console.print(f"[red]GPT-4o scoring error: {e}[/red]")
        return 0, f"Error: {str(e)}", []


def _generate_ranking_email_html(stats: dict) -> str:
    """Generate HTML email body for the weekly ranking report."""
    date_str = datetime.now().strftime("%B %d, %Y")

    def badge(value, color):
        return f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:4px;font-weight:bold;">{value}</span>'

    rows = ""
    metrics = [
        ("Total leads scored", str(stats.get("total_scored", 0)), "#6c757d"),
        ("Qualified (score &ge; 5)", str(stats.get("qualified", 0)), "#28a745"),
        ("With contact person", str(stats.get("with_contact", 0)), "#28a745"),
        ("Without contact (need Apollo)", str(stats.get("without_contact", 0)), "#fd7e14"),
        ("Blocked (engaged+ in Notion)", str(stats.get("blocked", 0)), "#6c757d"),
        ("Re-qualified", str(stats.get("requalified", 0)), "#17a2b8"),
        ("Filtered out (score &lt; 5)", str(stats.get("filtered_out", 0)), "#6c757d"),
        ("Sent to backlog", str(stats.get("backlog", 0)), "#6c757d"),
    ]
    for label, value, color in metrics:
        rows += f"""
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;">{label}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">{badge(value, color)}</td>
        </tr>"""

    # Score stats
    avg_score = stats.get("avg_score", 0)
    min_score = stats.get("min_score", 0)
    max_score = stats.get("max_score", 0)

    # Source breakdown
    sources = stats.get("sources", {})
    source_rows = ""
    if sources:
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            source_rows += f"""
            <tr>
                <td style="padding:4px 12px;border-bottom:1px solid #f5f5f5;">{src}</td>
                <td style="padding:4px 12px;border-bottom:1px solid #f5f5f5;text-align:right;">{count}</td>
            </tr>"""

    source_section = ""
    if source_rows:
        source_section = f"""
        <h3 style="color:#333;margin-top:24px;">Source Breakdown</h3>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
            {source_rows}
        </table>"""

    # Attachment note
    attachment_count = 0
    if stats.get("with_contact", 0) > 0:
        attachment_count += 1
    if stats.get("without_contact", 0) > 0:
        attachment_count += 1

    attachment_note = ""
    if attachment_count > 0:
        attachment_note = f"{attachment_count} CSV file{'s' if attachment_count > 1 else ''} attached"
    else:
        attachment_note = "No CSV files attached (no qualified leads this week)"

    return f"""
    <html>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#333;">
        <div style="background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:24px;border-radius:12px 12px 0 0;">
            <h1 style="margin:0;font-size:22px;">Weekly Qualified Leads Report</h1>
            <p style="margin:8px 0 0;opacity:0.8;font-size:14px;">{date_str}</p>
        </div>

        <div style="background:#fff;border:1px solid #e0e0e0;border-top:none;padding:20px;border-radius:0 0 12px 12px;">
            <h3 style="color:#333;margin-top:0;">Pipeline Summary</h3>
            <table style="width:100%;border-collapse:collapse;font-size:14px;">
                {rows}
            </table>

            <h3 style="color:#333;margin-top:24px;">Score Statistics</h3>
            <table style="width:100%;border-collapse:collapse;font-size:14px;">
                <tr>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;">Average score</td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-weight:bold;">{avg_score:.1f}</td>
                </tr>
                <tr>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;">Min score</td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">{min_score:.1f}</td>
                </tr>
                <tr>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;">Max score</td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">{max_score:.1f}</td>
                </tr>
            </table>

            {source_section}

            <p style="margin-top:24px;padding:12px;background:#f8f9fa;border-radius:8px;font-size:13px;color:#666;">
                {attachment_note}<br>
                <em>Generated by TUM Social AI Ranking Agent</em>
            </p>
        </div>
    </body>
    </html>
    """


def _send_ranking_report(stats: dict, csv_with: Path, csv_without: Path,
                         extra_csvs: List[Path] = None, subject_override: str = "") -> bool:
    """Send weekly ranking report email with CSV attachments."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        console.print("[yellow]Email not configured (GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing). Skipping email.[/yellow]")
        return False

    # Determine recipients: RANKING_REPORT_RECIPIENTS > REPORT_RECIPIENT_EMAIL > GMAIL_ADDRESS
    if RANKING_REPORT_RECIPIENTS:
        recipients = [r.strip() for r in RANKING_REPORT_RECIPIENTS.split(",") if r.strip()]
    elif REPORT_RECIPIENT_EMAIL:
        recipients = [REPORT_RECIPIENT_EMAIL]
    else:
        recipients = [GMAIL_ADDRESS]

    with_contact = stats.get("with_contact", 0)
    qualified = stats.get("qualified", 0)

    if subject_override:
        subject = subject_override
    else:
        subject = f"TUM Social AI \u2014 Weekly Leads: {qualified} qualified ({with_contact} with contacts)"

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    html_body = _generate_ranking_email_html(stats)
    msg.attach(MIMEText(html_body, "html"))

    # Attach CSVs (only if they exist and have data rows)
    all_csvs = [csv_with, csv_without] + (extra_csvs or [])
    for csv_path in all_csvs:
        if csv_path.exists() and csv_path.stat().st_size > 0:
            try:
                df_check = pd.read_csv(csv_path)
                if df_check.empty:
                    continue
            except Exception:
                continue
            with open(csv_path, "rb") as f:
                csv_attachment = MIMEApplication(f.read(), _subtype="csv")
                csv_attachment.add_header("Content-Disposition", "attachment", filename=csv_path.name)
                msg.attach(csv_attachment)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        console.print(f"[green]Ranking report emailed to {', '.join(recipients)}[/green]")
        return True
    except Exception as e:
        console.print(f"[red]Email sending failed: {e}[/red]")
        return False


def run_ranking(subject_override: str = ""):
    """Run the full ranking pipeline."""
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Sales Agent - Ranking Agent[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    console.print("=" * 60)

    # Load pending leads
    pending = load_pending_leads()

    if pending.empty:
        console.print("[yellow]No pending leads to rank. Run collector first.[/yellow]")
        return

    # Build pipeline success context once (shared across all scoring calls)
    pipeline_section = build_pipeline_success_section()

    # Score each lead
    scores = []
    reasonings = []
    all_red_flags = []

    use_progress = console.is_terminal
    progress_ctx = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) if use_progress else None

    if progress_ctx:
        progress_ctx.start()
        task = progress_ctx.add_task("[cyan]Scoring leads...", total=len(pending))

    scored_count = 0
    for idx, row in pending.iterrows():
        lead = row.to_dict()
        company_name = lead.get("company_name", "")

        # Check blocklist first
        if is_blocklisted(company_name):
            console.print(f"[red]Blocklisted: {company_name}[/red]")
            scores.append(0)
            reasonings.append(f"BLOCKLISTED: {company_name} is on the explicit blocklist (e.g., own organization, known false positive)")
            all_red_flags.append(["Blocklisted company"])
        # Check student club blocklist (skip GPT-4o entirely)
        elif is_student_club(company_name):
            console.print(f"[red]Student club auto-disqualified: {company_name}[/red]")
            scores.append(0)
            reasonings.append("AUTO-DISQUALIFIED: Student club/university association (we are a student club ourselves)")
            all_red_flags.append(["Student club/university association"])
        else:
            score, reasoning, red_flags = score_lead(lead, pipeline_section)

            # +1 bonus if we already have a contact person (higher conversion signal)
            person_name = str(lead.get("person_name", "") or "").strip()
            if person_name and person_name != "nan" and score > 0:
                score = min(score + 1, 10.0)
                reasoning += " | +1 contact bonus"

            # Two-step verification: if score >= 5 and name has suspicious patterns,
            # ask GPT-4o-mini to confirm it's not a student club
            if score >= 5 and _needs_student_club_verification(company_name):
                is_club = _verify_student_club_with_llm(company_name)
                if is_club:
                    original_score = score
                    console.print(f"[red]Student club caught by verification: {company_name} (was score {original_score:.1f})[/red]")
                    score = 0
                    reasoning = f"AUTO-DISQUALIFIED: GPT-4o-mini verification confirmed student club/university association (original score: {original_score:.1f})"
                    red_flags = ["Student club (verified by LLM)"]

            scores.append(score)
            reasonings.append(reasoning)
            all_red_flags.append(red_flags)

        scored_count += 1
        if progress_ctx:
            progress_ctx.update(task, advance=1, description=f"[cyan]Scoring: {company_name[:30]}...")
        elif scored_count % 25 == 0:
            console.print(f"[dim]Scored {scored_count}/{len(pending)} leads...[/dim]")

    if progress_ctx:
        progress_ctx.stop()

    # Add scores to pending dataframe
    pending = pending.copy()
    pending["score"] = scores
    pending["reasoning"] = reasonings
    pending["red_flags"] = [", ".join(rf) for rf in all_red_flags]

    # Sort by score descending
    pending = pending.sort_values("score", ascending=False)

    # Minimum score threshold for qualified leads
    MIN_SCORE = 5.0

    # Split into Qualified (score >= 5) and Filtered Out (score < 5)
    qualified = pending[pending["score"] >= MIN_SCORE]
    filtered_out = pending[pending["score"] < MIN_SCORE]

    console.print(f"\n[green]Qualified leads: {len(qualified)} (score >= {MIN_SCORE})[/green]")
    console.print(f"[yellow]Filtered out: {len(filtered_out)} (score < {MIN_SCORE})[/yellow]")

    # Fetch existing companies from Notion Accounts database
    console.print("\n[cyan]Checking for duplicates in Notion Accounts...[/cyan]")
    notion_accounts = get_existing_accounts_from_notion()

    # Fetch existing contacts (names, LinkedIn URLs, emails) for person-level dedup
    existing_contacts = get_existing_contacts_from_notion()

    # Split qualified leads into: blocked (engaged+), requalification candidates (low status), and new
    blocked_leads = []    # Status >= Engaged → skip from CSV
    requalify_candidates = []  # Status < Engaged, no active campaign → MAY re-enter pipeline (pending weekly shortlist)
    active_campaign_leads = []  # Active campaign < 1 month → skip
    new_leads = []        # Not in Notion at all
    trigger_updates = 0

    for idx, row in qualified.iterrows():
        company_name = str(row.get("company_name", "")).strip()
        company_domain = str(row.get("company_domain", "")).strip()
        normalized_name = company_name.lower()
        new_trigger = str(row.get("trigger", "")).strip()

        # Check if company exists in Notion (by name or domain)
        notion_page = None
        match_reason = ""

        if normalized_name in notion_accounts["company_names"]:
            notion_page = notion_accounts["company_names"][normalized_name]
            match_reason = f"company name '{company_name}' exists in Notion"
        elif company_domain and company_domain in notion_accounts["domains"]:
            notion_page = notion_accounts["domains"][company_domain]
            match_reason = f"domain '{company_domain}' exists in Notion"

        if notion_page:
            account_page_id = notion_page["page_id"]
            account_status = notion_page.get("status", "")
            campaign_ids = notion_page.get("campaign_ids", [])

            # Update trigger in Notion regardless of status
            if new_trigger:
                if update_trigger_in_notion(account_page_id, new_trigger):
                    trigger_updates += 1

            # Add contact to Notion Contacts DB regardless of status
            person_name = str(row.get("person_name", "")).strip()
            linkedin_url = str(row.get("linkedin_url_contact", "")).strip()

            if person_name and person_name != "nan":
                create_contact_in_notion(
                    person_name=person_name,
                    linkedin_url=linkedin_url if linkedin_url != "nan" else "",
                    account_page_id=account_page_id
                )

            # Decide: block, skip (active campaign), or candidate for re-qualification
            if is_status_engaged_or_above(account_status):
                # Status >= Engaged → always BLOCK
                blocked_leads.append({
                    "company": company_name,
                    "score": row.get("score", 0),
                    "reason": f"{match_reason} (status: {account_status})"
                })
                if account_status:
                    reset_account_status_if_stale(account_page_id, account_status, company_name)
            elif _has_active_campaign(campaign_ids):
                # Active Workflow campaign < 1 month old → don't re-qualify yet
                active_campaign_leads.append({
                    "company": company_name,
                    "score": row.get("score", 0),
                    "reason": f"{match_reason} (status: {account_status}, active campaign: {', '.join(campaign_ids)})"
                })
                console.print(f"[yellow]Skipping: {company_name} — campaign within {CAMPAIGN_COOLDOWN_DAYS}d cooldown ({', '.join(campaign_ids)})[/yellow]")
            else:
                # Low-engagement status + no active campaign → candidate for re-qualification
                # (actual re-qualification only if shortlisted into weekly top CSVs)
                requalify_candidates.append(row)
                console.print(f"[dim]Re-qualify candidate: {company_name} (status: {account_status or 'none'})[/dim]")
        else:
            new_leads.append(row)

    if blocked_leads:
        console.print(f"[yellow]Blocked {len(blocked_leads)} engaged+ companies from CSV:[/yellow]")
        for dup in blocked_leads[:5]:
            console.print(f"  - {dup['company']} (score {dup['score']:.1f}): {dup['reason']}")
        if len(blocked_leads) > 5:
            console.print(f"  ... and {len(blocked_leads) - 5} more")

    if active_campaign_leads:
        console.print(f"[yellow]Skipped {len(active_campaign_leads)} companies with active campaigns (<1 month):[/yellow]")
        for ac in active_campaign_leads[:5]:
            console.print(f"  - {ac['company']} (score {ac['score']:.1f}): {ac['reason']}")

    if trigger_updates > 0:
        console.print(f"[green]Updated triggers for {trigger_updates} existing companies in Notion[/green]")

    # Convert new_leads + requalify_candidates back to dataframe
    # (candidates compete for weekly slots; only those shortlisted get "requalified" status)
    all_qualifying = new_leads + requalify_candidates
    requalify_candidate_names = set(
        str(r.get("company_name", "")).strip() for r in requalify_candidates
    )
    qualified_new = pd.DataFrame(all_qualifying) if all_qualifying else pd.DataFrame()

    # Filter previously exported companies — enforce 3-month cooldown
    # A previously-exported lead can only re-enter the weekly list if:
    #   1. It was exported 3+ months ago (CAMPAIGN_COOLDOWN_DAYS), AND
    #   2. Its Notion status is <= "Engaged" (checked earlier via Notion dedup)
    # Having a new contact alone is NOT enough — cooldown must also be satisfied.
    exported_archive = load_exported_archive()
    previously_exported = []
    cooldown_blocked = []

    if qualified_new.empty:
        console.print("[yellow]No new leads to export (all blocked or already in Notion)[/yellow]")
        weekly_qualified = pd.DataFrame()
        backlog = pd.DataFrame()
    else:
        if exported_archive:
            truly_new = []
            readmitted = 0
            today = datetime.now()
            for idx, row in qualified_new.iterrows():
                name = str(row.get("company_name", "")).lower().strip()
                domain = str(row.get("company_domain", "")).lower().strip()

                # Find the most recent export date for this company
                export_date_str = None
                if ("name", name) in exported_archive:
                    export_date_str = exported_archive[("name", name)]
                if domain and domain != "nan" and ("domain", domain) in exported_archive:
                    domain_date = exported_archive[("domain", domain)]
                    if export_date_str is None or domain_date > export_date_str:
                        export_date_str = domain_date

                if export_date_str:
                    # Company was previously exported — check cooldown
                    try:
                        export_date = datetime.strptime(export_date_str, "%Y-%m-%d")
                        days_since_export = (today - export_date).days
                    except ValueError:
                        days_since_export = 0  # Can't parse date → treat as recent

                    if days_since_export < CAMPAIGN_COOLDOWN_DAYS:
                        # Still within cooldown — block regardless of new contact
                        cooldown_blocked.append(row)
                        console.print(
                            f"[yellow]Cooldown block: {row.get('company_name')} — "
                            f"exported {days_since_export}d ago (need {CAMPAIGN_COOLDOWN_DAYS}d)[/yellow]"
                        )
                    else:
                        # Cooldown expired — re-admit (new contact is a bonus, not required)
                        truly_new.append(row)
                        readmitted += 1
                        console.print(
                            f"[green]Re-admitting {row.get('company_name')}: "
                            f"cooldown expired ({days_since_export}d since last export)[/green]"
                        )
                else:
                    truly_new.append(row)
            qualified_new = pd.DataFrame(truly_new) if truly_new else pd.DataFrame()
            if cooldown_blocked:
                console.print(f"[yellow]Blocked {len(cooldown_blocked)} leads within {CAMPAIGN_COOLDOWN_DAYS}-day cooldown[/yellow]")
            if readmitted:
                console.print(f"[green]Re-admitted {readmitted} leads past cooldown period[/green]")

        if qualified_new.empty:
            console.print("[yellow]No new leads to export (all previously exported or duplicates)[/yellow]")
            weekly_qualified = pd.DataFrame()
            backlog = pd.DataFrame()
        else:
            # Cap at 25 qualified leads per week; rest goes to backlog
            MAX_QUALIFIED = 25
            weekly_qualified = qualified_new.head(MAX_QUALIFIED)
            backlog = qualified_new.iloc[MAX_QUALIFIED:]

            console.print(f"[green]Qualified leads for Apollo: {len(weekly_qualified)} (capped at {MAX_QUALIFIED})[/green]")
            console.print(f"[green]Score range: {weekly_qualified['score'].min():.1f} - {weekly_qualified['score'].max():.1f}[/green]")
            if not backlog.empty:
                console.print(f"[yellow]Overflow to backlog: {len(backlog)} leads (qualified but beyond cap)[/yellow]")

    # Determine which requalify candidates actually made the weekly shortlist
    # Only those get "requalified" status — the rest stay as backlog
    requalified_leads = []
    if not weekly_qualified.empty:
        shortlisted_names = set(weekly_qualified["company_name"].dropna().str.strip())
        for row in requalify_candidates:
            cname = str(row.get("company_name", "")).strip()
            if cname in shortlisted_names:
                requalified_leads.append(row)
        if requalified_leads:
            console.print(f"[green]Re-qualified {len(requalified_leads)} companies (shortlisted into weekly top CSVs)[/green]")

    # Generate requalified & blocked CSV for review
    rq_bl_records = []
    for row in requalified_leads:
        rq_bl_records.append({
            "company_name": row.get("company_name", ""),
            "company_domain": row.get("company_domain", ""),
            "person_name": row.get("person_name", ""),
            "score": row.get("score", 0),
            "notion_status": "re-qualified (shortlisted)",
            "action": "re-entered pipeline",
        })
    for bl in blocked_leads:
        rq_bl_records.append({
            "company_name": bl["company"],
            "company_domain": "",
            "person_name": "",
            "score": bl["score"],
            "notion_status": bl["reason"],
            "action": "blocked (engaged+)",
        })
    for ac in active_campaign_leads:
        rq_bl_records.append({
            "company_name": ac["company"],
            "company_domain": "",
            "person_name": "",
            "score": ac["score"],
            "notion_status": ac["reason"],
            "action": "skipped (active campaign <1 month)",
        })
    if rq_bl_records:
        rq_bl_df = pd.DataFrame(rq_bl_records)
        rq_bl_df.to_csv(REQUALIFIED_BLOCKED_CSV, index=False)
        console.print(f"[cyan]Saved {len(rq_bl_records)} requalified/blocked/skipped leads to {REQUALIFIED_BLOCKED_CSV.name}[/cyan]")

    # Split qualified leads into: with contact vs company-only
    export_columns = ["date_added", "company_name", "company_domain", "person_name",
                      "linkedin_url_contact", "linkedin_url_post", "trigger", "score",
                      "reasoning", "source"]

    if not weekly_qualified.empty:
        # Split: leads with contact (person_name present) vs company-only
        has_contact_mask = (
            weekly_qualified["person_name"].notna() &
            (weekly_qualified["person_name"].astype(str).str.strip() != "") &
            (weekly_qualified["person_name"].astype(str).str.lower() != "nan")
        )
        with_contact = weekly_qualified[has_contact_mask]
        without_contact = weekly_qualified[~has_contact_mask]

        # Display leads with contact
        if not with_contact.empty:
            table = Table(title=f"Weekly Qualified Leads — With Contact ({len(with_contact)})")
            table.add_column("#", style="dim")
            table.add_column("Company", style="cyan")
            table.add_column("Contact", style="white")
            table.add_column("Score", style="green")
            table.add_column("Reasoning", style="dim", max_width=40)

            for i, (_, row) in enumerate(with_contact.iterrows(), 1):
                table.add_row(
                    str(i),
                    str(row.get("company_name", ""))[:25],
                    str(row.get("person_name", ""))[:20],
                    f"{row['score']:.1f}",
                    str(row.get("reasoning", ""))[:40]
                )
            console.print(table)

        # Display company-only leads
        if not without_contact.empty:
            table2 = Table(title=f"No Contact Found ({len(without_contact)} — Manual Lookup Needed)")
            table2.add_column("#", style="dim")
            table2.add_column("Company", style="cyan")
            table2.add_column("Domain", style="white")
            table2.add_column("Score", style="green")

            for i, (_, row) in enumerate(without_contact.iterrows(), 1):
                table2.add_row(
                    str(i),
                    str(row.get("company_name", ""))[:25],
                    str(row.get("company_domain", ""))[:25],
                    f"{row['score']:.1f}"
                )
            console.print(table2)

        # Save leads WITH contact → weekly_qualified_leads.csv (for Apollo)
        if not with_contact.empty:
            available = [c for c in export_columns if c in with_contact.columns]
            with_contact[available].to_csv(QUALIFIED_CSV, index=False)
            console.print(f"[green]Saved {len(with_contact)} leads with contact to {QUALIFIED_CSV.name}[/green]")
        else:
            # Write empty CSV with headers so upload agent can still read it
            pd.DataFrame(columns=export_columns).to_csv(QUALIFIED_CSV, index=False)
            console.print("[yellow]No leads with contact info this week[/yellow]")

        # Save leads WITHOUT contact → weekly_qualified_leads_no_contact.csv (for Apollo to try)
        if not without_contact.empty:
            available = [c for c in export_columns if c in without_contact.columns]
            without_contact[available].to_csv(QUALIFIED_NO_CONTACT_CSV, index=False)
            console.print(f"[yellow]Saved {len(without_contact)} leads without contact to {QUALIFIED_NO_CONTACT_CSV.name}[/yellow]")
        else:
            pd.DataFrame(columns=export_columns).to_csv(QUALIFIED_NO_CONTACT_CSV, index=False)

        # Archive ALL exported companies (both with and without contact)
        append_to_exported_archive(weekly_qualified)

        console.print(f"\n[dim]Summary: {len(with_contact)} with contact (Apollo) + {len(without_contact)} no contact (Apollo attempt)[/dim]")
    else:
        console.print("[yellow]No new leads to export[/yellow]")

    # Note: Notion upload happens AFTER Apollo enrichment (not here)
    console.print("\n[dim]Notion upload skipped - will happen after Apollo enrichment[/dim]")

    # Update master CSV with scores, reasoning, and statuses
    master_df = pd.read_csv(MASTER_CSV)

    # Create lookup dicts for scores and reasoning from all processed leads
    score_lookup = dict(zip(pending["company_name"], pending["score"]))
    reasoning_lookup = dict(zip(pending["company_name"], pending["reasoning"]))

    # Save backlog (qualified leads beyond cap)
    if not backlog.empty:
        backlog_columns = ["date_added", "company_name", "company_domain", "person_name", "linkedin_url_contact", "linkedin_url_post", "trigger", "score", "reasoning", "source"]
        available_backlog_columns = [col for col in backlog_columns if col in backlog.columns]
        backlog_export = backlog[available_backlog_columns].copy()

        if BACKLOG_CSV.exists():
            existing_backlog = pd.read_csv(BACKLOG_CSV)
            backlog_export = pd.concat([existing_backlog, backlog_export], ignore_index=True)

        backlog_export.to_csv(BACKLOG_CSV, index=False)
        console.print(f"[dim]Saved {len(backlog)} leads to backlog (qualified but beyond weekly cap)[/dim]")

    # Get company sets for status updates
    qualified_companies = set(weekly_qualified["company_name"].dropna()) if not weekly_qualified.empty else set()
    backlog_companies = set(backlog["company_name"].dropna()) if not backlog.empty else set()
    filtered_companies = set(filtered_out["company_name"].dropna())
    blocked_companies = set([d["company"] for d in blocked_leads])
    active_campaign_companies = set([d["company"] for d in active_campaign_leads])
    requalified_companies = set(pd.DataFrame(requalified_leads)["company_name"].dropna()) if requalified_leads else set()
    all_export_blocked = previously_exported + cooldown_blocked
    prev_exported_companies = set(pd.DataFrame(all_export_blocked)["company_name"].dropna()) if all_export_blocked else set()

    # Update scores and reasoning for all processed leads
    for idx, row in master_df.iterrows():
        company = row["company_name"]
        if company in score_lookup:
            master_df.at[idx, "score"] = score_lookup[company]
            master_df.at[idx, "reasoning"] = reasoning_lookup[company]

    # Eligible statuses: leads that were processed this run (pending + backlog)
    eligible_mask = master_df["status"].isin(["pending", "backlog"])

    # Update statuses — requalified leads get "requalified", new leads get "qualified_for_apollo"
    requalified_in_qualified = qualified_companies & requalified_companies
    new_in_qualified = qualified_companies - requalified_companies

    master_df.loc[
        (master_df["company_name"].isin(new_in_qualified)) & eligible_mask,
        "status"
    ] = "qualified_for_apollo"

    master_df.loc[
        (master_df["company_name"].isin(requalified_in_qualified)) & eligible_mask,
        "status"
    ] = "requalified"

    # Backlog: qualified but beyond weekly cap
    master_df.loc[
        (master_df["company_name"].isin(backlog_companies)) & eligible_mask,
        "status"
    ] = "backlog"

    master_df.loc[
        (master_df["company_name"].isin(filtered_companies)) & eligible_mask,
        "status"
    ] = "filtered_out"

    # Mark blocked (engaged+) companies as duplicate in Notion
    master_df.loc[
        (master_df["company_name"].isin(blocked_companies)) & eligible_mask,
        "status"
    ] = "duplicate_in_notion"

    # Mark active campaign companies (being targeted, within cooldown period)
    master_df.loc[
        (master_df["company_name"].isin(active_campaign_companies)) & eligible_mask,
        "status"
    ] = "duplicate_in_notion"

    # Mark previously exported companies
    master_df.loc[
        (master_df["company_name"].isin(prev_exported_companies)) & eligible_mask,
        "status"
    ] = "exported_previously"

    master_df.to_csv(MASTER_CSV, index=False)
    console.print(f"[green]Updated master CSV with scores, reasoning, and statuses[/green]")

    # Final Summary
    console.print("\n" + "=" * 40)
    summary_table = Table(title="Ranking Complete")
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", style="green")
    summary_table.add_row("Leads scored", str(len(pending)))
    summary_table.add_row("Qualified (score >= 5)", str(len(qualified)))
    summary_table.add_row("Filtered out (score < 5)", str(len(filtered_out)))
    summary_table.add_row("Blocked (engaged+)", str(len(blocked_leads)))
    summary_table.add_row("Skipped (active campaign)", str(len(active_campaign_leads)))
    summary_table.add_row("Re-qualified", str(len(requalified_leads)))
    summary_table.add_row("Weekly qualified", str(len(weekly_qualified)))
    if not weekly_qualified.empty:
        summary_table.add_row("  → With contact", str(len(with_contact)))
        summary_table.add_row("  → No contact", str(len(without_contact)))
    summary_table.add_row("Overflow to backlog", str(len(backlog)))
    if not weekly_qualified.empty:
        summary_table.add_row("Average score", f"{weekly_qualified['score'].mean():.1f}")

    console.print(summary_table)

    # Send ranking report email
    wc_count = len(with_contact) if not weekly_qualified.empty else 0
    nc_count = len(without_contact) if not weekly_qualified.empty else 0

    # Build source distribution from all scored leads
    source_dist = {}
    if not weekly_qualified.empty:
        for src in weekly_qualified["source"].dropna():
            src_str = str(src).strip()
            if src_str and src_str != "nan":
                source_dist[src_str] = source_dist.get(src_str, 0) + 1

    email_stats = {
        "total_scored": len(pending),
        "qualified": len(qualified),
        "filtered_out": len(filtered_out),
        "blocked": len(blocked_leads),
        "requalified": len(requalified_leads),
        "with_contact": wc_count,
        "without_contact": nc_count,
        "backlog": len(backlog),
        "avg_score": weekly_qualified["score"].mean() if not weekly_qualified.empty else 0,
        "min_score": weekly_qualified["score"].min() if not weekly_qualified.empty else 0,
        "max_score": weekly_qualified["score"].max() if not weekly_qualified.empty else 0,
        "sources": source_dist,
    }
    extra_csvs = []
    if REQUALIFIED_BLOCKED_CSV.exists() and REQUALIFIED_BLOCKED_CSV.stat().st_size > 0:
        extra_csvs.append(REQUALIFIED_BLOCKED_CSV)

    _send_ranking_report(email_stats, QUALIFIED_CSV, QUALIFIED_NO_CONTACT_CSV,
                         extra_csvs=extra_csvs, subject_override=subject_override)

    console.print(f"\n[bold green]Next step: Send both CSVs to Apollo, then run upload agent[/bold green]")
    console.print(f"[dim]With contact    → {QUALIFIED_CSV.name}[/dim]")
    console.print(f"[dim]Without contact → {QUALIFIED_NO_CONTACT_CSV.name}[/dim]")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", type=str, default="", help="Override email subject line")
    args = parser.parse_args()
    run_ranking(subject_override=args.subject)
