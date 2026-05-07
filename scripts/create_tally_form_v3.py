#!/usr/bin/env python3
"""
Create the TUM Social AI Collaboration Requirements form on Tally.so via API.
V3: Single page, no page breaks, MULTIPLE_CHOICE instead of DROPDOWN.

Usage:
    cd tum_sales_agent
    source venv/bin/activate
    python3 scripts/create_tally_form_v3.py
"""

import os
import sys
import uuid
import json
import time
import requests
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

TALLY_API_KEY = os.getenv("TALLY_API_KEY")
TALLY_API = "https://api.tally.so/forms"


def uid():
    return str(uuid.uuid4())


def _opt(i, text, total):
    return {"index": i, "text": text, "isFirst": i == 0, "isLast": i == total - 1}


# ── Block builders ──

def form_title(title):
    return [
        {"uuid": uid(), "type": "FORM_TITLE", "groupUuid": uid(), "groupType": "TEXT",
         "payload": {"title": title, "html": title}},
    ]


def text_block(html):
    return [
        {"uuid": uid(), "type": "TEXT", "groupUuid": uid(), "groupType": "TEXT",
         "payload": {"html": f"<p>{html}</p>"}},
    ]


def heading(text, level=2):
    bt = f"HEADING_{min(level, 3)}"
    return [
        {"uuid": uid(), "type": bt, "groupUuid": uid(), "groupType": bt,
         "payload": {"html": text}},
    ]


def text_input(label, placeholder="", required=False):
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "INPUT_TEXT", "groupUuid": uid(), "groupType": "INPUT_TEXT",
         "payload": {"isRequired": required, "placeholder": placeholder}},
    ]


def textarea(label, placeholder="", required=False):
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "TEXTAREA", "groupUuid": uid(), "groupType": "TEXTAREA",
         "payload": {"isRequired": required, "placeholder": placeholder}},
    ]


def email_input(label, placeholder="", required=False):
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "INPUT_EMAIL", "groupUuid": uid(), "groupType": "INPUT_EMAIL",
         "payload": {"isRequired": required, "placeholder": placeholder}},
    ]


def phone_input(label, placeholder="", required=False):
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "INPUT_PHONE_NUMBER", "groupUuid": uid(), "groupType": "INPUT_PHONE_NUMBER",
         "payload": {"isRequired": required, "placeholder": placeholder}},
    ]


def date_input(label, required=False):
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "INPUT_DATE", "groupUuid": uid(), "groupType": "INPUT_DATE",
         "payload": {"isRequired": required}},
    ]


def choice(label, options, required=False):
    """Multiple choice (radio buttons) - replaces dropdown."""
    gid = uid()
    blocks = [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
    ]
    for i, opt in enumerate(options):
        blocks.append(
            {"uuid": uid(), "type": "MULTIPLE_CHOICE_OPTION", "groupUuid": gid,
             "groupType": "MULTIPLE_CHOICE",
             "payload": _opt(i, opt, len(options))}
        )
    return blocks


def multi_select(label, options, required=False):
    gid = uid()
    blocks = [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
    ]
    for i, opt in enumerate(options):
        blocks.append(
            {"uuid": uid(), "type": "MULTI_SELECT_OPTION", "groupUuid": gid,
             "groupType": "MULTI_SELECT",
             "payload": _opt(i, opt, len(options))}
        )
    return blocks


def file_upload(label, required=False):
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "FILE_UPLOAD", "groupUuid": uid(), "groupType": "FILE_UPLOAD",
         "payload": {"isRequired": required}},
    ]


