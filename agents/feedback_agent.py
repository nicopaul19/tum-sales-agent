"""
Feedback Agent — Monthly outreach effectiveness analysis and A/B test evaluation.

Analyzes contacts with outreach messages, classifies outcomes as success/failure/skip,
computes A/B variant statistics, runs GPT-4o pattern analysis, writes learnings to
data/prompts/outreach_learnings.md, and sends an HTML summary email.

Usage:
    python -m agents.feedback_agent                    # full run
    python -m agents.feedback_agent --dry-run          # analyze without writing learnings or sending email
    python -m agents.feedback_agent --min-data 5       # override minimum resolved outcomes (default: 10)
"""
import sys
import smtplib
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

import requests as http_requests
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
    NOTION_DB_CONTACTS_ID,
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    REPORT_RECIPIENT_EMAIL,
)
from utils.api_logger import log_api_usage
from utils.notion_client import _notion_api_headers
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


# =============================================================================
# Notion data fetching
# =============================================================================

def fetch_contacts_with_outreach(contacts_db_id: str) -> List[dict]:
    """
    Fetch all contacts that have outreach messages (LinkedIn 1st Cold is not empty).

    Returns list of dicts with contact fields, linked account status, and outreach text.
    """
    headers = _notion_api_headers()
    url = f"https://api.notion.com/v1/databases/{contacts_db_id}/query"

    query_filter = {
        "property": "LinkedIn 1st Cold",
        "rich_text": {"is_not_empty": True}
    }

    results = []
    has_more = True
    start_cursor = None

    while has_more:
        body = {"page_size": 100, "filter": query_filter}
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = http_requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            console.print(f"[red]Notion query error: {resp.status_code} - {resp.json().get('message', '')}[/red]")
            return []

        data = resp.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    contacts = []
    for page in results:
        props = page.get("properties", {})

        # Contact name
        contact_name = ""
        for pname, pdata in props.items():
            if pdata.get("type") == "title":
                titles = pdata.get("title", [])
                if titles:
                    contact_name = titles[0].get("plain_text", "")
                break

        # Outreach messages
        def _rich_text(prop_name):
            p = props.get(prop_name, {})
            if p.get("type") == "rich_text" and p.get("rich_text"):
                return p["rich_text"][0].get("plain_text", "")
            return ""

        linkedin_first = _rich_text("LinkedIn 1st Cold")
        linkedin_fu = _rich_text("LinkedIn FU message")
        email_body = _rich_text("Cold Email Body")
        email_subject = _rich_text("Cold Email Subject")

        # AB Variant
        ab_variant = ""
        ab_prop = props.get("AB Variant", {})
        if ab_prop.get("type") == "select" and ab_prop.get("select"):
            ab_variant = ab_prop["select"].get("name", "")

        # Account status from rollup
        account_status = ""
        status_rollup = props.get("Account Status", {})
        if status_rollup.get("type") == "rollup":
            status_array = status_rollup.get("rollup", {}).get("array", [])
            for item in status_array:
                if item.get("type") == "status" and item.get("status"):
                    account_status = item["status"].get("name", "")

        # Account last_edited_time from relation
        account_page_id = ""
        account_rel = props.get("Accounts", {})
        if account_rel.get("type") == "relation":
            relations = account_rel.get("relation", [])
            if relations:
                account_page_id = relations[0].get("id", "")

        # Company name from account (via a quick fetch if needed)
        company_name = ""
        account_last_edited = ""
        if account_page_id:
            account_data = _fetch_account_basics(account_page_id, headers)
            company_name = account_data.get("company_name", "")
            account_last_edited = account_data.get("last_edited_time", "")

        contacts.append({
            "contact_name": contact_name,
            "company_name": company_name,
            "account_status": account_status,
            "account_last_edited": account_last_edited,
            "ab_variant": ab_variant,
            "linkedin_first": linkedin_first,
            "linkedin_fu": linkedin_fu,
            "email_body": email_body,
            "email_subject": email_subject,
        })

    return contacts


def _fetch_account_basics(account_page_id: str, headers: dict) -> dict:
    """Fetch basic account fields (name, last_edited_time, status)."""
    try:
        resp = http_requests.get(
            f"https://api.notion.com/v1/pages/{account_page_id}",
            headers=headers
        )
        if resp.status_code != 200:
            return {}

        page = resp.json()
        props = page.get("properties", {})

        # Title
        company_name = ""
        for pdata in props.values():
            if pdata.get("type") == "title":
                titles = pdata.get("title", [])
                if titles:
                    company_name = titles[0].get("plain_text", "")
                break

        return {
            "company_name": company_name,
            "last_edited_time": page.get("last_edited_time", ""),
        }

    except Exception:
        return {}


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

    # FAILURE: stuck at Contacted LinkedIn/Email for > STALE_DAYS
    if status in ("Contacted LinkedIn \U0001f310", "Contacted Mail \U0001f4e9"):
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


# =============================================================================
# GPT-4o analysis
# =============================================================================

