# TUM Social AI — Strategic Partnerships Agents
## On-Demand Campaign Onboarding Guide

**Last updated:** June 5, 2026  
**Audience:** TUM Social AI teammates running partnership campaigns from Codex, Claude Code, Antigravity, or a terminal.

---

## 1. What This Infrastructure Does

This repo turns raw partnership signals into ready-to-send outreach:

1. **Collect input** from LinkedIn screenshots, LinkedIn URLs, and manual contact notes.
2. **Create a top leads report on demand** when the team wants a new campaign shortlist.
3. **Deduplicate and split before export** into companies with contacts and companies without contacts.
4. **Run Apollo enrichment review** with `python agent.py apollo-enrich`.
5. **Upload the strict Apollo-ready CSV into Notion** with an explicit campaign sender.
6. **Generate outreach copy** for that sender, then assign owners by account.
7. **Review and send manually** from each teammate's labeled Gmail folder.
8. **Review LinkedIn connection/follow-up actions** when requested.

The system is intentionally no longer a fixed weekly campaign machine. Campaign actions should happen when someone deliberately asks for them.

### What Each Agent Is Built On

| Agent / flow | What it does | Criteria and logic |
|---|---|---|
| `collector` | Aggregates lead signals into `data/tables/master_input.csv`. | Uses three input streams: LinkedIn screenshots in `data/inputs/images/`, LinkedIn post URLs in `data/inputs/linkedin_urls/`, and manual contact CSVs/notes in `data/inputs/manual_contacts/`. GPT-4o Vision extracts company/person/context from screenshots. LinkedIn URL processing extracts entities, domains, roles, and the specific trigger event. Deduplication uses normalized domain + person name, LinkedIn profile URLs, and a per-company cap so the master table stays campaign-safe. |
| `ranking_agent` | Creates the on-demand top leads report. | Uses GPT-4o structured scoring from 0-10. The strongest fit is a company similar to proven pipeline successes, with positive social/ecological impact, AI or AI-talent relevance, and evidence of engaging with student organizations in Germany. DACH presence is only a tiebreaker, not a hard requirement. Leads with score >= 5 qualify for the shortlist. |
| `ranking_agent` filters | Prevents obvious bad leads from entering campaigns. | Student clubs, university associations, other student initiatives, very early startups, companies with no AI/impact/ecological angle, traditional finance, gambling, tobacco/alcohol, weapons/defense, event services, catering, and similar support vendors are disqualified or heavily penalized. |
| `apollo_enrichment_agent` | Adds verified company/contact data before Notion upload. | The ranker writes with-contact, no-contact, and joint Apollo-ready CSVs. The Apollo flow creates `apollo_enrichment_batches.json`, merges Apollo connector/session-log or UI export results into `apollo_enriched_contacts_for_review.csv`, flags senior marketing/recruiting/people contacts that still need a real mobile number, and emits `apollo_upload_ready.csv` with only safe import rows. |
| `upload_agent` | Uploads Apollo CSVs into Notion Accounts and Contacts. | Requires an explicit campaign sender. It patches/validates required Notion properties, deduplicates accounts by Apollo Account ID/domain/name, deduplicates contacts by email/LinkedIn/name, links Contacts to Accounts, writes campaign ID, sender, account metadata, contact metadata, and safely updates existing records without resetting useful pipeline status unless intended. |
| `copywriter_agent` | Generates campaign-specific outreach in Notion **and creates Gmail drafts**. | Uses the shared outreach skill prompt plus processed `data/prompts/outreach_learnings.md`, campaign sender, contact/account context, trigger event, company mission, employee/funding context, and sometimes careers-page context. It writes LinkedIn first cold, LinkedIn follow-up, cold email subject, and cold email body to Notion — and automatically creates a Gmail draft in `partnerships@tum-socialaiclub.de` for every contact with an email address. The team reviews and sends drafts manually. Copy is short, English, specific to the trigger, sender-aware, and constrained against invented facts. |
| `owner_assignment_agent` | Splits campaign ownership by account after drafts exist. | Runs `python scripts/assign_partnership_outreach.py --apply`. It balances the current campaign across Timon, Felix, Till, and Nicolas; future campaigns rotate only across Timon, Felix, and Till. One account has one sender, and every contact under that account gets the same Notion `Contact Owner*`, `Campaign Sender`, and draft sender signature. Gmail labels can be applied after OAuth has label/modify scopes. |
| `linkedin_manager` | Reviews LinkedIn connection/follow-up actions. | Parses saved LinkedIn connections HTML, matches LinkedIn URLs to Notion Contacts/Accounts, detects new connections, identifies follow-up needs after 3-5 days, marks ghosted leads after the configured window, drafts follow-up text, and avoids downgrading Notion statuses through a status hierarchy guard. |
| `feedback_agent` | Turns outcome data and manual copywriter iterations into prompt learnings. | Reads resolved outcomes, analyzes A/B test results, scans the Notion Iterations page, distills reusable guidance into `data/prompts/outreach_learnings.md`, and moves processed iteration notes into the Processed section. |