def build_form():
    blocks = []

    # ── FORM TITLE ──
    blocks += form_title("Bring AI to Your Mission \u2014 TUM Social AI Project Application")

    blocks += text_block(
        "Thank you for the great conversation about your organization\u2014we love what you do! "
        "To make sure we are aligned on the project\u2019s scope, as well as the execution and "
        "handover process, we ask our social partners to fill out this form before presenting "
        "their projects at the upcoming Kick-Off/Hackathon."
    )

    blocks += text_block(
        "Together with our team, the engineers in our next batch of students will decide "
        "which projects and partner organizations they find most compelling. The more details "
        "you can provide below, the higher the chances we can realize this project with you."
    )

    blocks += text_block(
        "<strong>Note on Timeline &amp; Selection:</strong> We present around 10 projects at "
        "each Kick-Off. We will review all submissions internally by mid-April (for the Summer "
        "Semester) or mid-October (for the Winter Semester) and select the most promising 10. "
        "From that shortlist, our engineers will ultimately choose 4\u20135 projects to build. "
        "We will keep you updated on whether your project is selected for the Kick-Off, and "
        "of course, if it makes the final cut."
    )

    blocks += text_block(
        "To ensure our student teams can hit the ground running immediately after the kick-off, "
        "we need to confirm the technical and operational logistics. Please review the scope "
        "and complete the required fields."
    )

    # ── Section 1: Project Scope & Impact ──
    blocks += heading("Section 1: Project Scope &amp; Impact", 1)

    blocks += text_input("Organization Name",
        placeholder="Your organization's name", required=True)

    blocks += textarea("The Problem you wish to be solved",
        placeholder="Briefly describe the core challenge or pain point we discussed "
        "during our last conversation that you would like to address through "
        "this partnership. This ensures we are aligned with your expectations.",
        required=True)

    blocks += text_block(
        "<em>Note: We have sent you our proposed AI solution concept in the email "
        "accompanying this form. Please review it before continuing.</em>")

    blocks += heading("Expected Impact", 3)
    blocks += text_block("To help us understand urgency and prioritize, please estimate the following:")

    blocks += textarea("Current effort",
        placeholder="How much time and how many people does this problem currently take each week or month?")
    blocks += text_input("Usage frequency",
        placeholder="How often would you expect to use the AI tool (daily, weekly, monthly, ad hoc)?")
    blocks += textarea("Additional benefits",
        placeholder="What other measurable benefits would you expect "
        "(cost reduction, faster turnaround, fewer errors, better reporting, "
        "improved service quality, etc.)?")

    # ── Section 2: Data Readiness ──
    blocks += heading("Section 2: Data Readiness", 1)
    blocks += text_block("<em>Our teams work in short sprints. We need to know exactly when we receive the data.</em>")

    blocks += heading("2.1 Data Availability", 3)
    blocks += choice("When can the students access the data (relative to the Project Kick-Off)?",
        ["1 - 7 Days (Data is ready and accessible)",
         "Longer / Delayed (Data is not ready yet)"],
        required=True)

    blocks += text_input("If Longer: How long? What does it depend on?",
        placeholder="Only fill if you selected 'Longer / Delayed' above")

    blocks += text_block("<em>Note: If data is not ready at Kick-Off, the project risks being cancelled.</em>")

    blocks += heading("2.2 Data Language", 3)
    blocks += text_input("What language is the textual data in?",
        placeholder="e.g. English, German, Spanish, Swahili", required=True)

    # ── Section 3: Technical Logistics & Infrastructure ──
    blocks += heading("Section 3: Technical Logistics &amp; Infrastructure", 1)

    blocks += heading("3.1 Infrastructure &amp; Cloud Credits (AWS)", 3)
    blocks += text_block(
        "<em>We have an agreement with AWS that allows us to provide <strong>free cloud credits</strong> "
        "for our social partners.</em>")

    blocks += choice("Are you willing to open an AWS account (if you don't have one) to receive these free credits?",
        ["Yes - we will open an account / have one to receive credits",
         "No - but we will fund the infrastructure ourselves directly",
         "No - and we have no budget for infrastructure"],
        required=True)

    blocks += heading("3.2 Post-Deployment Sustainability", 3)
    blocks += text_block(
        "<em>If free AWS credits are not used or expire, running an AI tool incurs variable "
        "cloud costs once deployed (or if the project scales).</em>")

    blocks += choice("Does your organization have the capacity to fund these recurring operational costs after deployment?",
        ["Yes - we can cover recurring operational cloud costs",
         "No - we do not have budget for recurring software costs"],
        required=True)

    blocks += heading("3.3 Current Tech Ecosystem", 3)
    blocks += multi_select("What tools does your team primarily use? (Check all that apply)",
        ["Microsoft 365 / Teams",
         "Google Workspace / Drive",
         "Slack / Discord",
         "Other (e.g. Custom Internal Software)"])

    blocks += text_input("If 'Other': Please specify which tools/software",
        placeholder="e.g. SAP, Salesforce, custom CRM, etc.")

    # ── Section 4: Commitment & Timeline ──
    blocks += heading("Section 4: Commitment &amp; Timeline", 1)

    blocks += heading("4.1 The Product Owner (PO)", 3)
    blocks += text_block("Who is our main point of contact?")

    blocks += text_input("PO Name", placeholder="Full name", required=True)
    blocks += text_input("PO Role", placeholder="Job title / Role", required=True)
    blocks += email_input("PO Email", placeholder="email@organization.org", required=True)
    blocks += phone_input("PO Phone Number", placeholder="+49 ...", required=True)

    blocks += heading("4.2 PO English Fluency", 3)
    blocks += text_block("<em>Our student teams operate primarily in English. Direct communication is essential.</em>")
    blocks += choice("PO English Fluency",
        ["Confirmed: Professional Working Proficiency or better in English",
         "No: Cannot communicate effectively in English (may disqualify the project)"],
        required=True)

    blocks += heading("4.3 PO Technical Competence", 3)
    blocks += choice("How comfortable is the PO with Software/AI? (Helps us balance the team)",
        ["1 - Non-Technical: Focuses purely on social mission/operations",
         "2 - Basic Digital Literacy: Uses standard tools - understands data concepts",
         "3 - Tech-Savvy: Familiar with APIs - databases - or basic logic",
         "4 - Technical: Can read code or manage software projects",
         "5 - Expert: Software Engineer / Data Scientist background"],
        required=True)

    blocks += heading("4.4 Collaboration Bandwidth", 3)
    blocks += choice("Can the PO commit to a weekly 30-minute check-in/feedback loop?",
        ["Yes - the PO can commit to a weekly 30-minute check-in/feedback loop",
         "No - we cannot guarantee weekly feedback"],
        required=True)

    blocks += heading("4.5 Project Cycle &amp; Attendance", 3)
    blocks += choice("Which cohort are you applying for?",
        ["Summer Semester 2026 (Kick-Off: May | Demo Day: September)",
         "Winter Semester 2026/2027 (Kick-Off: November | Demo Day: March)"],
        required=True)

    blocks += choice("Are you willing to present the challenge (pitch) at the Kick-Off AND participate as a Jury Member at Demo Day? (Virtual/Live)",
        ["Yes - we will be there for both",
         "No - we cannot attend"],
        required=True)

    blocks += heading("4.6 Format Preference", 3)
    blocks += choice("Format Preference",
        ["Semester Project: 3-4 months - deep dive team project",
         "Hackathon: 48h sprint - fast prototype",
         "Thesis Topic: Completed by a single student as a Bachelor's or Master's thesis",
         "Either: We are open to what fits best"],
        required=True)

    # ── Section 5: Marketing & Final Sign-off ──
    blocks += heading("Section 5: Marketing &amp; Final Sign-off", 1)

    blocks += heading("5.1 Marketing Permission", 3)
    blocks += choice("May we use your organization's name and logo for TUM Social AI case studies/website?",
        ["Yes - you may use our name and logo as soon as the project has kicked off",
         "No - keep this project confidential (Internal only)"],
        required=True)

    blocks += text_block(
        "If you selected 'Yes', please upload your organization's logo below "
        "(PNG format, white background preferred).")
    blocks += file_upload("Organization Logo (PNG, white background)")

    blocks += heading("5.2 Final Acknowledgment", 3)
    blocks += text_block(
        "I understand that submitting this form does not guarantee a team. The projects are "
        "selected by the student engineering teams at the Semester Kick-Off or Hackathon. "
        "However, providing clear data, a dedicated Product Owner, and realistic expectations "
        "significantly increases the likelihood of selection.")

    blocks += text_input("Your Name", placeholder="Full name", required=True)
    blocks += date_input("Date", required=True)
    blocks += text_input("Signature (Type your name)",
        placeholder="Type your full name as signature", required=True)

    return {"status": "PUBLISHED", "blocks": blocks}


