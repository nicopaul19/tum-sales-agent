"""
Rewrite existing partnerships Gmail drafts for AI for Good Hackathon outreach.

This is intentionally scoped to attachment-free drafts sent from
partnerships@tum-socialaiclub.de. It preserves each recipient and the visible
sender signature, while replacing the subject/body with the current hackathon
RRR framework.
"""
from __future__ import annotations

import argparse
import base64
import csv
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import DATA_DIR, DEFAULT_CAMPAIGN_SENDER  # noqa: E402
from utils.gmail_client import (  # noqa: E402
    SENDER_ADDRESS,
    _build_raw_message,
    _get_gmail_service,
    _header_value,
    _payload_has_attachment,
)


EVENT_NAME = "AI-for-Good Hackathon"
EVENT_DATE = "June 26"
EVENT_LOCATION = "Munich university ecosystem"
ORGANIZERS = "OpenAI, Lovable, and other partners"


@dataclass
class DraftRewrite:
    draft_id: str
    to_header: str
    old_subject: str
    new_subject: str
    company: str
    angle: str
    greeting: str
    sender: str
    body: str
    skipped_reason: str = ""
    updated: bool = False


def decode_body_data(data: str) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def extract_plain_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain":
        return decode_body_data(payload.get("body", {}).get("data", ""))
    for part in payload.get("parts", []) or []:
        text = extract_plain_text(part)
        if text:
            return text
    if payload.get("body", {}).get("data"):
        return decode_body_data(payload.get("body", {}).get("data", ""))
    return ""


def clean_company(value: str) -> str:
    company = re.sub(r"\s+", " ", value or "").strip(" :-")
    company = re.sub(r"\s*:?\s*Collaboration Opportunity$", "", company, flags=re.I).strip()
    return company or "your team"


def company_from_subject(subject: str) -> str:
    subject = (subject or "").strip()
    patterns = [
        r"^AI for Good Hackathon x (.+)$",
        r"^Impact Hackathon x (.+?)(?::|$)",
        r"^TUM AI Talent x (.+)$",
        r"^Munich AI Ecosystem x (.+)$",
        r"^Munich AI Visibility x (.+)$",
        r"^AI Builders x (.+)$",
        r"^TUM AI Builders x (.+)$",
        r"^(.+?) x TUM Social AI$",
    ]
    for pattern in patterns:
        match = re.search(pattern, subject, flags=re.I)
        if match:
            return clean_company(match.group(1))
    return clean_company(subject)


def greeting_from_body(body: str, to_header: str) -> str:
    for line in (body or "").splitlines():
        match = re.match(r"\s*Hi\s+(.+?),\s*$", line, flags=re.I)
        if match:
            greeting = match.group(1).strip()
            lowered = greeting.lower().strip(". ")
            if lowered not in {"ai", "team", "there", "hi"} and not greeting.endswith("."):
                return greeting
    email_match = re.search(r"[\w.+-]+@", to_header or "")
    if email_match:
        local = email_match.group(0).split("@", 1)[0]
        token = re.split(r"[._+-]", local)[0]
        return token[:1].upper() + token[1:] if token else "there"
    return "there"


def sender_from_body(body: str) -> str:
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return DEFAULT_CAMPAIGN_SENDER or "Felix Laumann"
    if lines[-1].lower().startswith("best"):
        return DEFAULT_CAMPAIGN_SENDER or "Felix Laumann"
    return lines[-1]