### Strategic Ranking Criteria Details

| Criterion | High score means | Medium score means | Low/disqualify means |
|---|---|---|---|
| Impact fit | Clear social, humanitarian, ecological, or innovation mission; mission aligns with AI for Good. | General tech/innovation relevance but weaker impact story. | No impact, AI, or ecological connection. |
| AI / AI-talent relevance | Uses AI, sells AI, needs AI builders, or has AI initiatives where TUM Social AI talent is credible. | Tech-forward but not clearly AI-led. | Traditional industry with no innovation or AI angle. |
| Student ecosystem signal | Collaborates with student organizations, hires interns/juniors, sponsors hackathons, joins pitch events, or appears in Munich/DACH tech events. | Some event/startup ecosystem signal but not student-specific. | Student organization itself, university club, or peer initiative. These score 0. |
| Similarity to proven pipeline wins | Looks like companies that already reached engaged or further in Workflow campaigns. | Similar sector but weaker trigger or less obvious budget/talent need. | No resemblance to historical successes. |
| Timing / trigger | Strong reason to reach out now: event, hiring, funding, program launch, partnership, speaker role, or campaign-relevant news. | Generic but plausible company context. | No trigger and no clear reason for outreach. |

Human review still matters. The ranking agent creates a campaign shortlist, not a final partnership decision.

---

## 2. What You Need

You do **not** need coding knowledge. You need:

- One agentic coding environment: **Codex**, **Claude Code**, or **Antigravity**.
- A local clone of this repository.
- Python 3.9+.
- Access to the shared Notion workspace and the required databases.
- An OpenAI API key or a vibe-coding environment license. Ask Nicolas if you need to use his OpenAI credits.
- Optional: Gmail app password if you want email reports sent from your laptop.

### API Cost Basics

- **Running existing agents** is usually low cost. Costs mostly come from OpenAI calls during lead scoring, copywriting, feedback analysis, and screenshot parsing.
- **Changing or building agents** costs more because your coding environment uses model calls while editing, debugging, and testing.
- The agents currently use OpenAI API credits for their own AI work. The coding environment may have separate billing depending on Codex, Claude Code, or Antigravity.

---

## 3. Setup From GitHub

### macOS / Linux

```bash
git clone https://github.com/nicopaul19/tum-sales-agent.git
cd tum-sales-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.template .env
```

### Windows PowerShell

```powershell
git clone https://github.com/nicopaul19/tum-sales-agent.git
cd tum-sales-agent
py -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.template .env
```

### Required `.env` Values

```bash
OPENAI_API_KEY=
NOTION_TOKEN=
NOTION_DB_ACCOUNTS_ID=
NOTION_DB_CONTACTS_ID=
GMAIL_ADDRESS=
GMAIL_APP_PASSWORD=
REPORT_RECIPIENT_EMAIL=
RANKING_REPORT_RECIPIENTS=
FEEDBACK_REPORT_RECIPIENTS=
DEFAULT_CAMPAIGN_SENDER=
```

