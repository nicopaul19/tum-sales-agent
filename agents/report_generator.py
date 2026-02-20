"""
LinkedIn Report Generator — PDF and email report for weekly outreach review.

Follows the SupervisorPDF pattern from agents/supervisor.py.
Generates a summary of outreach actions: new connections, follow-ups, ghosted.

Output: data/reports/weekly_linkedin_report_YYYYMMDD.pdf
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fpdf import FPDF

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import REPORTS_DIR


# =============================================================================
# Helper Functions
# =============================================================================

def strip_emojis(text: str) -> str:
    """Remove emojis and non-latin-1 characters for PDF encoding compatibility."""
    # Replace common Unicode chars with ASCII equivalents
    replacements = {
        '\u2014': '--',  # em dash
        '\u2013': '-',   # en dash
        '\u2019': "'",   # right single quote
        '\u2018': "'",   # left single quote
        '\u201c': '"',   # left double quote
        '\u201d': '"',   # right double quote
        '\u2026': '...', # ellipsis
        '→': '->',       # arrow
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    # Remove any remaining non-latin-1 characters (including emojis)
    return ''.join(c if ord(c) < 256 else '' for c in text)


# =============================================================================
# Action Item model (passed from linkedin_manager)
# =============================================================================

class ActionItem:
    """A single action taken or recommended by the linkedin manager."""

    def __init__(
        self,
        category: str,         # "follow_up", "new_connection", "ghosted"
        partner_name: str,
        profile_url: str = "",
        account_name: str = "",
        old_status: str = "",
        new_status: str = "",
        reasoning: str = "",
        draft_message: str = "",
        cold_message: str = "",
        fu_message: str = "",
    ):
        self.category = category
        self.partner_name = partner_name
        self.profile_url = profile_url
        self.account_name = account_name
        self.old_status = old_status
        self.new_status = new_status
        self.reasoning = reasoning
        self.draft_message = draft_message
        self.cold_message = cold_message
        self.fu_message = fu_message


# =============================================================================
# PDF Subclass
# =============================================================================

class LinkedInReportPDF(FPDF):
    """Custom PDF with header/footer for the LinkedIn outreach report."""

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 10, "TUM Social AI - Weekly Outreach Review", new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_font("Helvetica", "", 9)
        self.cell(0, 5, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x="LMARGIN", new_y="NEXT", align="C")
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    def section_title(self, title: str):
        self.set_font("Helvetica", "B", 12)
        self.set_fill_color(230, 230, 230)
        self.cell(0, 8, strip_emojis(title), new_x="LMARGIN", new_y="NEXT", fill=True)
        self.ln(2)

    def kv_line(self, key: str, value: str):
        self.set_font("Helvetica", "B", 10)
        self.cell(60, 6, strip_emojis(key), new_x="RIGHT")
        self.set_font("Helvetica", "", 10)
        self.cell(0, 6, strip_emojis(value), new_x="LMARGIN", new_y="NEXT")

    def body_text(self, text: str):
        self.set_font("Helvetica", "", 9)
        self.multi_cell(0, 5, strip_emojis(text))
        self.ln(1)

    def linked_name(self, name: str, url: str, description: str = ""):
        """Render a contact name as a clickable link with optional description."""
        # Name (clickable link)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(26, 115, 232)  # Link blue
        name_clean = strip_emojis(name)
        if url:
            self.cell(0, 6, name_clean, new_x="LMARGIN", new_y="NEXT", link=url)
        else:
            self.cell(0, 6, name_clean, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)  # Reset to black

        # Description
        if description:
            self.set_font("Helvetica", "", 9)
            self.multi_cell(0, 5, strip_emojis(description))
            self.ln(1)

    def italic_block(self, text: str):
        """Render text in italic (for draft messages)."""
        self.set_font("Helvetica", "I", 9)
        self.set_text_color(80, 80, 80)
        self.multi_cell(0, 5, strip_emojis(text))
        self.set_text_color(0, 0, 0)
        self.ln(1)


# =============================================================================
# PDF Generation
# =============================================================================

def generate_linkedin_report(actions: List[ActionItem], stats: Optional[dict] = None) -> Path:
    """
    Build the weekly LinkedIn report PDF.

    Args:
        actions: List of ActionItem objects from the linkedin manager
        stats: Optional summary stats dict

    Returns:
        Path to the generated PDF
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"weekly_linkedin_report_{datetime.now().strftime('%Y%m%d')}.pdf"
    pdf_path = REPORTS_DIR / filename

    pdf = LinkedInReportPDF()
    pdf.alias_nb_pages()
    pdf.add_page()

    # Categorize actions
    follow_ups = [a for a in actions if a.category == "follow_up"]
    new_connections = [a for a in actions if a.category == "new_connection"]
    ghosted = [a for a in actions if a.category == "ghosted"]

    # --- Summary Stats ---
    pdf.section_title("Summary")
    pdf.kv_line("Total actions:", str(len(actions)))
    pdf.kv_line("New connections:", str(len(new_connections)))
    pdf.kv_line("Follow-ups needed:", str(len(follow_ups)))
    pdf.kv_line("Ghosted/Unqualified:", str(len(ghosted)))

    if stats:
        pdf.ln(2)
        pdf.kv_line("Connections parsed:", str(stats.get("connections_parsed", 0)))
        pdf.kv_line("Contacts in Notion:", str(stats.get("contacts_in_notion", 0)))
        pdf.kv_line("Accounts checked:", str(stats.get("accounts_checked", 0)))
        pdf.kv_line("GPT-4o calls:", str(stats.get("gpt_calls", 0)))
    pdf.ln(4)

    # --- Section 1: New Connections ---
    pdf.section_title("1. New Connections")
    if new_connections:
        for item in new_connections:
            pdf.linked_name(
                name=item.partner_name,
                url=item.profile_url,
                description=f"{item.old_status} -> {item.new_status}",
            )
            if item.cold_message:
                pdf.body_text(f"1st Cold: {item.cold_message[:300]}")
    else:
        pdf.body_text("No new connections this week.")
    pdf.ln(2)

    # --- Section 2: Action Required (Follow-Ups) ---
    pdf.section_title("2. Action Required (Follow-Ups)")
    if follow_ups:
        for item in follow_ups:
            pdf.linked_name(
                name=item.partner_name,
                url=item.profile_url,
                description=f"Account: {item.account_name}" if item.account_name else "",
            )
            if item.cold_message:
                pdf.body_text(f"1st Cold: {item.cold_message[:300]}")
            if item.fu_message:
                pdf.body_text(f"FU on file: {item.fu_message[:300]}")
            if item.draft_message:
                pdf.italic_block(f'New draft: "{item.draft_message}"')
    else:
        pdf.body_text("No follow-ups needed this week.")
    pdf.ln(2)

    # --- Section 3: The Graveyard ---
    pdf.section_title("3. The Graveyard")
    if ghosted:
        for item in ghosted:
            pdf.linked_name(
                name=item.partner_name,
                url=item.profile_url,
                description=item.reasoning or f"{item.old_status} -> {item.new_status}",
            )
    else:
        pdf.body_text("No ghosted contacts this week.")

    pdf.output(str(pdf_path))
    return pdf_path


