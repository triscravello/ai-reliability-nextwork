"""Format a triage decision and send it to a configured Slack webhook.

Webhook URLs are secrets. They are read from the user's config file only at send
time, and no message, exception or log line ever contains one.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".config" / "ai-triage-control-room" / "slack-webhooks.json"
WEBHOOK_PREFIX = "https://hooks.slack.com/"
REQUEST_TIMEOUT_SECONDS = 10

# Stored webhook name -> the channel it posts to.
CHANNELS = {"triage-review": "#triage-review", "team-routing": "#team-routing"}


class SlackNotifierError(Exception):
    """A decision could not be sent. The message never includes a webhook URL."""


def format_decision(decision: dict[str, Any], override_reason: str = "") -> dict[str, str]:
    """Build the Slack message payload for one TriageDecision."""
    review = "needs human review" if decision["requires_human_review"] else "no human review needed"
    text = (
        f"*{decision['issue_id']}*: {decision['priority']} -> {decision['routing_team']} ({review})\n"
        f"Source: {decision['decision_source']}\n"
        f"{decision['summary']}"
    )
    if override_reason.strip():
        text += f"\nRouting override: {override_reason.strip()}"
    return {"text": text}


def _load_webhook(destination: str, config_path: Path) -> str:
    try:
        webhook = json.loads(config_path.read_text(encoding="utf-8")).get(destination)
    except (OSError, json.JSONDecodeError, AttributeError):
        raise SlackNotifierError(
            "Slack is not configured. Run scripts/configure_slack.py first."
        ) from None
    if not isinstance(webhook, str) or not webhook.startswith(WEBHOOK_PREFIX):
        raise SlackNotifierError(f"No valid webhook stored for {destination}.")
    return webhook


def send_decision(
    decision: dict[str, Any],
    destination: str,
    override_reason: str = "",
    config_path: Path | None = None,
) -> None:
    """Post the decision to the named destination's channel."""
    webhook = _load_webhook(destination, config_path or CONFIG_PATH)
    request = urllib.request.Request(
        webhook,
        data=json.dumps(format_decision(decision, override_reason)).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
            pass
    except urllib.error.HTTPError as error:
        # Report only the status: the exception text can carry the request URL.
        raise SlackNotifierError(f"Slack rejected the message (HTTP {error.code}).") from None
    except (urllib.error.URLError, TimeoutError):
        raise SlackNotifierError("Could not reach Slack.") from None