def run_gpt_analysis(
    successes: List[dict],
    failures: List[dict],
    ab_stats: dict,
    client: OpenAI,
) -> Optional[FeedbackAnalysis]:
    """Run GPT-4o pattern analysis on success/failure message pairs."""

    def _format_contact(c: dict) -> str:
        lines = [f"Contact: {c['contact_name']} @ {c['company_name']}"]
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

    prompt = f"""You are analyzing outreach message effectiveness for TUM Social AI, a student AI-for-Good initiative at TUM.

## SUCCESSFUL OUTREACH (led to engagement, meetings, or partnerships)
{success_text}

## FAILED OUTREACH (prospect unqualified or no response after 30+ days)
{failure_text}

## A/B TEST DATA
Variant A framing: "We're developing AI solutions with partners like UN Women"
Variant B framing: "We're multiplying the impact of our social partners like UN Women in over 50 countries through custom AI tools"

{ab_summary}

## YOUR TASK
1. Identify patterns that distinguish successful vs. failed outreach messages
2. Provide actionable recommendations for improving future messages
3. Analyze the A/B test results and determine which framing performs better
4. Note any tone or style observations

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
            {"successes": len(successes), "failures": len(failures)}
        )

        return result

    except Exception as e:
        console.print(f"[red]GPT-4o analysis error: {e}[/red]")
        return None


# =============================================================================
# Learnings file writer
# =============================================================================

def write_learnings_file(analysis: FeedbackAnalysis, ab_stats: dict, n_success: int, n_failure: int):
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
) -> str:
    """Generate HTML email body for the feedback report."""

    def _bullets(items):
        return "".join(f"<li>{item}</li>" for item in items)

    ab_rows = ""
    for v in ("A", "B"):
        s = ab_stats[v]
        rate = f"{s['success_rate']:.0%}" if s.get("success_rate") is not None else "N/A"
        ab_rows += f"<tr><td>Variant {v}</td><td>{s['total']}</td><td>{s['success']}</td><td>{s['failure']}</td><td>{rate}</td></tr>"

    return f"""<html><body style="font-family: -apple-system, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px;">
<h2>TUM Social AI — Monthly Outreach Feedback</h2>
<p style="color: #666;">{datetime.now().strftime('%Y-%m-%d')} | {n_success} successes, {n_failure} failures, {n_skip} pending</p>

<h3>A/B Test Results</h3>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%;">
<tr style="background: #f5f5f5;"><th>Variant</th><th>Total</th><th>Success</th><th>Failure</th><th>Rate</th></tr>
{ab_rows}
</table>
<p><strong>Winner: {analysis.ab_winner}</strong> (confidence: {analysis.ab_confidence})</p>
<p>{analysis.ab_interpretation}</p>

<h3>Winning Patterns</h3>
<ul>{_bullets(analysis.winning_patterns)}</ul>

<h3>Losing Patterns</h3>
<ul>{_bullets(analysis.losing_patterns)}</ul>

<h3>Recommendations</h3>
<ul>{_bullets(analysis.recommendations)}</ul>

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

    subject = f"TUM Social AI — Monthly Outreach Feedback: {n_success} successes, {n_failure} failures (A/B winner: {analysis.ab_winner})"

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    html_body = _generate_feedback_email_html(analysis, ab_stats, n_success, n_failure, n_skip)
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
    Run the monthly feedback analysis.

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
    if not NOTION_TOKEN or not NOTION_DB_CONTACTS_ID:
        console.print("[red]Error: NOTION_TOKEN or NOTION_DB_CONTACTS_ID not configured[/red]")
        return

    # Step 1: Fetch all contacts with outreach messages
    console.print("\n[cyan]Fetching contacts with outreach messages...[/cyan]")
    contacts = fetch_contacts_with_outreach(NOTION_DB_CONTACTS_ID)

    if not contacts:
        console.print("[yellow]No contacts with outreach messages found. Nothing to analyze.[/yellow]")
        return

    console.print(f"[cyan]Found {len(contacts)} contacts with outreach messages[/cyan]")

    # Step 2: Classify outcomes
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

    # Step 3: Compute A/B stats
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

    # Step 4: Check minimum data threshold
    resolved = len(successes) + len(failures)
    if resolved < min_data:
        console.print(f"\n[yellow]Only {resolved} resolved outcomes (need {min_data}). Skipping GPT-4o analysis.[/yellow]")
        console.print("[dim]Re-run with --min-data to lower the threshold.[/dim]")
        return

    # Step 5: Run GPT-4o analysis
    console.print(f"\n[cyan]Running GPT-4o pattern analysis ({len(successes)} successes, {len(failures)} failures)...[/cyan]")
    client = OpenAI(api_key=OPENAI_API_KEY)

    analysis = run_gpt_analysis(successes, failures, ab_stats, client)
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

    console.print(f"\n[bold]Tone:[/bold] {analysis.tone_observations}")

    # Step 6: Write learnings file
    if not dry_run:
        write_learnings_file(analysis, ab_stats, len(successes), len(failures))

        # Step 7: Send email report
        send_feedback_email(analysis, ab_stats, len(successes), len(failures), len(skips))
    else:
        console.print("\n[yellow]Dry run — skipped writing learnings and sending email[/yellow]")

    console.print("\n[bold green]Feedback Agent finished.[/bold green]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monthly outreach feedback analysis and A/B test evaluation")
    parser.add_argument("--dry-run", action="store_true", help="Analyze without writing learnings or sending email")
    parser.add_argument("--min-data", type=int, default=10, help="Minimum resolved outcomes to run analysis (default: 10)")
    args = parser.parse_args()

    run_feedback(dry_run=args.dry_run, min_data=args.min_data)
