# Tally Form Setup Guide

## Notion Database

**DB Name:** Project Requirements
**DB ID:** `311a0c6e-6168-8153-a71d-c5fc7c4c4117`
**Location:** Social Partnerships > Project Requirements Form Submission NGOs
**URL:** https://www.notion.so/311a0c6e61688153a71dc5fc7c4c4117

---

## Tally Form Structure

Create a new form at https://tally.so and set it up with the sections below.

### Form Title
**TUM Social AI -- Project Requirements**

### Form Description
> To ensure our student teams can start immediately, we need to confirm the technical and operational logistics. Please review the scope and complete the required fields.

---

### Section 1: Project Scope & Impact

| # | Tally Field | Tally Type | Notion Property | Notion Type |
|---|------------|------------|----------------|-------------|
| 1 | Organization Name | Short Text | `Organization Name` | Title |
| 2 | The Problem you wish to be solved | Long Text | `Problem Statement` | Rich Text |
| 3 | Current effort (time/people per week/month) | Long Text | `Current Effort` | Rich Text |
| 4 | Usage frequency (daily/weekly/monthly/ad hoc) | Short Text | `Usage Frequency` | Rich Text |
| 5 | Additional benefits expected | Long Text | `Additional Benefits` | Rich Text |

### Section 2: Data Readiness

| # | Tally Field | Tally Type | Notion Property | Notion Type |
|---|------------|------------|----------------|-------------|
| 6 | Data Availability | Dropdown | `Data Availability` | Select |
|   | Options: `1-7 Days (Ready)` / `Longer / Delayed` | | | |
| 7 | If delayed: details | Short Text (conditional) | `Data Delay Details` | Rich Text |
| 8 | Data Language | Short Text | `Data Language` | Rich Text |

### Section 3: Technical Logistics & Infrastructure

| # | Tally Field | Tally Type | Notion Property | Notion Type |
|---|------------|------------|----------------|-------------|
| 9 | AWS Credits willingness | Dropdown | `AWS Credits` | Select |
|   | Options: `Yes - will open/have account` / `No - will self-fund` / `No budget for infrastructure` | | | |
| 10 | Post-Deployment Sustainability | Dropdown | `Post-Deployment Sustainability` | Select |
|    | Options: `Yes - can cover recurring costs` / `No budget for recurring costs` | | | |
| 11 | Current Tech Ecosystem | Multiple Choice | `Tech Ecosystem` | Multi-Select |
|    | Options: `Microsoft 365 / Teams` / `Google Workspace / Drive` / `Slack / Discord` / `Custom Internal Software` | | | |

### Section 4: Commitment & Timeline

| # | Tally Field | Tally Type | Notion Property | Notion Type |
|---|------------|------------|----------------|-------------|
| 12 | Product Owner Name | Short Text | `PO Name` | Rich Text |
| 13 | Product Owner Role | Short Text | `PO Role` | Rich Text |
| 14 | Product Owner Email | Email | `PO Email` | Email |
| 15 | Product Owner Phone | Phone | `PO Phone` | Phone |
| 16 | PO English Fluency | Dropdown | `PO English Fluency` | Select |
|    | Options: `Confirmed (Professional+)` / `No (Cannot communicate in English)` | | | |
| 17 | PO Technical Competence | Dropdown | `PO Technical Competence` | Select |
|    | Options: `1 - Non-Technical` / `2 - Basic Digital Literacy` / `3 - Tech-Savvy` / `4 - Technical` / `5 - Expert` | | | |
| 18 | Weekly 30min check-in commitment | Dropdown | `Weekly Check-in` | Select |
|    | Options: `Yes` / `No` | | | |
| 19 | Cohort | Dropdown | `Cohort` | Select |
|    | Options: `Summer Semester 2026` / `Winter Semester 2026/2027` | | | |
| 20 | Kick-Off & Demo Day Attendance | Dropdown | `Kick-Off & Demo Day Attendance` | Select |
|    | Options: `Yes - both` / `No - cannot attend` | | | |
| 21 | Format Preference | Dropdown | `Format Preference` | Select |
|    | Options: `Semester Project` / `Hackathon` / `Thesis Topic` / `Either` | | | |

### Section 5: Marketing & Final Sign-off

| # | Tally Field | Tally Type | Notion Property | Notion Type |
|---|------------|------------|----------------|-------------|
| 22 | Marketing Permission | Dropdown | `Marketing Permission` | Select |
|    | Options: `Yes` / `No (Confidential)` | | | |
| 23 | Signatory Full Name | Short Text | `Signatory Name` | Rich Text |
| 24 | Date | Date | `Signature Date` | Date |
| 25 | Signature (Type your name) | Short Text | `Signature (Typed)` | Rich Text |

---

## Tally -> Notion Integration Setup

1. In Tally, go to **form settings** > **Integrations** > **Notion**
2. Connect your Notion workspace
3. Select database: **Project Requirements**
4. Map each Tally field to the corresponding Notion property (see table above)
5. **Important:** The select/dropdown option text in Tally MUST match the Notion select option names EXACTLY (no commas allowed in Notion selects)

---

## Enrichment Script

After form submissions arrive in Notion, run the enrichment script to:
- Find or create the Account in the Accounts DB
- Find or create the Product Owner in the Contacts DB
- Link them via relation properties
- Set status to "Under Review"

```bash
cd tum_sales_agent
source venv/bin/activate
python3 scripts/enrich_requirements.py          # process all new entries
python3 scripts/enrich_requirements.py --dry-run # preview without changes
```

---

## Database Relations & Rollups

The Requirements DB has these cross-database connections:

### Relations (set by enrichment script)
- **Account** -> Accounts DB (links to NGO's company record)
- **Product Owner** -> Contacts DB (links to PO's contact record)

### Rollups (auto-populated from relations)
- **Account Status** <- from Account's Status
- **Account Website** <- from Account's Website URL
- **Account Type** <- from Account's Account Type
- **PO LinkedIn** <- from Contact's LinkedIn
- **PO Contact Email** <- from Contact's Email
