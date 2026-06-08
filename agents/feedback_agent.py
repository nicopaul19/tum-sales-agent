"""
Feedback Agent — Weekly outreach effectiveness analysis and A/B test evaluation.

Analyzes contacts with outreach messages, classifies outcomes as success/failure/skip,
computes A/B variant statistics, ingests manual copywriter iterations from Notion,
runs GPT-4o pattern analysis, writes learnings to data/prompts/outreach_learnings.md,
and sends an HTML summary email.

Usage:
    python -m agents.feedback_agent                    # full run
    python -m agents.feedback_agent --dry-run          # analyze without writing learnings or sending email
    python -m agents.feedback_agent --min-data 5       # override minimum resolved outcomes (default: 10)
"""
import sys
import smtplib
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from openai import OpenAI
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import (
    OPENAI_API_KEY,
    NOTION_TOKEN,
    NOTION_DB_ACCOUNTS_ID,
    NOTION_DB_CONTACTS_ID,
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    REPORT_RECIPIENT_EMAIL,
)
from utils.api_logger import log_api_usage
from utils.iterations_client import load_iterations, mark_iterations_processed
from utils.campaign_tracker import (
    build_campaign_records,
    fetch_accounts as fetch_campaign_accounts,
    fetch_contacts as fetch_campaign_contacts,
    sync_campaign_tracker,
)
from agents.notion_cleanup import STATUS_HIERARCHY

console = Console()

LEARNINGS_PATH = Path(__file__).parent.parent / "data" / "prompts" / "outreach_learnings.md"

# Import FEEDBACK_REPORT_RECIPIENTS — may not exist in older configs
try:
    from utils.config import FEEDBACK_REPORT_RECIPIENTS
except ImportError:
    FEEDBACK_REPORT_RECIPIENTS = None

# Outcome classification thresholds
ENGAGED_INDEX = 7       # STATUS_HIERARCHY index for "Engaged"
UNQUALIFIED_INDEX = 12  # STATUS_HIERARCHY index for "Prospect Unqualified"
STALE_DAYS = 30         # Days stuck at "Contacted" before classified as failure

# Max samples per category to send to GPT-4o
MAX_SAMPLES = 20


# =============================================================================
# Pydantic model for GPT-4o structured output
# =============================================================================

class FeedbackAnalysis(BaseModel):
    """Structured output from GPT-4o outreach analysis."""
    winning_patterns: List[str] = Field(
        description="Patterns observed in successful outreach messages (3-7 bullet points)"
    )
    losing_patterns: List[str] = Field(
        description="Patterns observed in failed outreach messages (3-7 bullet points)"
    )
    recommendations: List[str] = Field(
        description="Actionable improvements for future outreach (3-5 bullet points)"
    )
    tone_observations: str = Field(
        description="Observations about tone, style, and voice across messages (1-3 sentences)"
    )
    ab_winner: str = Field(
        description="Which A/B variant performed better: 'A', 'B', or 'inconclusive'"
    )
    ab_interpretation: str = Field(
        description="Why one variant works better or why results are inconclusive (1-3 sentences)"
    )
    ab_confidence: str = Field(
        description="Confidence in A/B result: 'high', 'medium', or 'low'"
    )
    iteration_learnings: List[str] = Field(
        description="Reusable lessons distilled from manual Notion copywriter iterations, if any"
    )


# =============================================================================
# Notion data fetching
# =============================================================================

def _has_outreach(record: dict) -> bool:
    """Return True when a CRM record has any generated outreach copy."""
    return bool(
        record.get("linkedin_first")
        or record.get("linkedin_fu")
        or record.get("email_body")
        or record.get("email_subject")
    )


