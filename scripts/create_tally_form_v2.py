#!/usr/bin/env python3
"""
Create the TUM Social AI Collaboration Requirements form on Tally.so via API.
V2: Fixed groupUuid pairing (TITLE shares groupUuid with its input) + single page.

Usage:
    cd tum_sales_agent
    source venv/bin/activate
    python3 scripts/create_tally_form_v2.py
"""

import os
import sys
import uuid
import json
import requests
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

TALLY_API_KEY = os.getenv("TALLY_API_KEY")
TALLY_API = "https://api.tally.so/forms"


def uid() -> str:
    return str(uuid.uuid4())


# ── Block builders ──
# Key fix: TITLE and its input share the same groupUuid.
# TITLE uses groupType="QUESTION", input uses its own groupType.


def form_title(title: str) -> list:
    return [
        {
            "uuid": uid(),
            "type": "FORM_TITLE",
            "groupUuid": uid(),
            "groupType": "TEXT",
            "payload": {"title": title, "html": title},
        }
    ]


def text_block(html: str) -> list:
    return [
        {
            "uuid": uid(),
            "type": "TEXT",
            "groupUuid": uid(),
            "groupType": "TEXT",
            "payload": {"html": f"<p>{html}</p>"},
        }
    ]


def page_break(index: int, is_first: bool = False, is_last: bool = False) -> list:
    """PAGE_BREAK block — required for forms with dropdowns."""
    return [
        {
            "uuid": uid(),
            "type": "PAGE_BREAK",
            "groupUuid": uid(),
            "groupType": "PAGE_BREAK",
            "payload": {
                "index": index,
                "isQualifiedForThankYouPage": False,
                "isFirst": is_first,
                "isLast": is_last,
            },
        }
    ]


def heading(text: str, level: int = 2) -> list:
    block_type = f"HEADING_{min(level, 3)}"
    return [
        {
            "uuid": uid(),
            "type": block_type,
            "groupUuid": uid(),
            "groupType": block_type,
            "payload": {"html": text},
        }
    ]


def text_input(label: str, placeholder: str = "", required: bool = False) -> list:
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "INPUT_TEXT", "groupUuid": uid(), "groupType": "INPUT_TEXT",
         "payload": {"isRequired": required, "placeholder": placeholder}},
    ]


def textarea(label: str, placeholder: str = "", required: bool = False) -> list:
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "TEXTAREA", "groupUuid": uid(), "groupType": "TEXTAREA",
         "payload": {"isRequired": required, "placeholder": placeholder}},
    ]


def email_input(label: str, placeholder: str = "", required: bool = False) -> list:
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "INPUT_EMAIL", "groupUuid": uid(), "groupType": "INPUT_EMAIL",
         "payload": {"isRequired": required, "placeholder": placeholder}},
    ]


def phone_input(label: str, placeholder: str = "", required: bool = False) -> list:
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "INPUT_PHONE_NUMBER", "groupUuid": uid(), "groupType": "INPUT_PHONE_NUMBER",
         "payload": {"isRequired": required, "placeholder": placeholder}},
    ]


def date_input(label: str, required: bool = False) -> list:
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "INPUT_DATE", "groupUuid": uid(), "groupType": "INPUT_DATE",
         "payload": {"isRequired": required}},
    ]


def _option_payload(index: int, text: str, total: int) -> dict:
    return {
        "index": index,
        "text": text,
        "isFirst": index == 0,
        "isLast": index == total - 1,
    }


def dropdown(label: str, options: list, required: bool = False) -> list:
    gid = uid()
    blocks = [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "DROPDOWN", "groupUuid": gid, "groupType": "QUESTION",
         "payload": {"isRequired": required}},
    ]
    for i, opt in enumerate(options):
        blocks.append(
            {"uuid": uid(), "type": "DROPDOWN_OPTION", "groupUuid": gid, "groupType": "DROPDOWN",
             "payload": _option_payload(i, opt, len(options))}
        )
    return blocks


def multi_select(label: str, options: list, required: bool = False) -> list:
    gid = uid()
    blocks = [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
    ]
    for i, opt in enumerate(options):
        blocks.append(
            {"uuid": uid(), "type": "MULTI_SELECT_OPTION", "groupUuid": gid,
             "groupType": "MULTI_SELECT",
             "payload": _option_payload(i, opt, len(options))}
        )
    return blocks


def file_upload(label: str, required: bool = False) -> list:
    return [
        {"uuid": uid(), "type": "TITLE", "groupUuid": uid(), "groupType": "TITLE",
         "payload": {"html": label}},
        {"uuid": uid(), "type": "FILE_UPLOAD", "groupUuid": uid(), "groupType": "FILE_UPLOAD",
         "payload": {"isRequired": required}},
    ]