`DEFAULT_CAMPAIGN_SENDER` is optional. If it is empty, upload and copywriter commands ask who will execute the campaign. In non-interactive runs, pass `--sender "Full Name"`.

Important: the Contacts database ID is correct, but the current Notion integration returns a 404 unless the Contacts DB is shared with the integration. Share both Accounts and Contacts with the integration named **Claude Code Integration** or the active Notion integration token.

### Verify Setup

```bash
python agent.py collect
python agent.py --help
```

---

## 4. Command-First Campaign Lifecycle

Use `agent.py` for all environments. It works in Codex, Claude Code, Antigravity, macOS Terminal, and Windows PowerShell.

| Step | When | Command |
|---|---|---|
| Process new inputs | After saving screenshots, URLs, or manual leads | `python agent.py collect` |
| Create top leads report | When the team wants a new campaign shortlist | `python agent.py rank` |
| Prepare Apollo enrichment review | After ranking / Apollo connector enrichment | `python agent.py apollo-enrich --mcp-json "/path/to/session.jsonl"` |
| Upload Apollo-ready CSV | After reviewing `apollo_enriched_contacts_for_review.csv` | `python agent.py upload --csv "data/tables/apollo_upload_ready.csv" --sender "Full Name"` |
| Generate outreach | After upload | `python agent.py copywrite --campaign Workflow_DDMM --sender "Full Name"` |
| Assign owners and sender folders | After drafts exist | `python scripts/assign_partnership_outreach.py --apply` |
| LinkedIn review | After saving LinkedIn network HTML | `python agent.py linkedin --connections-file "/path/to/network.html"` |
| Infrastructure audit | On request | `python agent.py supervisor` |
| Feedback analysis | On request, after enough outcomes | `python agent.py feedback` |
| Notion cleanup | On request | `python agent.py cleanup --all` |

### Campaign Sender

Every campaign needs a sender because the copy should sound like the teammate who will actually send it.

Examples:

```bash
python agent.py apollo-enrich --mcp-json "session-or-apollo-output.jsonl"
python agent.py upload --csv "data/tables/apollo_upload_ready.csv" --sender "Nicolas Paul"
python agent.py copywrite --campaign Workflow_0505 --sender "Nicolas Paul"
```

The generated messages use:

- LinkedIn sign-off: `Best, {first_name}`
- Email sign-off: `{full_name}`

### Owner Split, Labels, And Manual Sending

After drafts are created, run:

```bash
python scripts/assign_partnership_outreach.py --apply
```

For the current 87-message campaign, the script splits ownership across Timon, Felix, Till, and Nicolas as evenly as possible while keeping all contacts from the same account with the same owner. For future strategic partnerships campaigns, use only Timon, Felix, and Till as the owner rotation.

The ownership rule is strict:

- One account has exactly one responsible sender.
- All contacts related to that account inherit the same `Contact Owner*`.
- Notion `Account Owner`, `Contact Owner*`, `Campaign Sender`, email body, and email signature must match the person responsible for sending.
- Draft review remains manual: each teammate opens their own folder under `Strategic Partnerships`, reviews the messages, and sends on the assigned day.

If Gmail label application fails with an insufficient-scope error, regenerate the local token once:

```bash
rm gmail_token.json
python setup_gmail_auth.py
python scripts/assign_partnership_outreach.py --labels-only
```

---

## 5. Operating Cadence

Put these as recurring calendar blockers, but execute campaign actions only when useful.