def fetch_contacts_with_outreach(contacts_db_id: str = "") -> List[dict]:
    """
    Fetch all campaign-scoped outreach records from the CRM.

    This intentionally uses the Campaign Tracker extraction layer rather than
    querying only Contacts where LinkedIn 1st Cold is populated. That older
    query missed email-only campaigns and account-level NGO outreach.
    """
    accounts = fetch_campaign_accounts()
    raw_contacts = fetch_campaign_contacts()
    campaign_records = build_campaign_records(accounts, raw_contacts)

    outreach_records = []
    seen = set()
    for campaign in campaign_records:
        campaign_id = campaign.get("campaign_id", "")
        for record in campaign.get("contacts", []):
            if not _has_outreach(record):
                continue
            key = (campaign_id, record.get("id", ""))
            if key in seen:
                continue
            seen.add(key)
            outreach_records.append({
                "id": record.get("id", ""),
                "storage": record.get("storage", "contact"),
                "campaign_id": campaign_id,
                "contact_name": record.get("contact_name") or record.get("name") or "(account-level outreach)",
                "company_name": record.get("company_name", ""),
                "account_status": record.get("account_status", ""),
                "account_last_edited": record.get("account_last_edited") or record.get("last_edited_time", ""),
                "ab_variant": (record.get("ab_variant") or "").upper(),
                "linkedin_first": record.get("linkedin_first", ""),
                "linkedin_fu": record.get("linkedin_fu", ""),
                "email_body": record.get("email_body", ""),
                "email_subject": record.get("email_subject", ""),
            })

    return outreach_records


# =============================================================================
# Outcome classification
# =============================================================================

def classify_outcome(contact: dict) -> str:
    """
    Classify a contact's outreach outcome.

    Returns: "success", "failure", or "skip"
    """
    status = contact.get("account_status", "")

    # Get status index
    try:
        idx = STATUS_HIERARCHY.index(status)
    except ValueError:
        idx = -1

    # SUCCESS: status >= Engaged (idx 7+), but NOT Prospect Unqualified (idx 12)
    if idx >= ENGAGED_INDEX and idx != UNQUALIFIED_INDEX:
        return "success"

    # FAILURE: Prospect Unqualified
    if idx == UNQUALIFIED_INDEX:
        return "failure"

    # FAILURE: stuck at any Contacted state for > STALE_DAYS
    if status.startswith("Contacted"):
        last_edited = contact.get("account_last_edited", "")
        if last_edited:
            try:
                edited_dt = datetime.fromisoformat(last_edited.replace("Z", "+00:00"))
                days_since = (datetime.now(timezone.utc) - edited_dt).days
                if days_since > STALE_DAYS:
                    return "failure"
            except (ValueError, TypeError):
                pass
        return "skip"  # Too early to judge

    # Everything else: too early to judge
    return "skip"


# =============================================================================
# A/B test statistics
# =============================================================================

def compute_ab_stats(contacts: List[dict]) -> dict:
    """Compute A/B test statistics from classified contacts."""
    stats = {
        "A": {"success": 0, "failure": 0, "skip": 0, "total": 0},
        "B": {"success": 0, "failure": 0, "skip": 0, "total": 0},
        "none": {"success": 0, "failure": 0, "skip": 0, "total": 0},
    }

    for c in contacts:
        variant = c.get("ab_variant", "")
        outcome = c.get("outcome", "skip")
        bucket = variant if variant in ("A", "B") else "none"
        stats[bucket][outcome] += 1
        stats[bucket]["total"] += 1

    # Compute rates for A and B
    for v in ("A", "B"):
        resolved = stats[v]["success"] + stats[v]["failure"]
        if resolved > 0:
            stats[v]["success_rate"] = stats[v]["success"] / resolved
        else:
            stats[v]["success_rate"] = None

    return stats


def _success_rate(success: int, failure: int) -> Optional[float]:
    resolved = success + failure
    if resolved <= 0:
        return None
    return success / resolved


def _ab_winner(stats: dict) -> str:
    a_rate = _success_rate(stats["A"]["success"], stats["A"]["failure"])
    b_rate = _success_rate(stats["B"]["success"], stats["B"]["failure"])
    a_resolved = stats["A"]["success"] + stats["A"]["failure"]
    b_resolved = stats["B"]["success"] + stats["B"]["failure"]
    if a_resolved == 0 and b_resolved == 0:
        return "No Data"
    if min(a_resolved, b_resolved) < 3 or a_rate is None or b_rate is None:
        return "Inconclusive"
    if abs(a_rate - b_rate) < 0.05:
        return "Inconclusive"
    return "A" if a_rate > b_rate else "B"


