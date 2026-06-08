# TUM Social AI — Strategic Partnerships Agents

Command-first infrastructure for collecting partnership leads, creating on-demand campaign shortlists, uploading Apollo-enriched leads into Notion, generating sender-aware outreach copy, and reviewing LinkedIn follow-ups.

The system is no longer a fixed weekly campaign machine. Intake, safe cleanup, and feedback analysis can stay automated, while ranking, reports, upload, copywriting, and LinkedIn review are run when a teammate asks for them.

## Quick Start

```bash
git clone https://github.com/nicopaul19/tum-sales-agent.git
cd tum-sales-agent
python3 -m venv venv
source venv/bin/activate   # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.template .env
python agent.py --help
```

If an older onboarding page points to `github.com/tumsocialai/strategic-partnerships.git`, use the `nicopaul19/tum-sales-agent` URL above. The old org URL currently returns `Repository not found`.

## Main Commands

| Task | Command |
|---|---|
| Process new inputs | `python agent.py collect` |
| Create top leads report | `python agent.py rank` |
| Prepare Apollo enrichment review | `python agent.py apollo-enrich --mcp-json "session-or-apollo-output.jsonl"` |
| Upload Apollo export | `python agent.py upload --csv "apollo.csv" --sender "Full Name"` |
| Generate outreach copy | `python agent.py copywrite --campaign Workflow_DDMM --sender "Full Name"` |
| Sync Campaign Tracker | `python agent.py campaigns` |
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
| `apollo_enrichment_agent` | Repeatable Apollo review flow: writes enrichment batches, merges Apollo connector/UI exports, splits mobile-required blockers from email-ready contacts, and emits a strict upload-ready CSV. |
| `upload_agent` | Apollo CSV parsing, Notion schema/preflight checks, account/contact deduplication, campaign sender, campaign ID, and safe create/update behavior. |
| `copywriter_agent` | Outreach skill prompt, Campaign Tracker history, processed feedback learnings, campaign sender, contact/account context, trigger event, A/B variant, and four generated messages. |
| `campaign_tracker` | Notion Campaign Tracker backfill/sync from Accounts and Contacts: account relation, trigger, target audience, reasoning, engagement, and A/B performance. |
| `feedback_agent` | Outreach outcome classification, A/B test analysis, Campaign Tracker updates, Notion Iterations page ingestion, and `outreach_learnings.md` updates. |
| `linkedin_manager` | Saved LinkedIn connections HTML, Notion matching, follow-up/ghosting thresholds, and status hierarchy guards. |
| `company_phone_enrichment_agent` | Notion Accounts filter, website page discovery, Impressum/Kontakt/contact scanning, phone normalization to `+...`, and safe phone-only updates. |

Campaign tracking rule: setting `Campaign ID` on Accounts is only the first half of campaign creation. Every strategic partnership campaign must also have a Campaign Tracker database entry, related back to all targeted Accounts, with trigger, target audience, targeting reasoning, outreach summary, and A/B/performance fields. The upload, copywriter, and feedback agents sync this automatically; after manual CRM edits, run `python agent.py campaigns`.

## Requirements

- Python 3.9+
- OpenAI API key
- Notion integration token with access to Accounts, Contacts, and Campaign Tracker databases
- Optional Gmail app password for report emails
- Codex, Claude Code, Antigravity, or a normal terminal

See [ONBOARDING.md](ONBOARDING.md) or open `ONBOARDING.html` for teammate setup, cadence, troubleshooting, and operating guidance.

## Automation Policy

Keep scheduled: collector, project applications, requirements enrichment, Notion cleanup, feedback analysis.

Run on demand: ranking/top leads report, upload, copywriter, LinkedIn manager, supervisor, enrichment.

## Apollo Enrichment Flow

1. Run `python agent.py rank`. The ranker always writes `weekly_qualified_leads_with_contacts.csv`, `weekly_qualified_leads_no_contact.csv`, and `top_leads_for_apollo_enrichment.csv`.
2. Before export, the ranker checks current Notion Accounts by domain/name. Existing accounts are removed and the list is backfilled from the next best companies.
3. Search Apollo for contacts on the no-contact companies, keep known-contact companies separate, and write or review `apollo_contact_review.csv`.
4. Run `python agent.py apollo-enrich` to create `apollo_enrichment_batches.json`.
5. After approved Apollo enrichment, run `python agent.py apollo-enrich --mcp-json "session-or-apollo-output.jsonl"` or `python agent.py apollo-enrich --apollo-csv "apollo_export.csv"`.
6. Review `apollo_enriched_contacts_for_review.csv`. Senior marketing/recruiting/people, partnerships/BD, campus/university relations, ecosystem, and community contacts require a real mobile number; rows without that are blocked with `needs_mobile_scrape`. Other contacts only need email.
7. Upload only after review approval: `python agent.py upload --csv data/tables/apollo_upload_ready.csv --sender "Full Name"`.

Upload hard rules: new or updated Accounts are set to `Account Type* = Corporate`, new Contacts are set to `Contact Status = New`, and contact mobile/direct phone values are dropped when they match the company phone.

## License

Proprietary — TUM Social AI · https://tum-socialaiclub.de
