"""
Cross-environment entrypoint for the Strategic Partnerships agents.

Works from Codex, Claude Code, Antigravity, macOS Terminal, and Windows
PowerShell because it only relies on Python subcommands.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent


def project_python() -> str:
    """Prefer the repo-local virtualenv Python when present."""
    candidates = [
        PROJECT_ROOT / "venv" / "bin" / "python",
        PROJECT_ROOT / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def run_module(module: str, args: list[str]) -> int:
    cmd = [project_python(), "-m", module, *args]
    return subprocess.run(cmd, cwd=PROJECT_ROOT).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="TUM Social AI Strategic Partnerships runner")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Process new screenshots, URLs, and manual contacts")
    collect.add_argument("--watch", action="store_true", help="Watch screenshot input folder")

    rank = sub.add_parser("rank", help="Create an on-demand top leads report")
    rank.add_argument("--subject", default="", help="Optional ranking report email subject")

    upload = sub.add_parser("upload", help="Upload Apollo CSV into Notion")
    upload.add_argument("--csv", required=True, help="Apollo export CSV")
    upload.add_argument("--sender", default="", help="Campaign sender full name")
    upload.add_argument("--dry-run", action="store_true", help="Preview without writing to Notion")
    upload.add_argument("--no-input", action="store_true", help="Fail instead of prompting")

    copywrite = sub.add_parser("copywrite", help="Generate campaign outreach copy")
    copywrite.add_argument("--campaign", default="", help="Campaign ID, e.g. Workflow_0505")
    copywrite.add_argument("--sender", default="", help="Campaign sender full name")
    copywrite.add_argument("--dry-run", action="store_true", help="Preview without writing to Notion")
    copywrite.add_argument("--force", action="store_true", help="Regenerate existing messages")
    copywrite.add_argument("--no-input", action="store_true", help="Fail instead of prompting")

    linkedin = sub.add_parser("linkedin", help="Generate LinkedIn connection review report")
    linkedin.add_argument("--connections-file", default="", help="Saved LinkedIn HTML file")
    linkedin.add_argument("--dry-run", action="store_true", help="No email or Notion updates")
    linkedin.add_argument("--campaigns", default="", help="Comma-separated Campaign IDs")

    sub.add_parser("supervisor", help="Generate an on-demand infrastructure report")

    feedback = sub.add_parser("feedback", help="Run copywriter feedback analysis")
    feedback.add_argument("--dry-run", action="store_true", help="Analyze without writing learnings")
    feedback.add_argument("--min-data", type=int, default=10, help="Minimum resolved outcomes")

    cleanup = sub.add_parser("cleanup", help="Run Notion cleanup")
    cleanup.add_argument("--domains", action="store_true", help="Populate missing domains")
    cleanup.add_argument("--merge", action="store_true", help="Merge duplicates")
    cleanup.add_argument("--all", action="store_true", help="Run all cleanup phases")

    args = parser.parse_args()

    if args.command == "collect":
        return run_module("agents.collector", ["--watch"] if args.watch else [])
    if args.command == "rank":
        module_args = []
        if args.subject:
            module_args += ["--subject", args.subject]
        return run_module("agents.ranking_agent", module_args)
    if args.command == "upload":
        module_args = ["--csv", args.csv]
        if args.sender:
            module_args += ["--sender", args.sender]
        if args.dry_run:
            module_args.append("--dry-run")
        if args.no_input:
            module_args.append("--no-input")
        return run_module("agents.upload_agent", module_args)
    if args.command == "copywrite":
        module_args = []
        if args.campaign:
            module_args += ["--campaign", args.campaign]
        if args.sender:
            module_args += ["--sender", args.sender]
        if args.dry_run:
            module_args.append("--dry-run")
        if args.force:
            module_args.append("--force")
        if args.no_input:
            module_args.append("--no-input")
        return run_module("agents.copywriter_agent", module_args)
    if args.command == "linkedin":
        module_args = []
        if args.connections_file:
            module_args += ["--connections-file", args.connections_file]
        if args.dry_run:
            module_args.append("--dry-run")
        if args.campaigns:
            module_args += ["--campaigns", args.campaigns]
        return run_module("agents.linkedin_manager", module_args)
    if args.command == "supervisor":
        return run_module("agents.supervisor", [])
    if args.command == "feedback":
        module_args = ["--min-data", str(args.min_data)]
        if args.dry_run:
            module_args.append("--dry-run")
        return run_module("agents.feedback_agent", module_args)
    if args.command == "cleanup":
        module_args = []
        if args.domains:
            module_args.append("--domains")
        if args.merge:
            module_args.append("--merge")
        if args.all:
            module_args.append("--all")
        return run_module("agents.notion_cleanup", module_args)

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