def compute_campaign_stats(contacts: List[dict]) -> dict:
    """Compute per-campaign outcome and A/B statistics."""
    stats: dict = {}
    for c in contacts:
        campaign = c.get("campaign_id") or "Unknown"
        if campaign not in stats:
            stats[campaign] = {
                "success": 0,
                "failure": 0,
                "skip": 0,
                "total": 0,
                "account_level": 0,
                "A": {"success": 0, "failure": 0, "skip": 0, "total": 0},
                "B": {"success": 0, "failure": 0, "skip": 0, "total": 0},
                "none": {"success": 0, "failure": 0, "skip": 0, "total": 0},
            }
        bucket = stats[campaign]
        outcome = c.get("outcome", "skip")
        bucket[outcome] += 1
        bucket["total"] += 1
        if c.get("storage") == "account":
            bucket["account_level"] += 1
        variant = c.get("ab_variant", "")
        variant_bucket = variant if variant in ("A", "B") else "none"
        bucket[variant_bucket][outcome] += 1
        bucket[variant_bucket]["total"] += 1

    for campaign_stats in stats.values():
        campaign_stats["success_rate"] = _success_rate(
            campaign_stats["success"],
            campaign_stats["failure"],
        )
        for variant in ("A", "B"):
            campaign_stats[variant]["success_rate"] = _success_rate(
                campaign_stats[variant]["success"],
                campaign_stats[variant]["failure"],
            )
        campaign_stats["ab_winner"] = _ab_winner(campaign_stats)

    return stats


def format_campaign_stats_for_prompt(campaign_stats: dict, limit: int = 20) -> str:
    """Format campaign stats for GPT prompt context."""
    if not campaign_stats:
        return ""
    lines = []
    sorted_items = sorted(
        campaign_stats.items(),
        key=lambda item: (item[1].get("success", 0) + item[1].get("failure", 0), item[1].get("total", 0)),
        reverse=True,
    )
    for campaign, stats in sorted_items[:limit]:
        rate = f"{stats['success_rate']:.0%}" if stats.get("success_rate") is not None else "N/A"
        a_rate = f"{stats['A']['success_rate']:.0%}" if stats["A"].get("success_rate") is not None else "N/A"
        b_rate = f"{stats['B']['success_rate']:.0%}" if stats["B"].get("success_rate") is not None else "N/A"
        lines.append(
            f"- {campaign}: {stats['total']} targeted records, "
            f"{stats['success']} success, {stats['failure']} failure, {stats['skip']} pending, "
            f"success rate {rate}, A {a_rate}, B {b_rate}, winner {stats.get('ab_winner', 'No Data')}"
        )
    return "\n".join(lines)


# =============================================================================
# GPT-4o analysis
# =============================================================================

