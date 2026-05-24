# TUM Social AI — Strategic Partnerships Agents

Command-first infrastructure for collecting partnership leads, creating on-demand campaign shortlists, uploading Apollo-enriched leads into Notion, generating sender-aware outreach copy, and reviewing LinkedIn follow-ups.

The system is no longer a fixed weekly campaign machine. Intake, safe cleanup, and feedback analysis can stay automated, while ranking, reports, upload, copywriting, and LinkedIn review are run when a teammate asks for them.

## Quick Start

```bash
git clone https://github.com/tumsocialai/strategic-partnerships.git
cd strategic-partnerships
python3 -m venv venv
source venv/bin/activate   # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.template .env
python agent.py --help
```

## Main Commands

| Task | Command |
|---|---|
| Process new inputs | `python agent.py collect` |
| Create top leads report | `python agent.py rank` |
| Upload Apollo export | `python agent.py upload --csv "apollo.csv" --sender "Full Name"` |
| Generate outreach copy | `python agent.py copywrite --campaign Workflow_DDMM --sender "Full Name"` |
| LinkedIn follow-up review | `python agent.py linkedin --connections-file "network.html"` |
| Company phone enrichment | `python agent.py phone-enrich` |
| Infrastructure audit | `python agent.py supervisor` |
| Feedback analysis | `python agent.py feedback` |
| Notion cleanup | `python agent.py cleanup --all` |

## Agent Logic

| Agent / flow | Built on |
|---|---|
| `collector` | LinkedIn screenshots, LinkedIn URLs, and manual contacts; GPT-4o Vision/entity extraction; dedup by domain/person/profile URL. |
| `ranking_agent` | GPT-4o 0-10 scoring for impact fit, AI/talent relevance, student ecosystem signal, similarity to pipeline wins, and timing/trigger quality. |
| Apollo enrichment | Manual enrichment after ranking so the team enriches only selected campaign leads. |
| `upload_agent` | Apollo CSV parsing, Notion schema/preflight checks, account/contact deduplication, campaign sender, campaign ID, and safe create/update behavior. |
| `copywriter_agent` | Outreach skill prompt, processed feedback learnings, campaign sender, contact/account context, trigger event, and four generated messages. |
| `feedback_agent` | Outreach outcome classification, A/B test analysis, Notion Iterations page ingestion, and `outreach_learnings.md` updates. |
| `linkedin_manager` | Saved LinkedIn connections HTML, Notion matching, follow-up/ghosting thresholds, and status hierarchy guards. |
| `company_phone_enrichment_agent` | Notion Accounts filter, website page discovery, Impressum/Kontakt/contact scanning, phone normalization to `+...`, and safe phone-only updates. |

## Requirements

- Python 3.9+
- OpenAI API key
- Notion integration token with access to Accounts and Contacts databases
- Optional Gmail app password for report emails
- Codex, Claude Code, Antigravity, or a normal terminal

See [ONBOARDING.md](ONBOARDING.md) or open `ONBOARDING.html` for teammate setup, cadence, troubleshooting, and operating guidance.

## Automation Policy

Keep scheduled: collector, project applications, requirements enrichment, Notion cleanup, feedback analysis.

Run on demand: ranking/top leads report, upload, copywriter, LinkedIn manager, supervisor, enrichment.

## License

Proprietary — TUM Social AI · https://tum-socialaiclub.de