def infer_angle(subject: str, body: str) -> str:
    text = f"{subject} {body}".lower()
    if subject.lower().startswith("impact hackathon"):
        return "visibility"
    if "strong talent access opportunity" in text or "early access to ai and engineering talent" in text:
        return "talent"
    if "builder-community opportunity" in text or "more ai builders to use and test" in text:
        return "technical"
    if "credible impact opportunity" in text:
        return "impact"
    if "concrete visibility opportunity" in text or "more visibility in the munich university ecosystem" in text:
        return "visibility"
    if any(term in text for term in ("talent", "recruit", "hiring", "people", "hr", "cv access")):
        return "talent"
    if any(term in text for term in ("api", "cloud", "developer", "devrel", "product feedback", "ai builders", "tooling")):
        return "technical"
    if any(term in text for term in ("csr", "sustain", "impact", "nonprofit", "non-profit", "for good")):
        return "impact"
    if any(term in text for term in ("visibility", "brand", "marketing", "ecosystem", "campus")):
        return "visibility"
    return "partnership"


def opening_question(company: str, angle: str) -> str:
    possessive = f"{company}'" if company.lower().endswith("s") else f"{company}'s"
    if angle == "talent":
        return f"are you currently trying to get early access to AI and engineering talent for {company}?"
    if angle == "technical":
        return f"are you currently trying to get more AI builders to use and test {possessive} tools?"
    if angle == "impact":
        return f"are you currently looking for credible AI-for-Good visibility for {company}?"
    if angle == "visibility":
        return f"are you currently trying to get more visibility in the Munich university ecosystem for {company}?"
    return f"could a concrete partner role in Munich's AI ecosystem be relevant for {company}?"


def angle_sentence(company: str, angle: str) -> str:
    if angle == "talent":
        return (
            f"For {company}, this could be a strong talent access opportunity: meet AI and "
            "engineering talent early while positioning the team as an exciting tech brand on campus."
        )
    if angle == "technical":
        return (
            f"For {company}, this could be a sharp builder-community opportunity: give students "
            "access to your API, cloud credits or tooling and get feedback under real hackathon pressure."
        )
    if angle == "impact":
        return (
            f"For {company}, this could be a credible impact opportunity: visibly support AI teams "
            "building for global non-profits and help winning ideas move into implementation."
        )
    if angle == "visibility":
        return (
            f"For {company}, this could be a concrete visibility opportunity: stage presence, "
            "booth conversations and social reach inside Munich's AI ecosystem."
        )
    return (
        f"For {company}, this could be a strong partnership opportunity: stage visibility, "
        "a useful event role and direct access to Munich's university builder crowd."
    )


def build_body(greeting: str, company: str, angle: str, sender: str) -> str:
    return (
        f"Hi {greeting},\n\n"
        f"First of all, congrats to your new funding round recently! As {company} is probably "
        f"going to grow a lot in the coming months, I thought I might reach out with a proposal "
        f"around talent access and visibility in the Munich university ecosystem.\n\n"
        f"{opening_question(company, angle)} As TUM Social AI, we are hosting the \"{EVENT_NAME}\" "
        f"on {EVENT_DATE} in the {EVENT_LOCATION} together with {ORGANIZERS}. It is positioned "
        "around interdisciplinary students building real AI applications for global non-profits. "
        "We're already partnering up with organizations like the UN, OpenAI and Lovable.\n\n"
        f"{angle_sentence(company, angle)}\n\n"
        "We are looking for sponsors and tech-stack/API/cloud-credit partners. In return we can offer "
        "keynote-stage visibility, booth presence, exclusive CV access, social media reach inside "
        "Munich's AI ecosystem and a concrete role in helping winning teams move into implementation.\n\n"
        "Would a partner slot be relevant for your team?\n\n"
        "Best\n"
        f"{sender}"
    )


