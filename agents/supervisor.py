"""
Supervisor Agent - Weekly PDF report with API usage, safety audit, and pipeline overview.

Runs every Saturday at 09:00 via launchd.
Outputs a PDF to data/reports/supervisor_report_YYYYMMDD.pdf

Usage:
    python -m agents.supervisor
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from fpdf import FPDF
from rich.console import Console

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import (
    PROJECT_ROOT,
    DATA_DIR,
    LOGS_DIR,
    TABLES_DIR,
    REPORTS_DIR,
    API_USAGE_LOG,
    MASTER_CSV,
    QUALIFIED_CSV,
    BACKLOG_CSV,
    MASTER_CSV_HEADERS,
)

console = Console()

# GPT-4o pricing (per 1M tokens)
PRICING = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
}
DEFAULT_PRICING = {"input": 2.50, "output": 10.00}

HASHES_FILE = LOGS_DIR / "file_hashes.json"


# =============================================================================
# Section 1: API Usage Report
# =============================================================================

def api_usage_report(since: datetime) -> dict:
    """
    Parse api_usage.jsonl for records since `since` and compute totals.

    Returns dict with:
        total_calls, prompt_tokens, completion_tokens, total_tokens,
        estimated_cost_usd, by_agent (dict), by_action (dict)
    """
    result = {
        "total_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "by_agent": defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}),
        "by_action": defaultdict(lambda: {"calls": 0, "total_tokens": 0}),
    }

    if not API_USAGE_LOG.exists():
        return result

    with open(API_USAGE_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = datetime.fromisoformat(record["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < since:
                continue

            pt = record.get("prompt_tokens", 0)
            ct = record.get("completion_tokens", 0)
            tt = record.get("total_tokens", 0)
            model = record.get("model", "gpt-4o")
            prices = PRICING.get(model, DEFAULT_PRICING)
            cost = (pt / 1_000_000) * prices["input"] + (ct / 1_000_000) * prices["output"]

            result["total_calls"] += 1
            result["prompt_tokens"] += pt
            result["completion_tokens"] += ct
            result["total_tokens"] += tt
            result["estimated_cost_usd"] += cost

            agent = record.get("agent", "unknown")
            result["by_agent"][agent]["calls"] += 1
            result["by_agent"][agent]["prompt_tokens"] += pt
            result["by_agent"][agent]["completion_tokens"] += ct
            result["by_agent"][agent]["cost"] += cost

            action = record.get("action", "unknown")
            result["by_action"][action]["calls"] += 1
            result["by_action"][action]["total_tokens"] += tt

    # Convert defaultdicts to plain dicts for cleaner output
    result["by_agent"] = dict(result["by_agent"])
    result["by_action"] = dict(result["by_action"])

    return result


# =============================================================================
# Section 2: Safety Audit
# =============================================================================

def _hash_file(path: Path) -> str:
    """SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def safety_audit() -> dict:
    """
    Run safety checks:
    1. File integrity (.env, config.py) via SHA-256 hashes
    2. Scan logs for paths outside project root
    3. Verify CSV column integrity
    4. Count log errors

    Returns dict with findings.
    """
    findings = {
        "integrity_ok": True,
        "integrity_details": [],
        "suspicious_paths": [],
        "csv_integrity_ok": True,
        "csv_issues": [],
        "log_errors": 0,
    }

    # --- 1. File integrity ---
    watched_files = [
        PROJECT_ROOT / ".env",
        PROJECT_ROOT / "utils" / "config.py",
    ]

    current_hashes = {}
    for fp in watched_files:
        if fp.exists():
            current_hashes[str(fp)] = _hash_file(fp)

    if HASHES_FILE.exists():
        stored = json.loads(HASHES_FILE.read_text())
        for path_str, old_hash in stored.items():
            new_hash = current_hashes.get(path_str)
            if new_hash and new_hash != old_hash:
                findings["integrity_ok"] = False
                findings["integrity_details"].append(f"CHANGED: {Path(path_str).name}")
            elif new_hash is None:
                findings["integrity_details"].append(f"MISSING: {Path(path_str).name}")
    else:
        findings["integrity_details"].append("First run - baseline hashes stored")

    # Update stored hashes
    HASHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    HASHES_FILE.write_text(json.dumps(current_hashes, indent=2))

    # --- 2. Scan logs for paths outside project root ---
    project_root_str = str(PROJECT_ROOT)
    log_files = list(LOGS_DIR.glob("*.log")) if LOGS_DIR.exists() else []
    error_count = 0

    for log_file in log_files:
        try:
            content = log_file.read_text(errors="replace")
            for i, line in enumerate(content.split("\n"), 1):
                # Check for absolute paths that aren't within project
                if "/" in line:
                    # Look for paths like /Users/... /tmp/... /etc/...
                    import re
                    paths_found = re.findall(r'(/(?:Users|tmp|etc|var|opt|home)[/\w.-]+)', line)
                    for p in paths_found:
                        if not p.startswith(project_root_str):
                            findings["suspicious_paths"].append(f"{log_file.name}:{i}: {p[:80]}")

                # Count errors
                if "error" in line.lower() or "traceback" in line.lower():
                    error_count += 1
        except Exception:
            pass

    findings["log_errors"] = error_count
    # Cap suspicious paths for report readability
    findings["suspicious_paths"] = findings["suspicious_paths"][:20]

    # --- 3. CSV column integrity ---
    if MASTER_CSV.exists():
        try:
            df = pd.read_csv(MASTER_CSV, nrows=0)
            actual_cols = list(df.columns)
            if actual_cols != MASTER_CSV_HEADERS:
                findings["csv_integrity_ok"] = False
                missing = set(MASTER_CSV_HEADERS) - set(actual_cols)
                extra = set(actual_cols) - set(MASTER_CSV_HEADERS)
                if missing:
                    findings["csv_issues"].append(f"Missing columns: {', '.join(missing)}")
                if extra:
                    findings["csv_issues"].append(f"Extra columns: {', '.join(extra)}")
        except Exception as e:
            findings["csv_integrity_ok"] = False
            findings["csv_issues"].append(f"Read error: {e}")
    else:
        findings["csv_issues"].append("master_input.csv not found")

    return findings


