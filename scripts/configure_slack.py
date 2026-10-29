"""Store the two Slack webhook URLs used by the triage control room.

Run:  python scripts/configure_slack.py

The prompts are hidden, and the URLs are written only to the user's config file
(mode 0600). Nothing here prints or echoes a URL.
"""

import getpass
import json
import os
import sys
from pathlib import Path

# Run as a script, so put the repo root on the path to share the app's constants.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.slack_notifier import CONFIG_PATH, WEBHOOK_PREFIX  # noqa: E402

# Prompt order matters: triage-review first, then team-routing.
PROMPTS = (
    ("triage-review", "Webhook URL for #triage-review: "),
    ("team-routing", "Webhook URL for #team-routing: "),
)


def prompt_webhook(name: str, prompt: str) -> str:
    webhook = getpass.getpass(prompt).strip()
    if not webhook.startswith(WEBHOOK_PREFIX):
        # Never include the value in the message.
        sys.exit(f"The {name} value is not a Slack webhook URL. Nothing was saved.")
    return webhook


def write_private(path: Path, webhooks: dict[str, str]) -> None:
    """Write the config readable only by the current user where supported."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    # 0600 at creation, so the secret is never briefly world-readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
        json.dump(webhooks, config_file)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def main() -> None:
    webhooks = {name: prompt_webhook(name, prompt) for name, prompt in PROMPTS}
    write_private(CONFIG_PATH, webhooks)
    print(f"Saved {', '.join(webhooks)} to {CONFIG_PATH}")


if __name__ == "__main__":
    main()