# =============================================================================
# Email HTML Body Generation
# =============================================================================

def generate_email_html(actions: List[ActionItem], stats: Optional[dict] = None) -> str:
    """Generate an HTML email body summarizing the outreach actions."""
    follow_ups = [a for a in actions if a.category == "follow_up"]
    new_connections = [a for a in actions if a.category == "new_connection"]
    ghosted = [a for a in actions if a.category == "ghosted"]

    lines = []
    lines.append("<html><body style='font-family: -apple-system, Arial, sans-serif; max-width: 600px;'>")
    lines.append("<h2 style='color: #1a73e8;'>TUM Social AI — Weekly Outreach Review</h2>")
    lines.append(f"<p style='color: #666;'>{datetime.now().strftime('%Y-%m-%d %H:%M')}</p>")

    # Summary
    lines.append("<table style='border-collapse:collapse; margin: 16px 0;'>")
    for label, count in [
        ("New connections", len(new_connections)),
        ("Follow-ups needed", len(follow_ups)),
        ("Ghosted", len(ghosted)),
    ]:
        color = "#4caf50" if "connection" in label else "#ff9800" if "Follow" in label else "#f44336"
        lines.append(
            f"<tr><td style='padding:4px 12px 4px 0; font-weight:bold;'>{label}:</td>"
            f"<td style='padding:4px 0;'><span style='background:{color}; color:white; "
            f"padding:2px 8px; border-radius:12px;'>{count}</span></td></tr>"
        )
    lines.append("</table>")

    # New Connections
    if new_connections:
        lines.append("<h3 style='color:#4caf50; border-bottom:1px solid #eee; padding-bottom:4px;'>New Connections</h3>")
        for item in new_connections:
            lines.append(f"<p><a href='{item.profile_url}' style='color:#1a73e8; font-weight:bold;'>{item.partner_name}</a>")
            lines.append(f"<br><span style='color:#666;'>{item.old_status} &rarr; {item.new_status}</span></p>")
            if item.cold_message:
                lines.append(f"<p style='color:#888; font-size:0.9em;'>1st Cold: {item.cold_message[:200]}</p>")

    # Follow-Ups
    if follow_ups:
        lines.append("<h3 style='color:#ff9800; border-bottom:1px solid #eee; padding-bottom:4px;'>Action Required — Follow-Ups</h3>")
        for item in follow_ups:
            lines.append(f"<p><a href='{item.profile_url}' style='color:#1a73e8; font-weight:bold;'>{item.partner_name}</a>")
            if item.account_name:
                lines.append(f" <span style='color:#666;'>({item.account_name})</span>")
            lines.append(f"<br><span style='color:#888;'>{item.reasoning}</span></p>")
            if item.cold_message:
                lines.append(f"<p style='font-size:0.9em; color:#555;'>1st Cold: <em>{item.cold_message[:200]}</em></p>")
            if item.fu_message:
                lines.append(f"<p style='font-size:0.9em; color:#555;'>FU on file: <em>{item.fu_message[:200]}</em></p>")
            if item.draft_message:
                lines.append(
                    f"<div style='background:#fff3e0; border-left:3px solid #ff9800; padding:8px 12px; margin:8px 0;'>"
                    f"<strong>New draft:</strong> {item.draft_message}</div>"
                )

    # Ghosted
    if ghosted:
        lines.append("<h3 style='color:#f44336; border-bottom:1px solid #eee; padding-bottom:4px;'>The Graveyard</h3>")
        for item in ghosted:
            lines.append(f"<p><a href='{item.profile_url}' style='color:#1a73e8;'>{item.partner_name}</a>")
            lines.append(f"<br><span style='color:#888;'>{item.reasoning}</span></p>")

    # No actions
    if not actions:
        lines.append("<p style='color:#888;'>No outreach actions this week. All clear!</p>")

    lines.append("<hr style='border:none; border-top:1px solid #eee; margin-top:24px;'>")
    lines.append("<p style='color:#999; font-size:0.8em;'>Generated by TUM Social AI — LinkedIn Agent</p>")
    lines.append("</body></html>")

    return "\n".join(lines)
