"""
API usage logger — appends one JSON line per call to api_usage.jsonl.
Shared by collector, ranking agent, and any future agents.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.config import API_USAGE_LOG


def log_api_usage(agent: str, action: str, model: str, usage, metadata=None):
    """
    Append an API usage record to the JSONL log.

    Args:
        agent: Agent name (e.g. "collector", "ranking_agent")
        action: What the call did (e.g. "screenshot_extraction", "lead_scoring")
        model: Model used (e.g. "gpt-4o")
        usage: The usage object from the OpenAI response (has prompt_tokens, completion_tokens, total_tokens)
        metadata: Optional extra info (e.g. company_name, image_path)
    """
    API_USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "action": action,
        "model": model,
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
    }
    if metadata:
        record["metadata"] = metadata

    with open(API_USAGE_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
