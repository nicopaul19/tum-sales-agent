# TUM Social AI — Sales Agent System
## Complete Onboarding Guide

**Last Updated:** February 10, 2026
**Version:** 2.1
**Authors:** Nicolas Paul, Claude Code

**Video Walkthrough:** [Watch the full onboarding video on Loom](https://www.loom.com/share/f8a02d2259f349d9a2b0413921a6e12f)

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Architecture & Data Flow](#architecture--data-flow)
3. [Installation & Setup](#installation--setup)
4. [Agent Documentation](#agent-documentation)
5. [Apollo Enrichment Workflow](#apollo-enrichment-workflow)
6. [Notion Integration](#notion-integration)
7. [Daily Operations](#daily-operations)
8. [Troubleshooting](#troubleshooting)
9. [Appendix](#appendix)

---

## System Overview

### What is TUM Sales Agent?

The TUM Sales Agent is an **AI-powered lead generation and outreach automation system** designed for TUM Social AI's strategic partnership development. The system automates:

- **Lead collection** from multiple sources (LinkedIn screenshots, post URLs, manual input)
- **AI-powered lead scoring** using GPT-4o (0-10 scale based on ICP fit)
- **Data enrichment** via Apollo.io (emails, phone numbers, company data)
- **Contact upload** to Notion CRM with full relationship tracking
- **Personalized outreach generation** (4 messages per contact: LinkedIn cold, LinkedIn follow-up, email subject, email body)
- **LinkedIn connections analysis** with automated email reports (PDF + CSV), Notion status updates, and actionable outreach messages
- **Weekly reporting** with cost tracking and pipeline analytics

### Key Features

✅ **Multi-source lead aggregation** — Screenshots, LinkedIn URLs, manual contacts
✅ **Domain-based deduplication** — Prevents duplicate companies and contacts
✅ **GPT-4o lead scoring** — Social impact + ICP relevance + contact bonus
✅ **Apollo enrichment integration** — 28-column CSV → Notion Accounts + Contacts
✅ **Tone-of-voice copywriting** — Data-backed outreach templates (Gong, Lavender, Winning by Design)
✅ **Multi-language support** — German (DACH), Spanish (ES/LATAM), English (default)
✅ **Automated scheduling** — macOS launchd agents for hands-free operation
✅ **Cost tracking** — Every OpenAI API call logged for budget monitoring
✅ **Weekly audit reports** — PDF summaries with charts and file integrity checks
✅ **LinkedIn connections automation** — Save connections page via Quick Action, receive email with outreach actions (PDF + CSV)
✅ **macOS Quick Actions** — 4 Automator workflows for one-click lead input and LinkedIn analysis

---

## Architecture & Data Flow

### System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        INPUT SOURCES (3 STREAMS)                         │
├─────────────────────────────────────────────────────────────────────────┤
│  1. Screenshots (data/inputs/images/new/)                               │
│     → GPT-4o Vision extracts: company, domain, contact, trigger         │
│                                                                          │
│  2. LinkedIn Post URLs (data/inputs/linkedin_urls/new/)                 │
│     → Web fetch + GPT-4o extracts company info                          │
│                                                                          │
│  3. Manual Contacts (data/inputs/manual_contacts/new/)                  │
│     → Direct text file: linkedin_url, company_name, trigger             │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                     COLLECTOR AGENT (Mon/Wed/Fri 9am)                   │
│  • Aggregates all 3 streams                                             │
│  • Domain-based deduplication                                           │
│  • Verifies domains via HTTP HEAD                                       │
│  • Outputs: master_input.csv (11 columns, one contact per row)          │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                     RANKING AGENT (Tue/Thu 10am)                        │
│  • GPT-4o scoring (0-10 scale)                                          │
│  • Notion dedup check (status >= "Engaged" → blocked)                   │
│  • +1 bonus for leads with person_name                                  │
│  • Exports: weekly_qualified_leads.csv (all score >= 5)                 │
│  • Overflow → backlog.csv (re-scored next week)                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                  APOLLO ENRICHMENT (Manual Step)                        │
│  • Upload weekly_qualified_leads.csv to Apollo                          │
│  • Enrich: emails, phones, job titles, funding, employees               │
│  • Export: apollo-contacts-export.csv (28 columns)                      │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                      UPLOAD AGENT (Manual Run)                          │
│  • Groups by Apollo Account Id                                          │
│  • Creates/updates Notion Accounts DB (status-aware)                    │
│  • Creates Notion Contacts DB (linked via relation)                     │
│  • Campaign ID: Workflow_DDMM (e.g., Workflow_0902)                     │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                    COPYWRITER AGENT (Manual Run)                        │
│  • Queries Contacts DB filtered by campaign                             │
│  • Resolves linked Account for context                                  │
│  • GPT-4o generates 4 messages per contact                              │
│  • Writes to Notion: LinkedIn 1st Cold, LinkedIn FU, Email Body, Email  │
│    Subject                                                               │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                      SUPERVISOR AGENT (Sat 9am)                         │
│  • Weekly PDF audit report                                              │
│  • API cost tracking (OpenAI usage)                                     │
│  • Pipeline analytics (leads processed, qualified, exported)            │
│  • File integrity checks (SHA-256 hashes)                               │
└─────────────────────────────────────────────────────────────────────────┘
```

### CSV Schema Reference

#### master_input.csv (11 columns)
```
date_added, company_name, company_domain, person_name, linkedin_url_contact,
linkedin_url_post, trigger, score, reasoning, source, status
```

**Status values:**
- `pending` — New lead, not yet scored
- `qualified_for_apollo` — Score >= 5, exported to Apollo
- `backlog` — Score >= 5 but outside weekly cap (25 leads)
- `requalified` — Previously exported, re-entered with new contact
- `filtered_out` — Score < 5
- `duplicate_in_notion` — Blocked by Notion status >= "Engaged"
- `exported_previously` — Already exported, same contact
- `archived` — 3rd+ contact for same company in same batch

**Source values:**
- `manual_screenshot` — From screenshot extraction
- `linkedin_post` — From LinkedIn post URL
- `manual_contact` — From manual text file

#### weekly_qualified_leads.csv (Export to Apollo)
```
date_added, company_name, company_domain, person_name, linkedin_url_contact,
trigger, score, reasoning, source
```
*Note:* No `status`, `linkedin_url_post`, `draft_message`, `email`, or `red_flags` fields.

#### apollo-contacts-export.csv (28 columns)
```
First Name, Last Name, Title, Email, Person Linkedin Url, Corporate Phone,
Apollo Contact Id, Qualify Contact, Company Name, Company Name for Emails,
Website, Company Linkedin Url, City, Company State, Company Country,
Company Phone, Industry, # Employees, Latest Funding, Latest Funding Amount,
Apollo Account Id, Trigger, Mission, Lead Score, (+ 4 more internal Apollo cols)
```

---

## Installation & Setup

### Prerequisites

**Required:**
- **Python 3.8+** (tested on 3.9)
- **OpenAI API key** (GPT-4o access)
- **Notion integration token** + database IDs
- **Apollo.io account** (optional for enrichment)

**Platform Support:**
- ✅ **macOS** (fully tested, automated scheduling supported)
- ✅ **Linux** (manual runs supported, no automated scheduling yet)
- ✅ **Windows** (manual runs supported, see Windows setup below)

### Setup Instructions

#### 1. Clone/Download the Repository

```bash
# Navigate to your project directory
cd "/Users/your-username/your-projects-folder"

# Or on Windows:
cd "C:\Users\your-username\your-projects-folder"
```

#### 2. Create Virtual Environment

**macOS/Linux:**
```bash
cd tum_sales_agent
python3 -m venv venv
source venv/bin/activate
```

**Windows (PowerShell):**
```powershell
cd tum_sales_agent
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**Windows (Command Prompt):**
```cmd
cd tum_sales_agent
python -m venv venv
venv\Scripts\activate.bat
```

#### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

**Key dependencies:**
- `openai` — GPT-4o API client with structured output
- `requests` — HTTP library for Notion API and web fetching
- `watchdog` — File system monitoring for watch mode
- `rich` — Beautiful terminal output and tables
- `fpdf` — PDF generation for audit reports
- `beautifulsoup4` — HTML parsing for LinkedIn content
- `pydantic` — Data validation and structured outputs
- `pillow` — Image processing for screenshots
- `python-dotenv` — Environment variable management

#### 4. Configure Environment Variables

```bash
# Copy the template
cp .env.template .env

# Edit with your favorite editor
nano .env  # or vim, code, notepad, etc.
```

**Required variables:**
```bash
OPENAI_API_KEY=sk-proj-...
NOTION_TOKEN=secret_...
NOTION_DB_ACCOUNTS_ID=266a0c6e-6168-80c1-...
NOTION_DB_CONTACTS_ID=266a0c6e-6168-80c2-...
```

**Gmail variables (for LinkedIn agent email delivery):**
```bash
GMAIL_ADDRESS=your-email@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx    # Generate at myaccount.google.com/apppasswords
REPORT_RECIPIENT_EMAIL=recipient@example.com
```

**Important:** Use a Gmail **App Password**, not your regular Google password. Go to https://myaccount.google.com/apppasswords to generate one (requires 2FA enabled).

**Where to find these:**

1. **OpenAI API Key**
   - Go to https://platform.openai.com/api-keys
   - Create new secret key
   - Copy and paste into `.env`

2. **Notion Token**
   - Go to https://www.notion.so/my-integrations
   - Create new integration
   - Copy "Internal Integration Token"
   - **IMPORTANT:** Share your Accounts and Contacts databases with this integration

3. **Notion Database IDs**
   - Open your Notion Accounts database in browser
   - Copy the ID from URL: `notion.so/workspace/DATABASE_ID?v=...`
   - Repeat for Contacts database

#### 5. Verify Installation

```bash
# Test the collector
python -m agents.collector --help

# Test OpenAI connection
python -c "from utils.config import OPENAI_API_KEY; print('✓ OpenAI key loaded' if OPENAI_API_KEY else '✗ Missing key')"

# Test Notion connection
python -c "from utils.config import NOTION_TOKEN; print('✓ Notion token loaded' if NOTION_TOKEN else '✗ Missing token')"
```

---

## Agent Documentation

### 1. Collector Agent

**Purpose:** Aggregate leads from 3 input streams, deduplicate, verify domains, output to master CSV.

**Location:** `agents/collector.py`

**Usage:**
```bash
# One-time collection
python -m agents.collector

# Watch mode (monitors data/inputs/ for new files)
python -m agents.collector --watch
```

**Input directories:**
- `data/inputs/images/new/` — LinkedIn screenshots (.png, .jpg)
- `data/inputs/linkedin_urls/new/` — Text files with LinkedIn post URLs (one per line)
- `data/inputs/manual_contacts/new/` — Text files with format: `linkedin_url, company_name, trigger`

**Output:**
- `data/tables/master_input.csv` — All leads (append-only, deduplicated)

**Key behaviors:**
- **Deduplication key:** `(domain_or_company, person_name)` + LinkedIn profile URL check
- **Max 2 contacts per company per batch** — 3rd+ contact → `archived` status
- **Domain verification:** HTTP HEAD request to find valid domain (http → https upgrade)
- **Files moved to `processed/` after successful extraction**

**Scheduled:** Mon/Wed/Fri 9am (macOS launchd)

---

### 2. Ranking Agent

**Purpose:** Score leads using GPT-4o, export qualified leads (score >= 5) to Apollo CSV.

**Location:** `agents/ranking_agent.py`

**Usage:**
```bash
# Rank all pending + backlog leads
python -m agents.ranking_agent
```

**Scoring criteria (0-10 scale):**
- **High (8-10):** AI/tech VCs, social impact orgs with DACH presence, mission-aligned companies
- **Medium (5-7):** Tech companies, research institutions, potential strategic partners
- **Low (1-4):** Weak AI connection, low ICP fit
- **Auto-disqualify (0):** Crypto, gambling, weapons, student clubs, pre-seed startups

**Scoring bonuses:**
- **+1 for contact present** (`person_name` is not empty) — capped at 10, skipped if score = 0

**Notion dedup check:**
- Queries Notion Accounts DB for existing domains
- Blocks leads where status >= "Engaged" (index 7+)
- Allows re-qualification if status < "Engaged" → new status: `requalified`

**Weekly cap:**
- Exports top 25 qualified leads (score >= 5) to `weekly_qualified_leads.csv`
- Overflow → `backlog.csv` (re-scored next week)

**Re-export rule:**
- Previously exported companies re-enter weekly list ONLY if the row has a new contact not yet in Notion Contacts DB
- Otherwise → `exported_previously` status

**Output:**
- `data/tables/weekly_qualified_leads_with_contacts.csv` — Qualified leads with contact person
- `data/tables/weekly_qualified_leads_no_contact.csv` — Qualified leads without contact (need Apollo lookup)

**Email delivery:** After scoring, automatically emails a summary report with both CSVs attached to all recipients in `RANKING_REPORT_RECIPIENTS` (comma-separated in `.env`).

**Scheduled:** Tue/Thu 10am (macOS launchd)

---

### 3. Upload Agent

**Purpose:** Import Apollo-enriched CSV into Notion Accounts + Contacts databases with proper relations.

**Location:** `agents/upload_agent.py`

**Usage:**
```bash
python -m agents.upload_agent --csv "/path/to/apollo-contacts-export.csv"
```

**Property mapping:**

**Accounts DB (16 fields):**
| Apollo Column | Notion Property | Action |
|---|---|---|
| Company Name | Organization* | Set |
| Company Name for Emails | Cleaned Name* | Set |
| Website | Website URL* | Set |
| Company Linkedin Url | Company LinkedIn | Set |
| City | City | Set |
| Company Country | Country | Set |
| Company Phone | Company Phone Number | Set |
| Industry | Industry (Corporates) | Set |
| Trigger | Trigger Event | **Append** (never overwrite) |
| Mission (reasoning) | Mission* | Set |
| Lead Score | Lead Score | Set |
| # Employees | # Employees | Set |
| Latest Funding | Latest Funding | Set |
| Latest Funding Amount | Funding Amount | Set |
| Apollo Account Id | Apollo Account ID | Set |
| *(auto)* | Campaign ID | `Workflow_DDMM` (multi_select) |
| *(auto)* | Status | `"Prospect Qualified"` (new only) |

**Contacts DB (6 fields):**
| Apollo Column | Notion Property | Action |
|---|---|---|
| First Name + Last Name | Contact Name | Set |
| Email | Email | Set |
| Person Linkedin Url | LinkedIn | Set |
| Title | Job Title | Set |
| Corporate Phone | Phone | Set |
| Apollo Contact Id | Apollo Contact ID | Set |
| *(auto)* | Accounts | **Relation link** to account page_id |

**Key behaviors:**
1. **Groups by Apollo Account Id** (or domain fallback)
2. **Account creation/update logic:**
   - If NOT exists → create with Status="Prospect Qualified" + Campaign ID
   - If exists, status < Engaged → reset Status to "Prospect Qualified" + fill empty fields only + append trigger
   - If exists, status >= Engaged → fill empty fields only + append trigger (no status change)
3. **Contact creation logic:**
   - Dedup by email → LinkedIn URL → name
   - If NOT exists → create + link to account
   - If exists → skip
4. **Campaign ID:** `Workflow_DDMM` (e.g., `Workflow_0902` for Feb 9)
5. **All rows uploaded** — even "Disqualified" contacts (no filtering)

**Scheduled:** Manual (run after Apollo enrichment)

---

### 4. Copywriter Agent

**Purpose:** Generate 4 personalized outreach messages per contact using GPT-4o with tone-of-voice skill.

**Location:** `agents/copywriter_agent.py`

**Usage:**
```bash
# Generate messages for all contacts without messages
python -m agents.copywriter_agent

# Filter by campaign
python -m agents.copywriter_agent --campaign Workflow_0902

# Preview without writing to Notion
python -m agents.copywriter_agent --campaign Workflow_0902 --dry-run

# Overwrite existing messages (regenerate all)
python -m agents.copywriter_agent --campaign Workflow_0902 --force
```

**Generates 4 messages per contact:**
1. **LinkedIn 1st Cold** (max 75 words) — Initial connection request
2. **LinkedIn FU** (max 60 words) — Follow-up if no reply after 5-7 days, new angle
3. **Cold Email Subject** (max 8 words) — Title Case, descriptive
4. **Cold Email Body** (max 100 words) — References ghosted LinkedIn messages, mobile-optimized

**Skill prompt location:** `data/prompts/outreach_skill.md`

**Tone & style:**
- **Data-backed:** Gong.io, Lavender.ai, Winning by Design, Challenger Sale methodologies
- **Trigger hierarchy:** AA (mission alignment) → A (persona-informed) → B (funding) → C (AI/visibility) → D (student customers) → E (CSRD)
- **Namedropping:** TUM, UN Women, Entreculturas, AWS, Knowunity, Red Cross
- **Language auto-detection:** German (DACH) always "du", Spanish (ES/LATAM), English (default)
- **CTAs:** Always "relevant", never "interesting"
- **Always:** "short call" / "short exchange" / "kurzer Austausch"

**Key rules:**
- Job title is NOT a trigger — it informs which value prop to lead with
- Cold email MUST reference ghosted LinkedIn messages first
- German umlauts: ä, ö, ü, ß (NOT ae, oe, ue)
- Always "du" in German (never "Sie" when using first names)
- Moderate language: "fits well", "strong overlap" (NOT "perfect", "amazing")
- **NEVER mix languages** within a single message (pick ONE: German, English, or Spanish throughout)
- **NEVER use em dashes (—) or en dashes (–)** — use commas instead
- Always say **"non-profits"**, never "NGOs"
- **Winning pattern:** Frame value as a concrete question about whether talent access / student network is relevant for THEIR specific company

**Signature format:**
```
Nicolas Paul
Co-Founder | TUM Social AI | tum-socialaiclub.de
```

**Scheduled:** Manual (run after upload agent)

---

### 5. Supervisor Agent

**Purpose:** Generate weekly PDF audit report with API cost tracking and pipeline analytics.

**Location:** `agents/supervisor.py`

**Usage:**
```bash
python -m agents.supervisor
```

**Report sections:**
1. **API Cost Summary**
   - OpenAI usage by agent and action
   - Token counts (input/output)
   - Cost breakdown (GPT-4o: $2.50/1M input, $10/1M output)
   - Weekly total

2. **Lead Pipeline Analytics**
   - Leads processed by source (screenshot/LinkedIn/manual)
   - Scoring distribution (0-2, 3-4, 5-7, 8-10)
   - Qualified leads count (score >= 5)
   - Weekly export count

3. **File Integrity**
   - SHA-256 hashes for master CSV, weekly qualified CSV
   - File sizes and last modified timestamps

4. **Charts**
   - Lead source breakdown (pie chart)
   - Scoring distribution (bar chart)

**Output:** `data/reports/supervisor_report_YYYYMMDD.pdf`

**Scheduled:** Sat 9am (macOS launchd)

---

### 6. Notion Cleanup Agent

**Purpose:** Populate missing fields and merge duplicate accounts in Notion.

**Location:** `agents/notion_cleanup.py`

**Usage:**
```bash
# Phase 1: Populate missing Website URL and Account Type
python -m agents.notion_cleanup --domains

# Phase 2: Interactive duplicate merge
python -m agents.notion_cleanup --merge

# Run both phases
python -m agents.notion_cleanup --all
```

**Phase 1: Domain & Type Population**
- Queries accounts with empty `Website URL*`
- Uses GPT-4o to resolve domain from company name
- Verifies domain via HTTP HEAD
- Classifies `Account Type*`: NGO, Corporate, Academic, Student Club

**Phase 2: Duplicate Merge**
- Groups accounts by normalized domain
- Interactive CLI prompts for each duplicate set
- **Status hierarchy:** Higher status wins (Engaged > Prospect Qualified)
- **Relation re-linking:** Moves all contacts to primary account
- **Suspect promotion:** If duplicate is "Suspect", primary becomes "Prospect Qualified"
- **Merge log:** `data/logs/merge_log.jsonl` with full before/after snapshots

**Scheduled:** 1st & 15th of month, 10am (macOS launchd)

---

### 7. LinkedIn Analyst Agent

**Purpose:** Parse LinkedIn connections HTML, detect accepted connection requests, update Notion statuses, generate actionable outreach report (PDF + CSV), and email it automatically.

**Location:** `agents/linkedin_manager.py`, `agents/linkedin_parser.py`, `agents/report_generator.py`

**Usage:**
```bash
# Full run: parse connections, update Notion, generate report, send email
python -m agents.linkedin_manager

# Dry run: skip Notion updates and email delivery
python -m agents.linkedin_manager --dry-run

# Test parser on single file
python -m agents.linkedin_parser
```

**Automated trigger:** Use the "Save LinkedIn Inbox" macOS Quick Action (right-click menu) while on LinkedIn's "My Network > Connections" page. This saves the HTML and runs the LinkedIn manager automatically, delivering a report via email.

**Input:** Saved LinkedIn connections HTML → `data/inputs/linkedin_dump/network_*.html` (auto-detects most recent file)

**Output:**
- `data/reports/weekly_linkedin_report_YYYYMMDD.pdf` — PDF report with categorized actions
- `data/reports/linkedin_outreach_actions_YYYYMMDD.csv` — CSV with contact names, LinkedIn URLs, companies, and copy-paste-ready outreach messages (single-line, ready for Numbers/Excel)
- Automated email with both PDF and CSV attached

**Three rules:**
- **Rule A (New Connections):** Notion account status < "Contacted LinkedIn" → Category: `new_connection`. Pulls LinkedIn 1st Cold message from Contacts DB. Updates status to "Contacted LinkedIn".
- **Rule B (Follow-Up):** Notion account `last_edited_time` > 5 days ago → Category: `follow_up`. GPT-4o generates a contextual follow-up draft using previous outreach messages.
- **Rule C (Ghosted):** Notion account `last_edited_time` > 10 days ago → Category: `ghosted`. Flagged in "The Graveyard" section of the report.

**Status guard:** Never downgrades status (uses STATUS_HIERARCHY from notion_cleanup.py)

**Email delivery:** Gmail SMTP_SSL on port 465 with App Password. Sends HTML email body + PDF + CSV attachments. Requires `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `REPORT_RECIPIENT_EMAIL` in `.env`.

**Parser strategies (3 fallbacks):**
1. Data-attribute selectors (`data-view-name="connections-list"`)
2. Class-based selectors
3. Structural parsing (parent-child relationships)

**Important limitations:**
- **Connections only** — LinkedIn messaging pages are JavaScript SPAs that can't be captured via Cmd+S. Only the "My Network > Connections" page works.
- LinkedIn uses hashed/obfuscated CSS class names — no semantic selectors possible for 2026+ layout.

**Scheduled:** Automated via "Save LinkedIn Inbox" Quick Action (or manual `python -m agents.linkedin_manager`)

### 8. Feedback Agent

**Purpose:** Monthly outreach effectiveness analysis and A/B test evaluation. Classifies contact outcomes (success/failure/skip), computes A/B variant statistics, runs GPT-4o pattern analysis, writes learnings to a file that the copywriter agent auto-injects into future prompts, and sends an HTML summary email.

**Location:** `agents/feedback_agent.py`

**Usage:**
```bash
# Full run: analyze outcomes, write learnings, send email
python -m agents.feedback_agent

# Dry run: analyze without writing learnings or sending email
python -m agents.feedback_agent --dry-run

# Lower minimum data threshold (default: 10 resolved outcomes)
python -m agents.feedback_agent --min-data 5
```

**Outcome classification:**
- **SUCCESS:** Contact account status >= "Engaged" (index 7+ in status hierarchy), excluding "Unqualified" (index 12)
- **FAILURE:** Status = "Unqualified" (index 12) or outreach messages stale for 30+ days with no status progression
- **SKIP:** Too early to classify (less than 30 days since outreach, status still in early stages)

**A/B test evaluation:**
- Computes success/failure rates per variant (A vs B)
- Reports statistical confidence when enough data is available
- GPT-4o analyzes which variant performs better and why

**Output:**
- `data/prompts/outreach_learnings.md` — Auto-generated learnings file, overwritten each run. Automatically loaded by the copywriter agent on its next run.
- HTML summary email sent to `FEEDBACK_REPORT_RECIPIENTS` (fallback: `REPORT_RECIPIENT_EMAIL` → `GMAIL_ADDRESS`)

**Minimum data guard:** Requires 10+ resolved outcomes (success + failure) before running GPT-4o analysis. Override with `--min-data`.

**Feedback loop:** Feedback Agent → writes `outreach_learnings.md` → Copywriter Agent loads it → improved messages → Feedback Agent evaluates again next month.

**Scheduled:** 1st of each month at 11am (`com.tumsocialai.feedback-agent`)

**Environment variables:**
- `FEEDBACK_REPORT_RECIPIENTS` — Comma-separated email addresses for the report (optional, falls back to `REPORT_RECIPIENT_EMAIL`)

---

## Apollo Enrichment Workflow

### Overview

Apollo.io is used to enrich qualified leads with contact data (emails, phone numbers, job titles) and company data (funding, employee count, etc.). This is a **semi-automated manual step** between ranking and upload agents.

### Loom Video Tutorial

🎥 **Watch the complete walkthrough:**
[Apollo Enrichment Workflow - Loom Video](https://www.loom.com/share/YOUR_LOOM_ID_HERE)

*(Note: Replace with actual Loom link if available)*

### Step-by-Step Process

#### Step 1: Export Qualified Leads

After running the ranking agent, you'll have:
```
data/tables/weekly_qualified_leads.csv
```

This CSV contains all score >= 5 leads (up to 25 per week).

**Columns:**
```
date_added, company_name, company_domain, person_name, linkedin_url_contact,
trigger, score, reasoning, source
```

#### Step 2: Upload to Apollo

1. **Log in to Apollo.io**
   - Go to https://app.apollo.io
   - Navigate to **People** → **Import**

2. **Upload CSV**
   - Click **Upload CSV**
   - Select `weekly_qualified_leads.csv`
   - Map columns:
     - `company_name` → Company Name
     - `company_domain` → Website
     - `person_name` → Full Name (optional)
     - `linkedin_url_contact` → LinkedIn URL (optional)

3. **Configure Enrichment**
   - Select fields to enrich:
     - ✅ Email
     - ✅ Phone
     - ✅ Job Title
     - ✅ Company LinkedIn
     - ✅ Latest Funding
     - ✅ Latest Funding Amount
     - ✅ # Employees
     - ✅ Industry
   - Click **Start Import**

4. **Wait for Enrichment**
   - Apollo will process your list (usually 5-10 minutes for 25 leads)
   - You'll receive an email when complete

#### Step 3: Review & Qualify Contacts

1. **Open the imported list** in Apollo
2. For each company, Apollo will suggest contacts (usually 2-5 per company)
3. **Review contacts manually:**
   - Check job titles (look for: CEO, Founder, Partnerships Manager, Head of Business Development, CSR Manager)
   - Verify email deliverability status (green = verified)
   - Check LinkedIn profiles to ensure they're the right person
4. **Mark qualified contacts:**
   - Add "Qualify Contact" tag to contacts you want to reach out to
   - **Limit: Max 2 contacts per company** (follow TUM Social AI's rule)

#### Step 4: Export Enriched Data

1. **Select all qualified contacts** in Apollo
2. Click **Export** → **Export as CSV**
3. **Select all columns** (Apollo exports 28 columns by default):
   - First Name, Last Name, Title, Email, Person Linkedin Url
   - Corporate Phone, Apollo Contact Id, Qualify Contact
   - Company Name, Company Name for Emails, Website
   - Company Linkedin Url, City, Company State, Company Country
   - Company Phone, Industry, # Employees
   - Latest Funding, Latest Funding Amount, Apollo Account Id
   - Trigger, Mission, Lead Score
   - *(+ 5 more internal Apollo fields)*

4. **Save as:** `apollo-contacts-export.csv` (or `apollo-contacts-export (N).csv` if multiple exports)

#### Step 5: Upload to Notion

```bash
# Activate virtual environment
source venv/bin/activate  # macOS/Linux
# or
.\venv\Scripts\Activate.ps1  # Windows PowerShell

# Run upload agent
python -m agents.upload_agent --csv "/path/to/apollo-contacts-export.csv"

# Example:
python -m agents.upload_agent --csv "/Users/nicolasemidat/Downloads/apollo-contacts-export (2).csv"
```

**What the upload agent does:**
- Groups contacts by Apollo Account Id
- Creates/updates Notion Accounts DB (20 accounts created, 11 updated in last run)
- Creates Notion Contacts DB linked to accounts (21 contacts created)
- Sets Campaign ID = `Workflow_DDMM` (e.g., `Workflow_0902` for Feb 9)
- Handles status hierarchy (never downgrades accounts with status >= "Engaged")

**Output example:**
```
========================================
         Upload Agent Summary
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ Metric             ┃ Value         ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ Companies          │ 31            │
│ Accounts created   │ 20            │
│ Accounts updated   │ 11            │
│ Contacts created   │ 21            │
│ Contacts skipped   │ 10            │
│ Errors             │ 0             │
│ Campaign ID        │ Workflow_0902 │
└────────────────────┴───────────────┘
```

#### Step 6: Generate Outreach Messages

```bash
# Generate messages for the campaign
python -m agents.copywriter_agent --campaign Workflow_0902

# Preview first (dry run)
python -m agents.copywriter_agent --campaign Workflow_0902 --dry-run
```

**What the copywriter does:**
- Queries Contacts DB filtered by `Campaign ID` rollup
- Resolves linked Account for company context
- Generates 4 messages per contact using GPT-4o
- Writes to Notion: LinkedIn 1st Cold, LinkedIn FU, Cold Email Body, Cold Email Subject

**Output example:**
```
========================================
         Copywriter Summary
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ Metric             ┃ Value         ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ Contacts processed │ 32            │
│ Messages generated │ 32            │
│ Written to Notion  │ 32            │
│ Errors             │ 0             │
│ Campaign filter    │ Workflow_0902 │
└────────────────────┴───────────────┘
```

### Apollo Best Practices

**✅ DO:**
- Manually review all suggested contacts before qualifying
- Prioritize contacts with verified email addresses
- Check LinkedIn profiles to confirm role and company
- Limit to 2 contacts per company (quality > quantity)
- Use Apollo's "Save to Sequence" feature to track outreach progress
- Export immediately after qualifying (don't let lists go stale)

**❌ DON'T:**
- Auto-accept all suggested contacts without review
- Export more than 2-3 contacts per company
- Use unverified email addresses (gray/red status in Apollo)
- Forget to add "Qualify Contact" tag before exporting
- Re-export the same companies without new contacts

### Troubleshooting Apollo Issues

**Issue: Apollo didn't find any contacts for a company**
- Solution: Manually search LinkedIn for the company → identify 1-2 key contacts → add their LinkedIn URLs to Apollo's "Add People" feature

**Issue: All emails are unverified (red status)**
- Solution: Skip this company or find contacts with verified emails. Bounced emails hurt your sender reputation.

**Issue: Upload agent says "0 accounts created" but I exported 25 companies**
- Solution: Likely all companies already exist in Notion. Check Accounts DB for duplicate domains. Run `notion_cleanup.py --merge` to merge duplicates.

**Issue: Campaign ID not showing up in Notion**
- Solution: Notion auto-creates new multi_select options. Refresh the Accounts DB page in browser. If still missing, check that `NOTION_DB_ACCOUNTS_ID` in `.env` is correct.

---

## Notion Integration

### Database Structure

#### Accounts Database

**Primary fields (used by agents):**
- `Organization*` (title) — Company name
- `Website URL*` (url) — Company domain
- `Status` (status) — Pipeline stage (Suspect → Prospect Qualified → Contacted LinkedIn → ... → Engaged → Closed Won/Lost)
- `Campaign ID` (multi_select) — `Workflow_DDMM` tags
- `Trigger Event` (rich_text) — Why we're reaching out (append-only)
- `Mission*` (rich_text) — Company mission / GPT-4o reasoning
- `Account Type*` (select) — NGO / Corporate / Academic / Student Club
- `Industry (Corporates)` (rich_text) — Industry sector
- `City` (select) — City
- `Country` (multi_select) — Country
- `# Employees` (number) — Employee count
- `Latest Funding` (rich_text) — Funding round (e.g., "Series A")
- `Funding Amount` (number) — Funding amount in USD
- `Lead Score` (number) — GPT-4o score (0-10)
- `Apollo Account ID` (rich_text) — Apollo Account Id
- `Company LinkedIn` (url) — Company LinkedIn URL
- `Cleaned Name*` (rich_text) — Company Name for Emails (Apollo)
- `Company Phone Number` (phone_number) — Company phone

**Relations:**
- `Contacts` → Contacts DB (one-to-many)

#### Contacts Database

**Primary fields (used by agents):**
- `Contact Name` (title) — Full name (First + Last)
- `Email` (email) — Email address
- `LinkedIn` (url) — Personal LinkedIn URL
- `Job Title` (rich_text) — Job title
- `Phone` (phone_number) — Corporate phone
- `Apollo Contact ID` (rich_text) — Apollo Contact Id

**Outreach fields (generated by copywriter):**
- `LinkedIn 1st Cold` (rich_text) — Initial LinkedIn message
- `LinkedIn FU message` (rich_text) — Follow-up LinkedIn message
- `Cold Email Subject` (rich_text) — Email subject line
- `Cold Email Body` (rich_text) — Email body

**Relations:**
- `Accounts` → Accounts DB (many-to-one)

**Rollups:**
- `Campaign Source` — Rollup of `Accounts.Campaign ID` (shows which campaign(s) this contact came from)

### Notion Status Hierarchy

Used for merge decisions and status guards:

```python
STATUS_HIERARCHY = [
    "Suspect",                    # 0 — Lowest
    "Prospect Qualified",          # 1
    "Contacted Email 📧",          # 2
    "Contacted LinkedIn 🌐",       # 3
    "Contacted Phone 📞",          # 4
    "Contacted Physical Mail 📬",  # 5
    "First Call done",             # 6
    "Engaged",                     # 7 — ENGAGED THRESHOLD
    "Qualified Opportunity",       # 8
    "Partnership Agreement sent",  # 9
    "Closed Won 🎉",              # 10
    "Closed Lost",                 # 11
    "Prospect Unqualified",        # 12 — Highest (terminal)
]
```

**Key rules:**
- **Status >= "Engaged" (index 7+)** → Blocks re-ranking and re-export
- **Never downgrade status** — Always use higher status in merge operations
- **Status guards** in upload agent and linkedin manager prevent accidental downgrades

---

## Daily Operations

### Typical Weekly Workflow

#### Monday Morning (Automated)
- **9am:** Collector agent runs (Mon/Wed/Fri schedule)
- Processes any new screenshots, LinkedIn URLs, or manual contacts from weekend
- Outputs updated `master_input.csv`

#### Tuesday Morning (Automated)
- **10am:** Ranking agent runs (Tue/Thu schedule)
- Scores all `pending` + `backlog` leads
- Exports top 25 qualified leads to `weekly_qualified_leads.csv`
- Remaining score >= 5 leads → `backlog.csv`

#### Tuesday Afternoon (Manual)
1. **Upload to Apollo** (15 min)
   - Download `weekly_qualified_leads.csv`
   - Upload to Apollo.io
   - Wait for enrichment (5-10 min)

2. **Review & Qualify Contacts** (30 min)
   - Review Apollo's suggested contacts
   - Mark qualified contacts (max 2 per company)
   - Check email deliverability

3. **Export from Apollo** (5 min)
   - Export qualified contacts as CSV
   - Save as `apollo-contacts-export.csv`

4. **Upload to Notion** (5 min)
   ```bash
   python -m agents.upload_agent --csv "/path/to/apollo-contacts-export.csv"
   ```

5. **Generate Outreach Messages** (10 min)
   ```bash
   python -m agents.copywriter_agent --campaign Workflow_0209
   ```

#### Wednesday-Friday
- **Wed 9am:** Collector runs (processes midweek leads)
- **Thu 10am:** Ranking runs (second weekly ranking)
- Repeat Apollo workflow if you want a second batch this week (optional)

#### Saturday Morning (Automated)
- **9am:** Supervisor agent runs
- Generates weekly PDF audit report
- Review API costs and pipeline metrics

#### Bi-Monthly (1st & 15th)
- **10am:** Notion cleanup agent runs
- Populates missing domains and account types
- Interactive duplicate merge (if any detected)

#### Monthly (1st)
- **11am:** Feedback agent runs
- Classifies outreach outcomes (success/failure/skip)
- Evaluates A/B test variant performance
- Writes learnings to `data/prompts/outreach_learnings.md` (auto-injected by copywriter)

### Manual Tasks Checklist

**Daily (5 min):**
- [ ] Check `data/inputs/images/new/` for any failed screenshot extractions
- [ ] Review `data/logs/api_usage.jsonl` for unusual API cost spikes

**Weekly (1 hour):**
- [ ] Apollo enrichment workflow (see above)
- [ ] Review supervisor PDF report
- [ ] Check Notion Accounts DB for any "Engaged" status leads (requires follow-up)

**Bi-Monthly (30 min):**
- [ ] Run `notion_cleanup.py --all` to merge duplicates
- [ ] Review `data/logs/merge_log.jsonl` for merge decisions

**Monthly (1 hour):**
- [ ] Audit API costs (OpenAI usage)
- [ ] Review pipeline conversion rates (qualified → contacted → engaged)
- [ ] Update outreach skill prompt if needed (`data/prompts/outreach_skill.md`)
- [ ] Review feedback agent email report (auto-sent 1st of month)
- [ ] Check `data/prompts/outreach_learnings.md` for quality of auto-generated learnings

---

## Troubleshooting

### Common Issues & Solutions

#### Collector Agent

**Issue: Screenshot extraction returns empty company_name**
- **Cause:** Screenshot is too blurry, cropped, or doesn't contain LinkedIn content
- **Solution:** Retake screenshot, ensure full post is visible, move to `processed/` manually

**Issue: Domain verification fails (all attempts return None)**
- **Cause:** Company website is down or blocks automated requests
- **Solution:** Manually add domain to CSV, or skip domain (collector allows missing domains)

**Issue: Watchdog not triggering on new files**
- **Cause:** macOS Spotlight indexing or Google Drive sync delay
- **Solution:** Wait 10-15 seconds after file copy, or run collector manually with `python -m agents.collector`

#### Ranking Agent

**Issue: All leads get score = 0**
- **Cause:** OpenAI API key invalid or quota exceeded
- **Solution:** Check `.env` file, verify API key at https://platform.openai.com/api-keys, check billing

**Issue: Leads score high but aren't exported (still in `pending`)**
- **Cause:** Notion dedup blocked them (status >= "Engaged")
- **Solution:** Check Notion Accounts DB for existing domains. If duplicate, decide whether to keep or merge.

**Issue: Backlog keeps growing, never exports**
- **Cause:** Weekly cap (25 leads) hit every week
- **Solution:** Increase weekly cap in `ranking_agent.py` (`MAX_WEEKLY_EXPORT = 25` → adjust as needed), or manually export backlog separately

#### Upload Agent

**Issue: "0 accounts created" but CSV has 25 rows**
- **Cause:** All companies already exist in Notion (domain match)
- **Solution:** Expected behavior. Check "Accounts updated" count. Run with `--force` if you want to overwrite (not implemented yet).

**Issue: Contacts not linking to accounts (relation is empty)**
- **Cause:** Account lookup failed (domain mismatch)
- **Solution:** Manually verify domains in Notion Accounts DB match Apollo CSV `Website` column. Run `notion_cleanup.py --domains` to populate missing domains.

**Issue: Campaign ID not showing in Notion**
- **Cause:** Notion takes 5-10 seconds to sync multi_select options
- **Solution:** Refresh Accounts DB page in browser. Check Notion API status at https://status.notion.so

#### Copywriter Agent

**Issue: "Found 0 contacts needing messages" but I just uploaded 32 contacts**
- **Cause:** Contacts already have messages (from previous run)
- **Solution:** Use `--force` flag to regenerate: `python -m agents.copywriter_agent --campaign Workflow_0902 --force`

**Issue: Messages are generic / not personalized**
- **Cause:** Account data is missing (empty `Mission`, `Trigger Event`, `Industry`)
- **Solution:** Re-run ranking agent with `--force` to regenerate reasoning, or manually populate Notion fields

**Issue: German messages use "Sie" instead of "du"**
- **Cause:** Old skill prompt (before Feb 9 update)
- **Solution:** Re-run with `--force` flag. Check `data/prompts/outreach_skill.md` line 265 says "ALWAYS use 'du'"

**Issue: Email subject lines are all lowercase**
- **Cause:** Old skill prompt (before Feb 9 update)
- **Solution:** Re-run with `--force` flag. Check skill prompt line 230 says "Title Case"

#### Notion Cleanup Agent

**Issue: Domain population fails with "Could not verify domain"**
- **Cause:** GPT-4o suggested domain is invalid or blocked
- **Solution:** Manually add domain to Notion, or skip this account

**Issue: Merge prompt doesn't show up (no duplicates detected)**
- **Cause:** No duplicate domains found, or all domains are empty
- **Solution:** Run `--domains` first to populate missing domains, then run `--merge`

**Issue: Merge deleted the wrong account (lost data)**
- **Cause:** Status hierarchy chose lower-status account as primary
- **Solution:** Check `data/logs/merge_log.jsonl`, restore from backup (Notion keeps 30-day history), manually re-enter data

#### Supervisor Agent

**Issue: PDF report is empty / missing charts**
- **Cause:** `api_usage.jsonl` is empty or malformed
- **Solution:** Check `data/logs/api_usage.jsonl` exists and has valid JSON lines. Re-run agents to generate API usage logs.

**Issue: Cost calculations are wrong**
- **Cause:** GPT-4o pricing changed (currently $2.50/1M input, $10/1M output)
- **Solution:** Update pricing in `agents/supervisor.py` lines 200-210

### Debug Mode

Enable verbose logging for any agent:

```bash
# Set debug environment variable
export DEBUG=1  # macOS/Linux
# or
set DEBUG=1  # Windows

# Run agent
python -m agents.collector
```

This will print detailed logs to console, including:
- API request/response bodies
- File operations
- Notion queries
- GPT-4o prompts and completions

### Log Files

**API usage log:**
```
data/logs/api_usage.jsonl
```
Each line is a JSON object:
```json
{
  "timestamp": "2026-02-09T20:37:10",
  "agent": "copywriter_agent",
  "action": "generate_outreach",
  "model": "gpt-4o",
  "prompt_tokens": 1234,
  "completion_tokens": 567,
  "total_tokens": 1801,
  "cost_usd": 0.00868,
  "metadata": {"contact": "Leon Hergert", "company": "Spherecast"}
}
```

**Merge log:**
```
data/logs/merge_log.jsonl
```
Each line is a JSON object:
```json
{
  "timestamp": "2026-02-09T10:15:00",
  "action": "merge",
  "primary_id": "266a0c6e-...",
  "duplicate_id": "266a0c6e-...",
  "primary_before": {...},
  "duplicate_before": {...},
  "primary_after": {...},
  "relations_moved": 3
}
```

### Getting Help

**GitHub Issues:**
- Report bugs: https://github.com/tumsocialai/sales-agent/issues
- Feature requests: https://github.com/tumsocialai/sales-agent/discussions

**Internal Contact:**
- Nicolas Paul (Co-Founder, TUM Social AI)
- Email: nicolas@tum-socialaiclub.de

---

## Appendix

### A. File & Directory Reference

```
tum_sales_agent/
├── agents/
│   ├── collector.py           # Lead aggregation (Mon/Wed/Fri 9am)
│   ├── ranking_agent.py       # GPT-4o scoring (Tue/Thu 10am)
│   ├── upload_agent.py        # Apollo CSV → Notion (manual)
│   ├── copywriter_agent.py    # Outreach generation (manual)
│   ├── supervisor.py          # Weekly PDF report (Sat 9am)
│   ├── notion_cleanup.py      # Duplicate merge (1st/15th 10am)
│   ├── linkedin_parser.py     # LinkedIn connections HTML parser
│   ├── linkedin_manager.py    # LinkedIn analysis orchestrator
│   └── report_generator.py    # LinkedIn PDF report
├── utils/
│   ├── config.py              # Environment vars, paths, CSV schema
│   ├── notion_client.py       # Notion API wrapper (900+ lines)
│   ├── api_logger.py          # OpenAI API usage tracker
│   └── apollo_client.py       # Legacy Apollo API (deprecated)
├── data/
│   ├── inputs/
│   │   ├── images/new/        # Drop screenshots here
│   │   ├── images/processed/  # Processed screenshots
│   │   ├── linkedin_urls/new/ # Drop LinkedIn post URLs
│   │   ├── linkedin_urls/processed/
│   │   ├── manual_contacts/new/ # Drop manual contact files
│   │   ├── manual_contacts/processed/
│   │   └── linkedin_dump/     # Saved LinkedIn connections HTML
│   ├── tables/
│   │   ├── master_input.csv   # All leads (11 columns)
│   │   ├── weekly_qualified_leads.csv # Export to Apollo
│   │   └── backlog.csv        # Overflow leads
│   ├── logs/
│   │   ├── api_usage.jsonl    # OpenAI API log
│   │   └── merge_log.jsonl    # Notion merge decisions
│   ├── reports/
│   │   ├── supervisor_report_YYYYMMDD.pdf
│   │   ├── weekly_linkedin_report_YYYYMMDD.pdf
│   │   └── linkedin_outreach_actions_YYYYMMDD.csv  # Copy-paste ready outreach actions
│   └── prompts/
│       └── outreach_skill.md  # GPT-4o copywriter system prompt
├── scripts/
│   ├── run_collector.sh       # Wrapper for scheduled collector
│   ├── run_ranking.sh         # Wrapper for scheduled ranking
│   ├── run_notion_cleanup.sh  # Wrapper for scheduled cleanup
│   └── save_linkedin.sh       # Saves LinkedIn HTML + runs linkedin_manager
├── venv/                      # Python virtual environment
├── .env                       # Environment variables (DO NOT COMMIT)
├── .env.template              # Template for .env
├── requirements.txt           # Python dependencies
├── CLAUDE.md                  # Claude Code project instructions
└── ONBOARDING.md              # This file
```

### B. Environment Variables Reference

```bash
# OpenAI API
OPENAI_API_KEY=sk-proj-...                    # Required

# Notion API
NOTION_TOKEN=secret_...                        # Required
NOTION_DB_ACCOUNTS_ID=266a0c6e-6168-80c1-...   # Required
NOTION_DB_CONTACTS_ID=266a0c6e-6168-80c2-...   # Required

# Gmail (for LinkedIn agent email delivery)
GMAIL_ADDRESS=your-email@gmail.com             # Required for LinkedIn agent
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx         # App Password (NOT regular password)
REPORT_RECIPIENT_EMAIL=recipient@example.com   # Where LinkedIn reports are sent
RANKING_REPORT_RECIPIENTS=a@example.com,b@example.com  # Comma-separated ranking report recipients

# Optional (legacy)
APOLLO_API_KEY=...                             # Not used anymore
NOTION_DB_QUALIFIED_ID=...                     # Deprecated
```

### C. Python Dependencies

```
openai>=1.0.0           # GPT-4o API with structured output
requests>=2.31.0        # HTTP library
watchdog>=3.0.0         # File system monitoring
rich>=13.0.0            # Terminal output formatting
fpdf>=1.7.2             # PDF generation
beautifulsoup4>=4.12.0  # HTML parsing
pydantic>=2.0.0         # Data validation
pillow>=10.0.0          # Image processing
python-dotenv>=1.0.0    # .env file loading
```

### D. Notion API Permissions

Your Notion integration must have these capabilities:

**Required:**
- ✅ Read content
- ✅ Update content
- ✅ Insert content

**Database access:**
- ✅ Accounts DB (shared)
- ✅ Contacts DB (shared)

**How to share databases:**
1. Open database in Notion
2. Click **Share** (top-right)
3. Click **Invite**
4. Select your integration
5. Click **Invite**

### E. macOS Scheduling (launchd)

**Installed agents:**
- `com.tumsocialai.sales-collector` — Mon/Wed/Fri 9am
- `com.tumsocialai.sales-ranking` — Tue/Thu 10am
- `com.tumsocialai.sales-supervisor` — Sat 9am
- `com.tumsocialai.notion-cleanup` — 1st & 15th 10am
- `com.tumsocialai.feedback-agent` — 1st of month 11am

**Locations:**
```
~/Library/LaunchAgents/com.tumsocialai.sales-collector.plist
~/Library/LaunchAgents/com.tumsocialai.sales-ranking.plist
~/Library/LaunchAgents/com.tumsocialai.sales-supervisor.plist
~/Library/LaunchAgents/com.tumsocialai.notion-cleanup.plist
~/Library/LaunchAgents/com.tumsocialai.feedback-agent.plist
```

**Commands:**
```bash
# Load agent
launchctl load ~/Library/LaunchAgents/com.tumsocialai.sales-collector.plist

# Unload agent
launchctl unload ~/Library/LaunchAgents/com.tumsocialai.sales-collector.plist

# Run immediately (test)
launchctl start com.tumsocialai.sales-collector

# Check status
launchctl list | grep tumsocialai

# View logs
tail -f /tmp/tum_sales_collector.log
```

### F. macOS Quick Actions (Automator)

Four Quick Actions available in Finder's right-click menu (Services):

| Quick Action | What It Does |
|---|---|
| **Add LinkedIn URL** | Saves clipboard LinkedIn post URL to `data/inputs/linkedin_urls/new/` |
| **Take Lead Screenshot** | Saves current screen to `data/inputs/images/new/` |
| **Add Lead Contact** | Saves clipboard manual contact to `data/inputs/manual_contacts/new/` |
| **Save LinkedIn Inbox** | Saves current LinkedIn connections page HTML, then runs `linkedin_manager` automatically. Delivers email with PDF + CSV report. |

**How to use "Save LinkedIn Inbox":**
1. Open LinkedIn → My Network → Connections in your browser
2. Right-click anywhere → Quick Actions → "Save LinkedIn Inbox"
3. The workflow saves the HTML page and immediately runs the LinkedIn analysis
4. Within 1-2 minutes you receive an email with the outreach report

**Location:** `~/Library/Services/*.workflow`

**Troubleshooting:** If a Quick Action shows "not configured correctly", open it in Automator.app, re-save, and run `/System/Library/CoreServices/pbs -flush` to refresh the services cache.

### G. Windows Scheduling (Task Scheduler)

**Not yet implemented.** Manual runs only.

**Planned implementation:**
1. Create `.bat` files for each agent
2. Use Windows Task Scheduler to run .bat files on schedule
3. Logs saved to `C:\Users\your-username\AppData\Local\TUMSalesAgent\logs\`

**Example .bat file:**
```batch
@echo off
cd "C:\Users\your-username\tum_sales_agent"
call venv\Scripts\activate.bat
python -m agents.collector
pause
```

### H. Linux Scheduling (cron)

**Not yet implemented.** Manual runs only.

**Planned implementation:**
```bash
# Edit crontab
crontab -e

# Add these lines (adjust paths):
0 9 * * 1,3,5 cd /home/user/tum_sales_agent && ./venv/bin/python -m agents.collector
0 10 * * 2,4 cd /home/user/tum_sales_agent && ./venv/bin/python -m agents.ranking_agent
0 9 * * 6 cd /home/user/tum_sales_agent && ./venv/bin/python -m agents.supervisor
0 10 1,15 * * cd /home/user/tum_sales_agent && ./venv/bin/python -m agents.notion_cleanup --all
```

### I. GPT-4o Pricing (as of Feb 2026)

| Model | Input | Output |
|---|---|---|
| GPT-4o | $2.50 / 1M tokens | $10.00 / 1M tokens |
| GPT-4o-mini | $0.15 / 1M tokens | $0.60 / 1M tokens |

**Typical costs per agent run:**
- Collector (screenshot extraction): $0.05 - $0.15 per screenshot
- Ranking (lead scoring): $0.10 - $0.30 per 25 leads
- Copywriter (outreach generation): $0.50 - $1.50 per 32 contacts
- Notion Cleanup (domain resolution): $0.05 - $0.10 per account

**Monthly estimate (active usage):**
- ~$50-100 per month for typical TUM Social AI volume (100-200 leads/month)

### J. Quick Reference Commands

```bash
# Activate virtual environment
source venv/bin/activate  # macOS/Linux
.\venv\Scripts\Activate.ps1  # Windows

# Collect leads (one-time)
python -m agents.collector

# Collect leads (watch mode)
python -m agents.collector --watch

# Rank leads
python -m agents.ranking_agent

# Upload Apollo CSV
python -m agents.upload_agent --csv "/path/to/apollo-contacts-export.csv"

# Generate outreach
python -m agents.copywriter_agent --campaign Workflow_0902

# Preview outreach (dry run)
python -m agents.copywriter_agent --campaign Workflow_0902 --dry-run

# Regenerate all outreach (overwrite)
python -m agents.copywriter_agent --campaign Workflow_0902 --force

# Generate weekly report
python -m agents.supervisor

# Notion cleanup (all)
python -m agents.notion_cleanup --all

# Notion cleanup (domains only)
python -m agents.notion_cleanup --domains

# Notion cleanup (merge only)
python -m agents.notion_cleanup --merge

# Process LinkedIn connections (full: Notion updates + email)
python -m agents.linkedin_manager

# Process LinkedIn connections (dry run: no Notion updates, no email)
python -m agents.linkedin_manager --dry-run

# Monthly outreach feedback analysis
python -m agents.feedback_agent

# Feedback analysis (dry run: no learnings file, no email)
python -m agents.feedback_agent --dry-run

# Feedback analysis with lower data threshold
python -m agents.feedback_agent --min-data 5
```

---

## Changelog

### Version 2.1 (Feb 10, 2026)
- ✅ LinkedIn Agent: Gmail email delivery with PDF + CSV attachments
- ✅ LinkedIn Agent: CSV export with copy-paste-ready outreach messages (single-line, Numbers/Excel compatible)
- ✅ LinkedIn Agent: Rule A expanded to detect any status < "Contacted LinkedIn" (not just exact "Connect. Request sent")
- ✅ LinkedIn Agent: Connections-only approach (LinkedIn messaging pages are JS SPAs, not capturable via Cmd+S)
- ✅ Copywriter: Never mix languages within a single message
- ✅ Copywriter: Never use em dashes or en dashes, use commas instead
- ✅ Copywriter: Always say "non-profits", never "NGOs"
- ✅ Copywriter: Added winning pattern (talent access / student network relevance framing)
- ✅ Copywriter: A/B testing (50/50 random variant per contact, stored in Notion `AB Variant` select)
- ✅ Copywriter: Persona-aware smart hooks — fetches careers pages for talent/HR/founder/marketing personas, question-framed hooks that connect naturally to pitch
- ✅ Copywriter: Learnings injection from feedback agent (`data/prompts/outreach_learnings.md`)
- ✅ Feedback Agent: Monthly outreach effectiveness analysis + A/B test evaluation (new)
- ✅ Feedback Agent: GPT-4o pattern analysis, outcome classification (success/failure/skip)
- ✅ Feedback Agent: Auto-generates learnings file for copywriter injection
- ✅ Feedback Agent: Scheduled 1st of month 11am (`com.tumsocialai.feedback-agent`)
- ✅ macOS Quick Actions: 4 Automator workflows (Add LinkedIn URL, Take Lead Screenshot, Add Lead Contact, Save LinkedIn Inbox)
- ✅ Save LinkedIn Inbox automation: saves HTML + runs linkedin_manager + emails report in one click

### Version 2.0 (Feb 9, 2026)
- ✅ Upload Agent: Apollo CSV → Notion Accounts + Contacts (complete)
- ✅ Copywriter Agent: 4 personalized outreach messages per contact (complete)
- ✅ Outreach skill prompt: Data-backed tone-of-voice (Gong, Lavender, Winning by Design)
- ✅ Multi-language support: German (DACH), Spanish (ES/LATAM), English
- ✅ Trigger hierarchy: AA through E tiers
- ✅ Contact-level outreach properties (moved from Accounts to Contacts DB)
- ✅ `--force` flag for copywriter (overwrite existing messages)
- ✅ Status hierarchy guard (never downgrade)
- ✅ Campaign ID system: `Workflow_DDMM`

### Version 1.0 (Feb 6, 2026)
- ✅ Collector Agent: 3-stream lead aggregation
- ✅ Ranking Agent: GPT-4o lead scoring
- ✅ Supervisor Agent: Weekly PDF audit reports
- ✅ Notion Cleanup Agent: Domain population + duplicate merge
- ✅ LinkedIn Analyst Agent: LinkedIn connections parser
- ✅ macOS scheduling: launchd agents
- ✅ API cost tracking: `api_usage.jsonl`

---

**Questions or feedback?**
Reach out to Nicolas Paul at nicolas@tum-socialaiclub.de or open an issue on GitHub.

**Built with ❤️ by TUM Social AI**
https://tum-socialaiclub.de