| Cadence | Blocker | Action |
|---|---|---|
| Continuous | LinkedIn input capture | Save promising posts, profiles, screenshots, and manual contacts as you browse. |
| Continuous | Trigger sourcing | Actively scrape LinkedIn posts, hiring pages, hackathon/sponsorship pages, event pages, accelerator/news posts, and other sources where a company shows a fresh reason to talk. Send strong trigger events and contact ideas to Jaron so they can be included in upcoming campaigns. |
| On campaign start | Input cleanup + top leads + Apollo | Run `python agent.py collect`, scan inputs, run `python agent.py rank`, run Apollo search/enrichment through the connector, then normalize with `python agent.py apollo-enrich`. |
| After Apollo | Upload + copywriter | Run upload with `--sender`, then copywrite for the campaign. |
| Campaign launch | Owner split + Slack launch note | Assign owners, post the launch message in `#strategic-partnerships`, and split sending over four days for four senders or three days for three senders. Keep the team at 20-30 sent outreach emails per day total to protect deliverability. |
| 1x per week during active campaign | LinkedIn follow-up review | Save the LinkedIn connections page and run `python agent.py linkedin --connections-file ...`. |
| After outcomes accumulate | Copywriter feedback | Add examples to the Notion improvement log; run `python agent.py feedback` when there is enough data. |

Recommended calendar blocks:

- **60 min input cleanup + campaign shortlist + Apollo enrichment**
- **30 min outreach sending**
- **20 min LinkedIn follow-up review**

---

## 6. Current Automation Policy

| Job | Schedule | Purpose |
|---|---|---|
| `com.tumsocialai.sales-collector` | Mon/Wed/Fri 09:00 + 14:00 | Processes incoming screenshots, URLs, manual notes. |
| `com.tumsocialai.project-applications` | Monday 09:30 | Processes project application intake. |
| `com.tumsocialai.requirements-enrichment` | Monday 07:00 + 12:00 | Enriches requirements/applications. |
| `com.tumsocialai.notion-cleanup` | Monday 10:00 + 15:00 | Fills missing domains/account types automatically; duplicate merges remain manual. |
| `com.tumsocialai.feedback-agent` | Monday 11:00 + 16:00 | Analyzes outreach outcomes, A/B results, and Notion copywriter iterations. |

Campaign/action jobs that run on demand:

| Job | Status |
|---|---|
| `com.tumsocialai.sales-ranking` | Manual (`python agent.py rank`) |
| `com.tumsocialai.linkedin-manager` | Manual (`python agent.py linkedin ...`) |
| `com.tumsocialai.sales-supervisor` | Manual (`python agent.py supervisor`) |
| `com.tumsocialai.enrichment` | Manual |
| `com.tumsocialai.copywriter` | Manual (`python agent.py copywrite --campaign ... --sender ...`) |

On macOS, Quick Actions are optional convenience tools. They are not required for teammates using Codex, Claude Code, Antigravity, or Windows.

---

## 7. Files And Data Flow

```text
LinkedIn screenshots / URLs / manual leads
  -> data/inputs/
  -> agent.py collect
  -> data/tables/master_input.csv
  -> agent.py rank
  -> data/tables/weekly_qualified_leads_with_contacts.csv
  -> data/tables/weekly_qualified_leads_no_contact.csv
  -> data/tables/top_leads_for_apollo_enrichment.csv
  -> Apollo connector search/enrichment
  -> agent.py apollo-enrich --mcp-json ...
  -> data/tables/apollo_enriched_contacts_for_review.csv
  -> data/tables/apollo_upload_ready.csv
  -> agent.py upload --csv data/tables/apollo_upload_ready.csv --sender ...
  -> Notion Accounts / Contacts
  -> agent.py copywrite --campaign ... --sender ...
  -> outreach messages in Notion + Gmail drafts in partnerships@tum-socialaiclub.de
  -> scripts/assign_partnership_outreach.py --apply
  -> Notion owners + sender signatures + teammate Gmail folders
  -> Slack launch note in #strategic-partnerships
  -> manual review and staggered sending
```

The CSV filenames still contain `weekly_` for backward compatibility. Treat them as on-demand campaign shortlist files.

---

## 8. Copywriter Improvement Loop

The weekly feedback agent (`com.tumsocialai.feedback-agent`) owns the copywriter improvement loop. Each run:

