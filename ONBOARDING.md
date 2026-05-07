# TUM Social AI — Strategic Partnerships Agents
## On-Demand Campaign Onboarding Guide

**Last updated:** May 5, 2026  
**Audience:** TUM Social AI teammates running partnership campaigns from Codex, Claude Code, Antigravity, or a terminal.

---

## 1. What This Infrastructure Does

This repo turns raw partnership signals into ready-to-send outreach:

1. **Collect input** from LinkedIn screenshots, LinkedIn URLs, and manual contact notes.
2. **Create a top leads report on demand** when the team wants a new campaign shortlist.
3. **Enrich selected leads in Apollo** manually.
4. **Upload Apollo exports into Notion** with an explicit campaign sender.
5. **Generate outreach copy** for that sender.
6. **Review LinkedIn connection/follow-up actions** when requested.

The system is intentionally no longer a fixed weekly campaign machine. Campaign actions should happen when someone deliberately asks for them.

### What Each Agent Is Built On

| Agent / flow | What it does | Criteria and logic |
|---|---|---|
| `collector` | Aggregates lead signals into `data/tables/master_input.csv`. | Uses three input streams: LinkedIn screenshots in `data/inputs/images/`, LinkedIn post URLs in `data/inputs/linkedin_urls/`, and manual contact CSVs/notes in `data/inputs/manual_contacts/`. GPT-4o Vision extracts company/person/context from screenshots. LinkedIn URL processing extracts entities, domains, roles, and the specific trigger event. Deduplication uses normalized domain + person name, LinkedIn profile URLs, and a per-company cap so the master table stays campaign-safe. |
| `ranking_agent` | Creates the on-demand top leads report. | Uses GPT-4o structured scoring from 0-10. The strongest fit is a company similar to proven pipeline successes, with positive social/ecological impact, AI or AI-talent relevance, and evidence of engaging with student organizations in Germany. DACH presence is only a tiebreaker, not a hard requirement. Leads with score >= 5 qualify for the shortlist. |
| `ranking_agent` filters | Prevents obvious bad leads from entering campaigns. | Student clubs, university associations, other student initiatives, very early startups, companies with no AI/impact/ecological angle, traditional finance, gambling, tobacco/alcohol, weapons/defense, event services, catering, and similar support vendors are disqualified or heavily penalized. |
| Apollo enrichment | Adds verified company/contact data before Notion upload. | This step is manual in Apollo. It is used after ranking so the team does not spend enrichment effort on weak leads. Apollo exports should include account/contact identifiers, email, title, company domain, LinkedIn, employee/funding fields when available, and campaign-relevant contact rows. |
| `upload_agent` | Uploads Apollo CSVs into Notion Accounts and Contacts. | Requires an explicit campaign sender. It patches/validates required Notion properties, deduplicates accounts by Apollo Account ID/domain/name, deduplicates contacts by email/LinkedIn/name, links Contacts to Accounts, writes campaign ID, sender, account metadata, contact metadata, and safely updates existing records without resetting useful pipeline status unless intended. |
| `copywriter_agent` | Generates campaign-specific outreach in Notion. | Uses the shared outreach skill prompt plus processed `data/prompts/outreach_learnings.md`, campaign sender, contact/account context, trigger event, company mission, employee/funding context, and sometimes careers-page context. It writes LinkedIn first cold, LinkedIn follow-up, cold email subject, and cold email body. Copy is short, English, specific to the trigger, sender-aware, and constrained against invented facts. |
| `linkedin_manager` | Reviews LinkedIn connection/follow-up actions. | Parses saved LinkedIn connections HTML, matches LinkedIn URLs to Notion Contacts/Accounts, detects new connections, identifies follow-up needs after 3-5 days, marks ghosted leads after the configured window, drafts follow-up text, and avoids downgrading Notion statuses through a status hierarchy guard. |
| `feedback_agent` | Turns outcome data into prompt learnings. | Reads enough resolved outcomes, analyzes what copy worked or failed, and distills reusable guidance into `data/prompts/outreach_learnings.md` for future copywriter runs. |

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
git clone https://github.com/tumsocialai/strategic-partnerships.git
cd strategic-partnerships
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.template .env
```

### Windows PowerShell

```powershell
git clone https://github.com/tumsocialai/strategic-partnerships.git
cd strategic-partnerships
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
| Upload Apollo export | After Apollo enrichment | `python agent.py upload --csv "/path/to/apollo.csv" --sender "Full Name"` |
| Generate outreach | After upload | `python agent.py copywrite --campaign Workflow_DDMM --sender "Full Name"` |
| LinkedIn review | After saving LinkedIn network HTML | `python agent.py linkedin --connections-file "/path/to/network.html"` |
| Infrastructure audit | On request | `python agent.py supervisor` |
| Feedback analysis | On request, after enough outcomes | `python agent.py feedback` |
| Notion cleanup | On request | `python agent.py cleanup --all` |