def build_form() -> dict:
    """Build the complete single-page form payload."""

    blocks = []

    # ── FORM TITLE ──
    blocks += form_title("TUM Social AI \u2014 Collaboration Requirements Form")

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
        "<strong>Note on Timeline & Selection:</strong> We present around 10 projects at "
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
    blocks += heading("Section 1: Project Scope & Impact", 1)

    blocks += text_input(
        "Organization Name",
        placeholder="Your organization's name",
        required=True,
    )

    blocks += textarea(
        "The Problem you wish to be solved",
        placeholder=(
            "Briefly describe the core challenge or pain point we discussed "
            "during our last conversation that you would like to address through "
            "this partnership. This ensures we are aligned with your expectations."
        ),
        required=True,
    )

    blocks += text_block(
        "<em>Note: We have sent you our proposed AI solution concept in the email "
        "accompanying this form. Please review it before continuing.</em>"
    )

    blocks += heading("Expected Impact", 3)
    blocks += text_block(
        "To help us understand urgency and prioritize, please estimate the following:"
    )

    blocks += textarea(
        "Current effort",
        placeholder="How much time and how many people does this problem currently take each week or month?",
    )
    blocks += text_input(
        "Usage frequency",
        placeholder="How often would you expect to use the AI tool (daily, weekly, monthly, ad hoc)?",
    )
    blocks += textarea(
        "Additional benefits",
        placeholder=(
            "What other measurable benefits would you expect "
            "(cost reduction, faster turnaround, fewer errors, better reporting, "
            "improved service quality, etc.)?"
        ),
    )

    # ── Section 2: Data Readiness ──
    blocks += page_break(0, is_first=True)
    blocks += heading("Section 2: Data Readiness", 1)
    blocks += text_block(
        "<em>Our teams work in short sprints. We need to know exactly when we receive the data.</em>"
    )

    blocks += heading("2.1 Data Availability", 3)
    blocks += dropdown(
        "When can the students access the data (relative to the Project Kick-Off)?",
        [
            "1 - 7 Days (Data is ready and accessible)",
            "Longer / Delayed (Data is not ready yet)",
        ],
        required=True,
    )

    blocks += text_input(
        "If Longer: How long? What does it depend on?",
        placeholder="Only fill if you selected 'Longer / Delayed' above",
    )

    blocks += text_block(
        "<em>Note: If data is not ready at Kick-Off, the project risks being cancelled.</em>"
    )

    blocks += heading("2.2 Data Language", 3)
    blocks += text_input(
        "What language is the textual data in?",
        placeholder="e.g. English, German, Spanish, Swahili",
        required=True,
    )

    # ── Section 3: Technical Logistics & Infrastructure ──
    blocks += page_break(1)
    blocks += heading("Section 3: Technical Logistics & Infrastructure", 1)

    blocks += heading("3.1 Infrastructure & Cloud Credits (AWS)", 3)
    blocks += text_block(
        "<em>We have an agreement with AWS that allows us to provide <strong>free cloud credits</strong> "
        "for our social partners.</em>"
    )

    blocks += dropdown(
        "Are you willing to open an AWS account (if you don't have one) to receive these free credits?",
        [
            "Yes - we will open an account / have one to receive credits",
            "No - but we will fund the infrastructure ourselves directly",
            "No - and we have no budget for infrastructure",
        ],
        required=True,
    )

    blocks += heading("3.2 Post-Deployment Sustainability", 3)
    blocks += text_block(
        "<em>If free AWS credits are not used or expire, running an AI tool incurs variable "
        "cloud costs once deployed (or if the project scales).</em>"
    )

    blocks += dropdown(
        "Does your organization have the capacity to fund these recurring operational costs after deployment?",
        [
            "Yes - we can cover recurring operational cloud costs",
            "No - we do not have budget for recurring software costs",
        ],
        required=True,
    )

    blocks += heading("3.3 Current Tech Ecosystem", 3)
    blocks += multi_select(
        "What tools does your team primarily use? (Check all that apply)",
        [
            "Microsoft 365 / Teams",
            "Google Workspace / Drive",
            "Slack / Discord",
            "Other (e.g. Custom Internal Software)",
        ],
    )

    blocks += text_input(
        "If 'Other': Please specify which tools/software",
        placeholder="e.g. SAP, Salesforce, custom CRM, etc.",
    )

    # ── Section 4: Commitment & Timeline ──
    blocks += page_break(2)
    blocks += heading("Section 4: Commitment & Timeline", 1)

    blocks += heading("4.1 The Product Owner (PO)", 3)
    blocks += text_block("Who is our main point of contact?")

    blocks += text_input("PO Name", placeholder="Full name", required=True)
    blocks += text_input("PO Role", placeholder="Job title / Role", required=True)
    blocks += email_input("PO Email", placeholder="email@organization.org", required=True)
    blocks += phone_input("PO Phone Number", placeholder="+49 ...", required=True)

    blocks += heading("4.2 PO English Fluency", 3)
    blocks += text_block(
        "<em>Our student teams operate primarily in English. Direct communication is essential.</em>"
    )
    blocks += dropdown(
        "PO English Fluency",
        [
            "Confirmed: Professional Working Proficiency or better in English",
            "No: Cannot communicate effectively in English (may disqualify the project)",
        ],
        required=True,
    )

    blocks += heading("4.3 PO Technical Competence", 3)
    blocks += dropdown(
        "How comfortable is the PO with Software/AI? (Helps us balance the team)",
        [
            "1 - Non-Technical: Focuses purely on social mission/operations",
            "2 - Basic Digital Literacy: Uses standard tools - understands data concepts",
            "3 - Tech-Savvy: Familiar with APIs - databases - or basic logic",
            "4 - Technical: Can read code or manage software projects",
            "5 - Expert: Software Engineer / Data Scientist background",
        ],
        required=True,
    )

    blocks += heading("4.4 Collaboration Bandwidth", 3)
    blocks += dropdown(
        "Can the PO commit to a weekly 30-minute check-in/feedback loop?",
        [
            "Yes - the PO can commit to a weekly 30-minute check-in/feedback loop",
            "No - we cannot guarantee weekly feedback",
        ],
        required=True,
    )

    blocks += heading("4.5 Project Cycle & Attendance", 3)

    blocks += dropdown(
        "Which cohort are you applying for?",
        [
            "Summer Semester 2026 (Kick-Off: May | Demo Day: September)",
            "Winter Semester 2026/2027 (Kick-Off: November | Demo Day: March)",
        ],
        required=True,
    )

    blocks += dropdown(
        "Are you willing to present the challenge (pitch) at the Kick-Off AND participate as a Jury Member at Demo Day? (Virtual/Live)",
        [
            "Yes - we will be there for both",
            "No - we cannot attend",
        ],
        required=True,
    )

    blocks += heading("4.6 Format Preference", 3)
    blocks += dropdown(
        "Format Preference",
        [
            "Semester Project: 3-4 months - deep dive team project",
            "Hackathon: 48h sprint - fast prototype",
            "Thesis Topic: Completed by a single student as a Bachelor's or Master's thesis",
            "Either: We are open to what fits best",
        ],
        required=True,
    )

    # ── Section 5: Marketing & Final Sign-off ──
    blocks += page_break(3, is_last=True)
    blocks += heading("Section 5: Marketing & Final Sign-off", 1)

    blocks += heading("5.1 Marketing Permission", 3)
    blocks += dropdown(
        "May we use your organization's name and logo for TUM Social AI case studies/website?",
        [
            "Yes - you may use our name and logo as soon as the project has kicked off",
            "No - keep this project confidential (Internal only)",
        ],
        required=True,
    )

    blocks += text_block(
        "If you selected 'Yes', please upload your organization's logo below "
        "(PNG format, white background preferred)."
    )
    blocks += file_upload(
        "Organization Logo (PNG, white background)",
        required=False,
    )

    blocks += heading("5.2 Final Acknowledgment", 3)
    blocks += text_block(
        "I understand that submitting this form does not guarantee a team. The projects are "
        "selected by the student engineering teams at the Semester Kick-Off or Hackathon. "
        "However, providing clear data, a dedicated Product Owner, and realistic expectations "
        "significantly increases the likelihood of selection."
    )

    blocks += text_input("Your Name", placeholder="Full name", required=True)
    blocks += date_input("Date", required=True)
    blocks += text_input("Signature (Type your name)", placeholder="Type your full name as signature", required=True)

    return {
        "status": "PUBLISHED",
        "blocks": blocks,
    }


def main():
    if not TALLY_API_KEY:
        print("TALLY_API_KEY not found in .env")
        sys.exit(1)

    payload = build_form()
    print(f"Form has {len(payload['blocks'])} blocks (5 pages with page breaks)")

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

    # Verify it renders
    import time
    time.sleep(2)
    verify = requests.get(f"https://tally.so/r/{form_id}")

    print(f"\nForm created successfully!")
    print(f"  Form ID: {form_id}")
    print(f"  Edit:    https://tally.so/forms/{form_id}/edit")
    print(f"  Share:   https://tally.so/r/{form_id}")
    print(f"  Render:  HTTP {verify.status_code}")

    if verify.status_code != 200:
        print(f"  WARNING: Form may not render correctly!")

    print()
    print("Next steps:")
    print("  1. Open the edit link to review the form")
    print("  2. Upload the TUM Social AI logo in the form header")
    print("  3. Set up styling (blue theme, redirect URL, email notifications)")
    print("  4. Go to Integrations > Notion")
    print("  5. Connect workspace and select 'Project Requirements' database")
    print("  6. Map each field to the corresponding Notion property")
    print("     (see TALLY_FORM_SETUP.md for the full mapping)")


if __name__ == "__main__":
    main()