def main():
    if not TALLY_API_KEY:
        print("TALLY_API_KEY not found in .env")
        sys.exit(1)

    payload = build_form()
    print(f"Form has {len(payload['blocks'])} blocks (single page, no dropdowns)")

    headers = {
        "Authorization": f"Bearer {TALLY_API_KEY}",
        "Content-Type": "application/json",
    }

    print("Creating form on Tally...")
    resp = requests.post(TALLY_API, headers=headers, json=payload)

    if resp.status_code != 201:
        print(f"ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)

    form = resp.json()
    form_id = form["id"]

    time.sleep(2)
    render = requests.get(f"https://tally.so/r/{form_id}")

    # Verify blocks in page data
    page_blocks = 0
    if "__NEXT_DATA__" in render.text:
        import re
        start = render.text.index("__NEXT_DATA__")
        script_start = render.text.rfind("<script", 0, start)
        script_end = render.text.index("</script>", start)
        json_start = render.text.index(">", script_start) + 1
        data = json.loads(render.text[json_start:script_end])
        page_blocks = len(data.get("props", {}).get("pageProps", {}).get("blocks", []))

    print(f"\nForm created successfully!")
    print(f"  Form ID:     {form_id}")
    print(f"  Edit:        https://tally.so/forms/{form_id}/edit")
    print(f"  Share:       https://tally.so/r/{form_id}")
    print(f"  HTTP:        {render.status_code}")
    print(f"  Page blocks: {page_blocks}")

    if render.status_code != 200 or page_blocks == 0:
        print(f"  WARNING: Form may not render correctly!")


if __name__ == "__main__":
    main()