def run_gpt_analysis(
    successes: List[dict],
    failures: List[dict],
    ab_stats: dict,
    client: OpenAI,
    manual_iterations: str = "",
    campaign_stats: Optional[dict] = None,
) -> Optional[FeedbackAnalysis]:
    """Run GPT-4o pattern analysis on outcomes, A/B stats, and manual iterations."""

    def _format_contact(c: dict) -> str:
        lines = [f"Contact: {c['contact_name']} @ {c['company_name']}"]
        if c.get("campaign_id"):
            lines.append(f"Campaign: {c['campaign_id']}")
        if c.get("account_status"):
            lines.append(f"Account status: {c['account_status']}")
        if c.get("ab_variant"):
            lines.append(f"Variant: {c['ab_variant']}")
        lines.append(f"LinkedIn 1st Cold: {c['linkedin_first']}")
        if c.get("linkedin_fu"):
            lines.append(f"LinkedIn FU: {c['linkedin_fu']}")
        if c.get("email_subject"):
            lines.append(f"Email Subject: {c['email_subject']}")
        if c.get("email_body"):
            lines.append(f"Email Body: {c['email_body']}")
        return "\n".join(lines)

    success_text = "\n\n---\n\n".join(_format_contact(s) for s in successes[:MAX_SAMPLES])
    failure_text = "\n\n---\n\n".join(_format_contact(f) for f in failures[:MAX_SAMPLES])

    # A/B stats summary
    ab_summary_lines = []
    for v in ("A", "B"):
        s = ab_stats[v]
        rate = f"{s['success_rate']:.0%}" if s.get("success_rate") is not None else "N/A"
        ab_summary_lines.append(
            f"Variant {v}: {s['total']} total, {s['success']} success, "
            f"{s['failure']} failure, {s['skip']} pending, success rate: {rate}"
        )
    ab_summary = "\n".join(ab_summary_lines)
    manual_iterations_section = (
        f"\n## MANUAL COPYWRITER ITERATIONS FROM NOTION\n{manual_iterations}\n"
        if manual_iterations.strip()
        else ""
    )
    campaign_summary = format_campaign_stats_for_prompt(campaign_stats or {})

    prompt = f"""You are analyzing outreach message effectiveness for TUM Social AI, a student AI-for-Good initiative at TUM.

## SUCCESSFUL OUTREACH (led to engagement, meetings, or partnerships)
{success_text or "(No resolved successes in this run.)"}

## FAILED OUTREACH (prospect unqualified or no response after 30+ days)
{failure_text or "(No resolved failures in this run.)"}

## A/B TEST DATA
Variant A framing: "We're developing AI solutions with partners like UN Women"
Variant B framing: "We're multiplying the impact of our social partners like UN Women in over 50 countries through custom AI tools"

{ab_summary}

## CAMPAIGN PERFORMANCE BY CAMPAIGN
{campaign_summary or "(No campaign-scoped data available.)"}
{manual_iterations_section}

## YOUR TASK
1. Identify patterns that distinguish successful vs. failed outreach messages
2. Provide actionable recommendations for improving future messages
3. Analyze the A/B test results and determine which framing performs better
4. Distill manual Notion copywriter iterations into reusable prompt guidance
5. Note any tone or style observations

Be specific and actionable. Reference concrete examples from the messages above."""

    try:
        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert outreach analyst. Analyze message effectiveness patterns and A/B test results."},
                {"role": "user", "content": prompt},
            ],
            response_format=FeedbackAnalysis,
            max_tokens=2000,
        )

        result = response.choices[0].message.parsed

        log_api_usage(
            "feedback_agent", "pattern_analysis", "gpt-4o",
            response.usage,
            {
                "successes": len(successes),
                "failures": len(failures),
                "manual_iterations": bool(manual_iterations.strip()),
            }
        )

        return result

    except Exception as e:
        console.print(f"[red]GPT-4o analysis error: {e}[/red]")
        return None


# =============================================================================
# Learnings file writer
# =============================================================================

def write_learnings_file(
    analysis: FeedbackAnalysis,
    ab_stats: dict,
    n_success: int,
    n_failure: int,
    n_iterations: int = 0,
    campaign_stats: Optional[dict] = None,
):
    """Write analysis results to the learnings markdown file."""
    LEARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Outreach Learnings (Auto-generated)",
        f"# Last updated: {datetime.now().strftime('%Y-%m-%d')} | {n_success} successes, {n_failure} failures analyzed",
        "",
    ]

    # A/B Test Results
    lines.append("## A/B TEST RESULTS")
    lines.append(f"- Winner: **{analysis.ab_winner}** (confidence: {analysis.ab_confidence})")
    lines.append(f"- Interpretation: {analysis.ab_interpretation}")
    for v in ("A", "B"):
        s = ab_stats[v]
        rate = f"{s['success_rate']:.0%}" if s.get("success_rate") is not None else "N/A"
        lines.append(f"- Variant {v}: {s['success']}/{s['success'] + s['failure']} resolved → {rate} success rate")
    lines.append("")

    if campaign_stats:
        lines.append("## CAMPAIGN PERFORMANCE")
        for campaign, stats in sorted(campaign_stats.items()):
            rate = f"{stats['success_rate']:.0%}" if stats.get("success_rate") is not None else "N/A"
            lines.append(
                f"- {campaign}: {stats['success']} success / {stats['failure']} failure / "
                f"{stats['skip']} pending across {stats['total']} targeted records; "
                f"A/B winner: {stats.get('ab_winner', 'No Data')}; success rate: {rate}"
            )
        lines.append("")

    if analysis.iteration_learnings:
        lines.append("## MANUAL NOTION ITERATIONS")
        lines.append(f"- Source: {n_iterations} unprocessed item(s) from the Notion Iterations page")
        for item in analysis.iteration_learnings:
            lines.append(f"- {item}")
        lines.append("")

    # Winning Patterns
    lines.append("## WINNING PATTERNS")
    for p in analysis.winning_patterns:
        lines.append(f"- {p}")
    lines.append("")

    # Losing Patterns
    lines.append("## LOSING PATTERNS")
    for p in analysis.losing_patterns:
        lines.append(f"- {p}")
    lines.append("")

    # Recommendations
    lines.append("## RECOMMENDATIONS")
    for r in analysis.recommendations:
        lines.append(f"- {r}")
    lines.append("")

    # Tone & Style
    lines.append("## TONE & STYLE NOTES")
    lines.append(analysis.tone_observations)
    lines.append("")

    LEARNINGS_PATH.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]Learnings written to {LEARNINGS_PATH}[/green]")