# =============================================================================
# Section 3: Lead Pipeline Report
# =============================================================================

def lead_pipeline_report() -> dict:
    """
    Compute lead funnel counts and source breakdown.

    Returns dict with:
        total, pending, qualified, backlog, filtered,
        source_breakdown (dict), this_week_added, all_time
    """
    result = {
        "total": 0,
        "pending": 0,
        "qualified": 0,
        "backlog": 0,
        "filtered": 0,
        "source_breakdown": defaultdict(int),
        "this_week_added": 0,
    }

    if not MASTER_CSV.exists():
        return result

    df = pd.read_csv(MASTER_CSV)
    result["total"] = len(df)

    status_counts = df["status"].value_counts().to_dict()
    result["pending"] = status_counts.get("pending", 0)
    result["qualified"] = status_counts.get("qualified_for_apollo", 0)
    result["backlog"] = status_counts.get("backlog", 0)
    result["filtered"] = status_counts.get("filtered_out", 0)

    # Source breakdown
    for source, count in df["source"].value_counts().items():
        result["source_breakdown"][source] = count
    result["source_breakdown"] = dict(result["source_breakdown"])

    # This week's additions
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())  # Monday
    if "date_added" in df.columns:
        df["_date"] = pd.to_datetime(df["date_added"], errors="coerce").dt.date
        result["this_week_added"] = int((df["_date"] >= week_start).sum())

    return result


# =============================================================================
# Section 4: Chart Generation
# =============================================================================