### Campaign Sender

Every campaign needs a sender because the copy should sound like the teammate who will actually send it.

Examples:

```bash
python agent.py upload --csv "apollo_export.csv" --sender "Nicolas Paul"
python agent.py copywrite --campaign Workflow_0505 --sender "Nicolas Paul"
```

The generated messages use:

- LinkedIn sign-off: `Best, {first_name}`
- Email sign-off: `{full_name}`

---

## 5. Operating Cadence

Put these as recurring calendar blockers, but execute campaign actions only when useful.

| Cadence | Blocker | Action |
|---|---|---|
| Continuous | LinkedIn input capture | Save promising posts, profiles, screenshots, and manual contacts as you browse. |
| On campaign start | Input cleanup + top leads + Apollo | Run `python agent.py collect`, scan inputs, run `python agent.py rank`, review the shortlist, then enrich selected leads in Apollo. |
| After Apollo | Upload + copywriter | Run upload with `--sender`, then copywrite for the campaign. |
| 1x per week during active campaign | LinkedIn follow-up review | Save the LinkedIn connections page and run `python agent.py linkedin --connections-file ...`. |
| After outcomes accumulate | Copywriter feedback | Add examples to the Notion improvement log; run `python agent.py feedback` when there is enough data. |

Recommended calendar blocks:

- **60 min input cleanup + campaign shortlist + Apollo enrichment**
- **30 min outreach sending**
- **20 min LinkedIn follow-up review**

---

## 6. Current Automation Policy

Only lightweight intake automation should stay scheduled:

| Job | Status | Purpose |
|---|---|---|
| `com.tumsocialai.sales-collector` | Keep scheduled | Processes incoming screenshots, URLs, manual notes. |
| `com.tumsocialai.project-applications` | Keep scheduled | Processes project application intake. |
| `com.tumsocialai.requirements-enrichment` | Keep scheduled | Enriches requirements/applications. |

Campaign/action jobs should be manual:

| Job | New status |
|---|---|
| `com.tumsocialai.sales-ranking` | Disabled |
| `com.tumsocialai.linkedin-manager` | Disabled |
| `com.tumsocialai.sales-supervisor` | Disabled |
| `com.tumsocialai.feedback-agent` | Disabled |
| `com.tumsocialai.notion-cleanup` | Disabled |
| `com.tumsocialai.enrichment` | Disabled |

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
  -> Apollo enrichment
  -> agent.py upload --csv ... --sender ...
  -> Notion Accounts / Contacts
  -> agent.py copywrite --campaign ... --sender ...
  -> outreach messages in Notion
```

The CSV filenames still contain `weekly_` for backward compatibility. Treat them as on-demand campaign shortlist files.

---

## 8. Copywriter Improvement Loop

Use the Notion copywriter improvement log inside the Partnerships Agents page.

Add each bad or low-quality message as an item under **not yet processed** with:

- bad/generated snippet
- improved version
- why the improved version is better
- campaign/contact context

When processed, move it under **processed** and add the distilled learning to:

```bash
data/prompts/outreach_learnings.md
```

The copywriter injects this file into future prompt runs.

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

# Apollo upload
python agent.py upload --csv "apollo_export.csv" --sender "Full Name"

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