# =============================================================================
# Email report
# =============================================================================

def _generate_feedback_email_html(
    analysis: FeedbackAnalysis,
    ab_stats: dict,
    n_success: int,
    n_failure: int,
    n_skip: int,
    n_iterations: int = 0,
    campaign_stats: Optional[dict] = None,
) -> str:
    """Generate HTML email body for the feedback report."""

    def _bullets(items):
        return "".join(f"<li>{item}</li>" for item in items)

    ab_rows = ""
    for v in ("A", "B"):
        s = ab_stats[v]
        rate = f"{s['success_rate']:.0%}" if s.get("success_rate") is not None else "N/A"
        ab_rows += f"<tr><td>Variant {v}</td><td>{s['total']}</td><td>{s['success']}</td><td>{s['failure']}</td><td>{rate}</td></tr>"
    iteration_section = ""
    if analysis.iteration_learnings:
        iteration_section = f"""
<h3>Manual Notion Iterations</h3>
<p>{n_iterations} unprocessed iteration(s) from the Notion page were folded into the learnings file.</p>
<ul>{_bullets(analysis.iteration_learnings)}</ul>
"""
    campaign_rows = ""
    if campaign_stats:
        for campaign, s in sorted(campaign_stats.items()):
            rate = f"{s['success_rate']:.0%}" if s.get("success_rate") is not None else "N/A"
            campaign_rows += (
                f"<tr><td>{campaign}</td><td>{s['total']}</td><td>{s['success']}</td>"
                f"<td>{s['failure']}</td><td>{s['skip']}</td><td>{rate}</td>"
                f"<td>{s.get('ab_winner', 'No Data')}</td></tr>"
            )
    campaign_section = ""
    if campaign_rows:
        campaign_section = f"""
<h3>Campaign Performance</h3>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%;">
<tr style="background: #f5f5f5;"><th>Campaign</th><th>Total</th><th>Success</th><th>Failure</th><th>Pending</th><th>Rate</th><th>A/B</th></tr>
{campaign_rows}
</table>
"""

    return f"""<html><body style="font-family: -apple-system, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px;">
<h2>TUM Social AI — Weekly Outreach Feedback</h2>
<p style="color: #666;">{datetime.now().strftime('%Y-%m-%d')} | {n_success} successes, {n_failure} failures, {n_skip} pending</p>

<h3>A/B Test Results</h3>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%;">
<tr style="background: #f5f5f5;"><th>Variant</th><th>Total</th><th>Success</th><th>Failure</th><th>Rate</th></tr>
{ab_rows}
</table>
<p><strong>Winner: {analysis.ab_winner}</strong> (confidence: {analysis.ab_confidence})</p>
<p>{analysis.ab_interpretation}</p>

{campaign_section}

<h3>Winning Patterns</h3>
<ul>{_bullets(analysis.winning_patterns)}</ul>

<h3>Losing Patterns</h3>
<ul>{_bullets(analysis.losing_patterns)}</ul>

<h3>Recommendations</h3>
<ul>{_bullets(analysis.recommendations)}</ul>

{iteration_section}

<h3>Tone & Style</h3>
<p>{analysis.tone_observations}</p>

<hr style="margin-top: 30px; border: none; border-top: 1px solid #ddd;">
<p style="color: #999; font-size: 12px;">Auto-generated by TUM Social AI Feedback Agent</p>
</body></html>"""