def generate_chart(output_path: Path) -> bool:
    """
    Create a stacked bar chart: manual vs apollo leads per ISO week.
    Saves PNG to output_path.

    Returns True on success.
    """
    if not MASTER_CSV.exists():
        return False

    df = pd.read_csv(MASTER_CSV)
    if df.empty or "date_added" not in df.columns:
        return False

    df["_date"] = pd.to_datetime(df["date_added"], errors="coerce")
    df = df.dropna(subset=["_date"])
    if df.empty:
        return False

    df["_week"] = df["_date"].dt.isocalendar().week.astype(int)
    df["_year"] = df["_date"].dt.isocalendar().year.astype(int)
    df["_yw"] = df["_year"].astype(str) + "-W" + df["_week"].astype(str).str.zfill(2)

    # Classify sources
    df["_source_group"] = df["source"].apply(
        lambda s: "apollo" if str(s) == "apollo" else "manual"
    )

    pivot = df.pivot_table(index="_yw", columns="_source_group", values="_date", aggfunc="count", fill_value=0)
    pivot = pivot.sort_index()

    # Keep last 12 weeks max for readability
    pivot = pivot.tail(12)

    fig, ax = plt.subplots(figsize=(8, 4))

    manual_vals = pivot["manual"].values if "manual" in pivot.columns else [0] * len(pivot)
    apollo_vals = pivot["apollo"].values if "apollo" in pivot.columns else [0] * len(pivot)
    weeks = pivot.index.tolist()
    x = range(len(weeks))

    ax.bar(x, manual_vals, label="Manual", color="#2bbcb3")  # teal
    ax.bar(x, apollo_vals, bottom=manual_vals, label="Apollo", color="#f0883e")  # orange

    ax.set_xticks(x)
    ax.set_xticklabels(weeks, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Leads added")
    ax.set_title("Weekly Lead Intake: Manual vs Apollo")
    ax.legend()
    fig.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return True


# =============================================================================
# Section 5: PDF Generation
# =============================================================================

class SupervisorPDF(FPDF):
    """Custom PDF with header/footer for the supervisor report."""

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 10, "TUM Social AI - Supervisor Report", new_x="LMARGIN", new_y="NEXT", align="C")
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
        self.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT", fill=True)
        self.ln(2)

    def kv_line(self, key: str, value: str):
        self.set_font("Helvetica", "B", 10)
        self.cell(60, 6, key, new_x="RIGHT")
        self.set_font("Helvetica", "", 10)
        self.cell(0, 6, value, new_x="LMARGIN", new_y="NEXT")

    def body_text(self, text: str):
        self.set_font("Helvetica", "", 9)
        self.multi_cell(0, 5, text)
        self.ln(1)