1. Reads unprocessed iterations from the [Notion Iterations page](https://www.notion.so/Iterations-on-Strategic-Partnersh-Copywriter-Agent-366a0c6e616880f8ba37ffa95d90b2fa)
2. Combines them with outreach outcome data and A/B test statistics
3. Writes consolidated guidance to `data/prompts/outreach_learnings.md`
4. Moves processed iterations into the **Processed ✅** toggle on the Notion page
5. The copywriter agent consumes `outreach_learnings.md` on its next on-demand campaign run

### Adding a New Iteration (When You Flag a Bad Draft)

Open the [Iterations page](https://www.notion.so/Iterations-on-Strategic-Partnersh-Copywriter-Agent-366a0c6e616880f8ba37ffa95d90b2fa) and add a new toggle inside **Not yet processed ❌** with:

- **Toggle title:** short description of the issue (e.g. "Too generic — no reference to trigger")
- **Inside the toggle:**
  - Bad/generated snippet
  - Improved version
  - Why the improved version is better

The feedback agent will pick it up on the next Monday run and mark it as **Processed ✅** automatically.

### Gmail Draft Review

After every copywriter run, drafts appear in `partnerships@tum-socialaiclub.de`:

1. Log into Gmail as `partnerships@tum-socialaiclub.de`
2. Open **Drafts** and your folder under **Strategic Partnerships**
3. Review the messages assigned to you, edit if needed, and send only on your assigned sending day
4. Keep the whole team to 20-30 sent outreach emails per day total

The draft subject and body match exactly what was written to Notion.

### Gmail OAuth (One-Time Setup Per Machine)

The OAuth token is already set up on Nicolas's machine (`gmail_token.json`). If you're running the agent from a different machine:

```bash
python setup_gmail_auth.py
```

This opens a browser — log in as `contact@tum-socialaiclub.de` and grant access. The token is saved locally and never committed to Git.

---

## 9. Troubleshooting

### Contacts DB 404

The database ID can be correct and still fail if the Notion integration is not invited to the database.

Fix:

1. Open the Contacts DB in Notion.
2. Click **Share**.
3. Invite the active integration, currently shown by Notion as **Claude Code Integration**.
4. Re-run the command.

Until this is fixed, the agents use Accounts DB fallback fields where possible.

### Upload/copywriter asks for a sender

Pass it explicitly:

```bash
python agent.py upload --csv "apollo.csv" --sender "Full Name"
python agent.py copywrite --campaign Workflow_DDMM --sender "Full Name"
```

Or set:

```bash
DEFAULT_CAMPAIGN_SENDER=Full Name
```

### macOS Google Drive permission errors

If scheduled intake jobs fail with “Operation not permitted”, grant `/bin/bash` Full Disk Access:

System Settings -> Privacy & Security -> Full Disk Access -> add `/bin/bash`

### LinkedIn parser finds no connections

Use a saved LinkedIn connections/network HTML file:

```bash
python agent.py linkedin --dry-run --connections-file "data/inputs/linkedin_dump/network_YYYYMMDD_HHMMSS.html"
```

---

## 10. Quick Reference

```bash
# Universal runner
python agent.py --help

# Intake
python agent.py collect

# Campaign shortlist
python agent.py rank

# Apollo enrichment review + upload
python agent.py apollo-enrich --mcp-json "session-or-apollo-output.jsonl"
python agent.py upload --csv "data/tables/apollo_upload_ready.csv" --sender "Full Name"

# Copywriter
python agent.py copywrite --campaign Workflow_DDMM --sender "Full Name"
python agent.py copywrite --campaign Workflow_DDMM --sender "Full Name" --dry-run

# LinkedIn review
python agent.py linkedin --connections-file "network.html"
python agent.py linkedin --dry-run --connections-file "network.html"

# Maintenance on demand
python agent.py supervisor
python agent.py feedback --dry-run
python agent.py cleanup --all
```
