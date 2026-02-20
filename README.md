# 🤖 TUM Sales Agent

> ⚠️ **Private Repository** — Internal use only.

A multi-agent AI sales pipeline for automated lead discovery, scoring, enrichment, and outreach — built for TUM Social AI.

## System Overview

This is a complete **AI-powered sales automation system** consisting of multiple specialized agents:

### Agents

| Agent | What It Does | Schedule |
|-------|-------------|----------|
| **Collector** | Extracts leads from LinkedIn screenshots, URLs, and manual inputs | On-demand / watch mode |
| **Ranking Agent** | GPT-4o scoring (0-10) with dedup checking against Notion CRM | Tue/Thu 10am |
| **Upload Agent** | Pushes qualified leads to Notion CRM databases | After ranking |
| **LinkedIn Manager** | Parses LinkedIn connection exports for new connections | Weekly |
| **Copywriter Agent** | Generates personalized outreach messages (LinkedIn + email) | Manual |
| **Feedback Agent** | Collects team feedback on lead quality | Weekly |
| **Supervisor** | Runs full pipeline & generates weekly reports | Weekly |
| **Report Generator** | PDF reports with cost tracking & pipeline analytics | On-demand |

### Data Flow

```
LinkedIn Screenshots/URLs → Collector → Master CSV
                                           ↓
                                    Ranking Agent (GPT-4o scoring)
                                           ↓
                                    Upload Agent → Notion CRM
                                           ↓
                                    Copywriter → Outreach Messages
```

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/nicopaul19/tum-sales-agent.git
cd tum-sales-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.template .env
# Edit .env with your API keys
```

### 3. Run individual agents

```bash
# Collect leads from inputs
python -m agents.collector

# Score & rank leads
python -m agents.ranking_agent

# Upload to Notion CRM
python -m agents.upload_agent

# Full pipeline
python -m agents.supervisor
```

## Project Structure

```
tum_sales_agent/
├── agents/                   # All agent modules
│   ├── collector.py          # Lead input processing (screenshots, URLs)
│   ├── ranking_agent.py      # GPT-4o lead scoring
│   ├── upload_agent.py       # Notion CRM uploader
│   ├── linkedin_manager.py   # LinkedIn connections analysis
│   ├── linkedin_parser.py    # LinkedIn HTML parser
│   ├── copywriter_agent.py   # Outreach message generation
│   ├── feedback_agent.py     # Team feedback collection
│   ├── supervisor.py         # Pipeline orchestrator
│   ├── report_generator.py   # PDF/analytics reports
│   └── notion_cleanup.py     # Notion database maintenance
├── utils/                    # Shared utilities
│   ├── config.py             # Configuration & env loading
│   ├── notion_client.py      # Notion API wrapper
│   ├── apollo_client.py      # Apollo.io API client
│   ├── api_logger.py         # API cost tracking
│   └── preflight.py          # Pre-run validation
├── scripts/                  # Shell scripts for agents
├── data/                     # Data directory (gitignored)
│   ├── inputs/               # Lead input files
│   ├── tables/               # CSV data files
│   ├── reports/              # Generated reports
│   └── logs/                 # API usage logs
├── requirements.txt          # Python dependencies
├── .env.template             # Environment variable template
└── ONBOARDING.md             # Detailed system documentation
```

## Requirements

- Python 3.8+
- OpenAI API account (GPT-4o)
- Notion workspace with CRM databases
- Gmail account with App Password (for email reports)
- Apollo.io API key (optional, for lead enrichment)

## License

Proprietary — TUM Social AI