def send_feedback_email(
    analysis: FeedbackAnalysis,
    ab_stats: dict,
    n_success: int,
    n_failure: int,
    n_skip: int,
    n_iterations: int = 0,
    campaign_stats: Optional[dict] = None,
) -> bool:
    """Send feedback report email."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        console.print("[yellow]Email not configured (GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing). Skipping email.[/yellow]")
        return False

    # Determine recipients
    if FEEDBACK_REPORT_RECIPIENTS:
        recipients = [r.strip() for r in FEEDBACK_REPORT_RECIPIENTS.split(",") if r.strip()]
    elif REPORT_RECIPIENT_EMAIL:
        recipients = [REPORT_RECIPIENT_EMAIL]
    else:
        recipients = [GMAIL_ADDRESS]

    subject = f"TUM Social AI — Weekly Outreach Feedback: {n_success} successes, {n_failure} failures (A/B winner: {analysis.ab_winner})"

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    html_body = _generate_feedback_email_html(
        analysis,
        ab_stats,
        n_success,
        n_failure,
        n_skip,
        n_iterations,
        campaign_stats=campaign_stats,
    )
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        console.print(f"[green]Feedback report emailed to {', '.join(recipients)}[/green]")
        return True
    except Exception as e:
        console.print(f"[red]Email sending failed: {e}[/red]")
        return False


# =============================================================================
# Main runner
# =============================================================================

def run_feedback(dry_run: bool = False, min_data: int = 10):
    """
    Run the weekly feedback analysis.

    Args:
        dry_run: If True, analyze but don't write learnings or send email.
        min_data: Minimum resolved outcomes (success + failure) required to run GPT-4o analysis.
    """
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Social AI — Feedback Agent[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    if dry_run:
        console.print("[yellow]DRY RUN — learnings will NOT be written, email will NOT be sent[/yellow]")
    console.print("=" * 60)

    # Validate
    if not OPENAI_API_KEY:
        console.print("[red]Error: OPENAI_API_KEY not configured[/red]")
        return
    if not NOTION_TOKEN or not NOTION_DB_ACCOUNTS_ID:
        console.print("[red]Error: NOTION_TOKEN or NOTION_DB_ACCOUNTS_ID not configured[/red]")
        return

    # Step 1: Load manual copywriter iterations from Notion.
    console.print("\n[cyan]Scanning Notion copywriter iterations page...[/cyan]")
    iterations_injection, iterations_block_ids = load_iterations()
    n_iterations = len(iterations_block_ids)
    if n_iterations:
        console.print(f"[cyan]Found {n_iterations} unprocessed copywriter iteration(s).[/cyan]")
    else:
        console.print("[dim]No unprocessed copywriter iterations found.[/dim]")

    # Step 2: Fetch all contacts with outreach messages
    console.print("\n[cyan]Fetching contacts with outreach messages...[/cyan]")
    contacts = fetch_contacts_with_outreach(NOTION_DB_CONTACTS_ID)

    if not contacts:
        console.print("[yellow]No contacts with outreach messages found.[/yellow]")
        if not iterations_injection:
            console.print("[yellow]No manual iterations found either. Nothing to analyze.[/yellow]")
            return

    console.print(f"[cyan]Found {len(contacts)} campaign-scoped outreach records[/cyan]")

    # Step 3: Classify outcomes
    successes = []
    failures = []
    skips = []

    for c in contacts:
        outcome = classify_outcome(c)
        c["outcome"] = outcome
        if outcome == "success":
            successes.append(c)
        elif outcome == "failure":
            failures.append(c)
        else:
            skips.append(c)

    # Display classification summary
    table = Table(title="Outcome Classification")
    table.add_column("Category", style="cyan")
    table.add_column("Count", style="green")
    table.add_row("Success (Engaged+)", str(len(successes)))
    table.add_row("Failure (Unqualified/Stale)", str(len(failures)))
    table.add_row("Skip (Too early)", str(len(skips)))
    table.add_row("Total", str(len(contacts)))
    console.print(table)

    # Step 4: Compute A/B stats
    ab_stats = compute_ab_stats(contacts)

    ab_table = Table(title="A/B Test Distribution")
    ab_table.add_column("Variant", style="cyan")
    ab_table.add_column("Total")
    ab_table.add_column("Success", style="green")
    ab_table.add_column("Failure", style="red")
    ab_table.add_column("Rate")
    for v in ("A", "B", "none"):
        s = ab_stats[v]
        rate = f"{s['success_rate']:.0%}" if s.get("success_rate") is not None else "N/A"
        label = f"Variant {v}" if v != "none" else "No variant (pre-AB)"
        ab_table.add_row(label, str(s["total"]), str(s["success"]), str(s["failure"]), rate)
    console.print(ab_table)

    campaign_stats = compute_campaign_stats(contacts)
    campaign_table = Table(title="Campaign Performance")
    campaign_table.add_column("Campaign", style="cyan")
    campaign_table.add_column("Total", justify="right")
    campaign_table.add_column("Success", style="green", justify="right")
    campaign_table.add_column("Failure", style="red", justify="right")
    campaign_table.add_column("Pending", justify="right")
    campaign_table.add_column("A/B")
    campaign_table.add_column("Account-level", justify="right")
    for campaign, s in sorted(campaign_stats.items()):
        campaign_table.add_row(
            campaign,
            str(s["total"]),
            str(s["success"]),
            str(s["failure"]),
            str(s["skip"]),
            s.get("ab_winner", "No Data"),
            str(s.get("account_level", 0)),
        )
    console.print(campaign_table)

    if not dry_run:
        console.print("\n[cyan]Syncing Campaign Tracker with latest outcome and A/B stats...[/cyan]")
        sync_campaign_tracker()

    # Step 5: Check minimum data threshold
    resolved = len(successes) + len(failures)
    if resolved < min_data and not iterations_injection:
        console.print(f"\n[yellow]Only {resolved} resolved outcomes (need {min_data}). Skipping GPT-4o analysis.[/yellow]")
        console.print("[dim]Re-run with --min-data to lower the threshold.[/dim]")
        return
    if resolved < min_data and iterations_injection:
        console.print(
            f"\n[yellow]Only {resolved} resolved outcomes (need {min_data}), "
            "but manual Notion iterations are present, so feedback analysis will still run.[/yellow]"
        )

    # Step 6: Run GPT-4o analysis
    console.print(f"\n[cyan]Running GPT-4o pattern analysis ({len(successes)} successes, {len(failures)} failures)...[/cyan]")
    client = OpenAI(api_key=OPENAI_API_KEY)

    analysis = run_gpt_analysis(
        successes,
        failures,
        ab_stats,
        client,
        iterations_injection,
        campaign_stats=campaign_stats,
    )
    if not analysis:
        console.print("[red]GPT-4o analysis failed. Aborting.[/red]")
        return

    # Display results
    console.print(f"\n[bold green]A/B Winner: {analysis.ab_winner}[/bold green] (confidence: {analysis.ab_confidence})")
    console.print(f"[dim]{analysis.ab_interpretation}[/dim]")

    console.print("\n[bold]Winning Patterns:[/bold]")
    for p in analysis.winning_patterns:
        console.print(f"  + {p}")

    console.print("\n[bold]Losing Patterns:[/bold]")
    for p in analysis.losing_patterns:
        console.print(f"  - {p}")

    console.print("\n[bold]Recommendations:[/bold]")
    for r in analysis.recommendations:
        console.print(f"  > {r}")

    if analysis.iteration_learnings:
        console.print("\n[bold]Manual Notion Iterations:[/bold]")
        for item in analysis.iteration_learnings:
            console.print(f"  * {item}")

    console.print(f"\n[bold]Tone:[/bold] {analysis.tone_observations}")

    # Step 7: Write learnings file
    if not dry_run:
        write_learnings_file(
            analysis,
            ab_stats,
            len(successes),
            len(failures),
            n_iterations,
            campaign_stats=campaign_stats,
        )

        if iterations_block_ids:
            console.print("\n[cyan]Marking Notion iterations as processed...[/cyan]")
            mark_iterations_processed(iterations_block_ids)

        # Step 8: Send email report
        send_feedback_email(
            analysis,
            ab_stats,
            len(successes),
            len(failures),
            len(skips),
            n_iterations,
            campaign_stats=campaign_stats,
        )
    else:
        console.print("\n[yellow]Dry run — skipped writing learnings, marking iterations processed, and sending email[/yellow]")

    console.print("\n[bold green]Feedback Agent finished.[/bold green]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weekly outreach feedback analysis and A/B test evaluation")
    parser.add_argument("--dry-run", action="store_true", help="Analyze without writing learnings or sending email")
    parser.add_argument("--min-data", type=int, default=10, help="Minimum resolved outcomes to run analysis (default: 10)")
    args = parser.parse_args()

    run_feedback(dry_run=args.dry_run, min_data=args.min_data)