def generate_pdf_report(api_data: dict, safety_data: dict, pipeline_data: dict, chart_path: Path | None) -> Path:
    """
    Build the PDF with all four report sections.

    Returns the path to the generated PDF.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"supervisor_report_{datetime.now().strftime('%Y%m%d')}.pdf"
    pdf_path = REPORTS_DIR / filename

    pdf = SupervisorPDF()
    pdf.alias_nb_pages()
    pdf.add_page()

    # --- Section 1: API Usage ---
    pdf.section_title("1. API Usage (This Week)")
    pdf.kv_line("Total API calls:", str(api_data["total_calls"]))
    pdf.kv_line("Prompt tokens:", f"{api_data['prompt_tokens']:,}")
    pdf.kv_line("Completion tokens:", f"{api_data['completion_tokens']:,}")
    pdf.kv_line("Total tokens:", f"{api_data['total_tokens']:,}")
    pdf.kv_line("Estimated cost:", f"${api_data['estimated_cost_usd']:.4f}")
    pdf.ln(2)

    if api_data["by_agent"]:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Breakdown by agent:", new_x="LMARGIN", new_y="NEXT")
        for agent, stats in api_data["by_agent"].items():
            pdf.kv_line(f"  {agent}:", f"{stats['calls']} calls, {stats['prompt_tokens']+stats['completion_tokens']:,} tokens, ${stats['cost']:.4f}")
    pdf.ln(2)

    if api_data["by_action"]:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Breakdown by action:", new_x="LMARGIN", new_y="NEXT")
        for action, stats in api_data["by_action"].items():
            pdf.kv_line(f"  {action}:", f"{stats['calls']} calls, {stats['total_tokens']:,} tokens")
    pdf.ln(4)

    # --- Section 2: Safety Audit ---
    pdf.section_title("2. Safety Audit")
    integrity_status = "PASS" if safety_data["integrity_ok"] else "CHANGED"
    pdf.kv_line("File integrity:", integrity_status)
    for detail in safety_data["integrity_details"]:
        pdf.body_text(f"  - {detail}")

    csv_status = "PASS" if safety_data["csv_integrity_ok"] else "FAIL"
    pdf.kv_line("CSV schema:", csv_status)
    for issue in safety_data["csv_issues"]:
        pdf.body_text(f"  - {issue}")

    pdf.kv_line("Log errors found:", str(safety_data["log_errors"]))

    if safety_data["suspicious_paths"]:
        pdf.kv_line("Suspicious paths:", str(len(safety_data["suspicious_paths"])))
        for sp in safety_data["suspicious_paths"][:10]:
            pdf.body_text(f"  - {sp}")
    else:
        pdf.kv_line("Suspicious paths:", "0")
    pdf.ln(4)

    # --- Section 3: Lead Pipeline ---
    pdf.section_title("3. Lead Pipeline")
    pdf.kv_line("Total leads (all time):", str(pipeline_data["total"]))
    pdf.kv_line("Added this week:", str(pipeline_data["this_week_added"]))
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Funnel:", new_x="LMARGIN", new_y="NEXT")
    pdf.kv_line("  Pending:", str(pipeline_data["pending"]))
    pdf.kv_line("  Qualified:", str(pipeline_data["qualified"]))
    pdf.kv_line("  Backlog:", str(pipeline_data["backlog"]))
    pdf.kv_line("  Filtered out:", str(pipeline_data["filtered"]))
    pdf.ln(2)

    if pipeline_data["source_breakdown"]:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Source breakdown:", new_x="LMARGIN", new_y="NEXT")
        for source, count in pipeline_data["source_breakdown"].items():
            pdf.kv_line(f"  {source}:", str(count))
    pdf.ln(4)

    # --- Section 4: Chart ---
    if chart_path and chart_path.exists():
        pdf.section_title("4. Weekly Lead Intake Chart")
        # Calculate width to fit within margins
        pdf.image(str(chart_path), x=15, w=180)

    pdf.output(str(pdf_path))
    return pdf_path


# =============================================================================
# Main Orchestrator
# =============================================================================

def run_supervisor():
    """Run the full supervisor report pipeline."""
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Social AI - Supervisor Agent[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    console.print("=" * 60)

    # Time window: this ISO week (Monday 00:00 UTC to now)
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday(), hours=now.hour, minutes=now.minute, seconds=now.second)

    # 1. API Usage
    console.print("\n[cyan]Collecting API usage data...[/cyan]")
    api_data = api_usage_report(since=week_start)
    console.print(f"  {api_data['total_calls']} calls, {api_data['total_tokens']:,} tokens, ${api_data['estimated_cost_usd']:.4f}")

    # 2. Safety Audit
    console.print("[cyan]Running safety audit...[/cyan]")
    safety_data = safety_audit()
    status = "PASS" if safety_data["integrity_ok"] and safety_data["csv_integrity_ok"] else "ISSUES FOUND"
    console.print(f"  Status: {status}, {safety_data['log_errors']} log errors")

    # 3. Lead Pipeline
    console.print("[cyan]Analyzing lead pipeline...[/cyan]")
    pipeline_data = lead_pipeline_report()
    console.print(f"  {pipeline_data['total']} total leads, {pipeline_data['this_week_added']} this week")

    # 4. Chart
    console.print("[cyan]Generating chart...[/cyan]")
    chart_path = REPORTS_DIR / "weekly_intake_chart.png"
    chart_ok = generate_chart(chart_path)
    if chart_ok:
        console.print(f"  Chart saved to {chart_path}")
    else:
        console.print("  [yellow]No data for chart (master CSV empty or missing dates)[/yellow]")
        chart_path = None

    # 5. PDF
    console.print("[cyan]Generating PDF report...[/cyan]")
    pdf_path = generate_pdf_report(api_data, safety_data, pipeline_data, chart_path)
    console.print(f"\n[bold green]Report saved: {pdf_path}[/bold green]")

    return pdf_path


if __name__ == "__main__":
    run_supervisor()