def list_rewrites(service) -> list[DraftRewrite]:
    rewrites: list[DraftRewrite] = []
    page_token = None
    while True:
        params = {"userId": "me", "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        response = service.users().drafts().list(**params).execute()
        for draft in response.get("drafts", []):
            draft_id = draft.get("id", "")
            full = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
            message = full.get("message", {})
            payload = message.get("payload", {})
            from_header = _header_value(message, "from").lower()
            if SENDER_ADDRESS.lower() not in from_header:
                continue
            to_header = _header_value(message, "to")
            old_subject = _header_value(message, "subject")
            text = extract_plain_text(payload)
            company = company_from_subject(old_subject)
            angle = infer_angle(old_subject, text)
            greeting = greeting_from_body(text, to_header)
            sender = sender_from_body(text)
            body = build_body(greeting, company, angle, sender)
            rewrite = DraftRewrite(
                draft_id=draft_id,
                to_header=to_header,
                old_subject=old_subject,
                new_subject=f"AI for Good Hackathon x {company}",
                company=company,
                angle=angle,
                greeting=greeting,
                sender=sender,
                body=body,
            )
            if _payload_has_attachment(payload):
                rewrite.skipped_reason = "has_attachment"
            rewrites.append(rewrite)
        page_token = response.get("nextPageToken")
        if not page_token:
            return rewrites


def write_report(rewrites: list[DraftRewrite], apply: bool) -> Path:
    reports_dir = DATA_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    suffix = "live" if apply else "dry_run"
    path = reports_dir / f"ai_for_good_hackathon_draft_rewrites_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{suffix}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "draft_id",
                "to",
                "old_subject",
                "new_subject",
                "company",
                "angle",
                "greeting",
                "sender",
                "updated",
                "skipped_reason",
                "body",
            ],
        )
        writer.writeheader()
        for rewrite in rewrites:
            writer.writerow(
                {
                    "draft_id": rewrite.draft_id,
                    "to": rewrite.to_header,
                    "old_subject": rewrite.old_subject,
                    "new_subject": rewrite.new_subject,
                    "company": rewrite.company,
                    "angle": rewrite.angle,
                    "greeting": rewrite.greeting,
                    "sender": rewrite.sender,
                    "updated": rewrite.updated,
                    "skipped_reason": rewrite.skipped_reason,
                    "body": rewrite.body,
                }
            )
    return path


def audit_rewrite(rewrite: DraftRewrite) -> list[str]:
    issues: list[str] = []
    required = [
        EVENT_NAME,
        EVENT_DATE,
        EVENT_LOCATION,
        "new funding round",
        "global non-profits",
        "real AI applications",
        "the UN",
        "OpenAI",
        "Lovable",
        "Would a partner slot be relevant",
    ]
    haystack = f"{rewrite.new_subject}\n{rewrite.body}"
    for item in required:
        if item not in haystack:
            issues.append(f"missing {item}")
    if rewrite.skipped_reason:
        issues.append(f"skipped {rewrite.skipped_reason}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite partnership Gmail drafts for the AI for Good Hackathon")
    parser.add_argument("--apply", action="store_true", help="Update Gmail drafts. Omit for a dry run.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Seconds to sleep between Gmail updates.")
    args = parser.parse_args()

    service = _get_gmail_service()
    if not service:
        raise RuntimeError("Could not initialize Gmail service")

    rewrites = list_rewrites(service)
    if args.apply:
        for rewrite in rewrites:
            if rewrite.skipped_reason:
                continue
            raw = _build_raw_message(rewrite.to_header, rewrite.new_subject, rewrite.body)
            service.users().drafts().update(
                userId="me",
                id=rewrite.draft_id,
                body={"id": rewrite.draft_id, "message": {"raw": raw}},
            ).execute()
            rewrite.updated = True
            time.sleep(args.sleep)

    report = write_report(rewrites, args.apply)
    issue_count = sum(len(audit_rewrite(rewrite)) for rewrite in rewrites)
    updated = sum(1 for rewrite in rewrites if rewrite.updated)
    skipped = sum(1 for rewrite in rewrites if rewrite.skipped_reason)
    print(f"drafts_found={len(rewrites)} updated={updated} skipped={skipped} audit_issues={issue_count}")
    print(f"report={report}")
    if issue_count:
        for rewrite in rewrites:
            issues = audit_rewrite(rewrite)
            if issues:
                print(f"{rewrite.draft_id} {rewrite.to_header}: {'; '.join(issues)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
