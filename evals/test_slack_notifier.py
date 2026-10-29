"""Slack notifier: message format, destination lookup and secret handling.

Only placeholder URLs are used, and urlopen is replaced, so nothing is sent.
"""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from app import slack_notifier

FAKE_TEAM_ROUTING = "https://hooks.slack.com/services/T0/B0/team-routing-placeholder"
FAKE_TRIAGE_REVIEW = "https://hooks.slack.com/services/T0/B0/triage-review-placeholder"
DECISION = {
    "issue_id": "ISSUE-001",
    "priority": "P0",
    "routing_team": "security",
    "requires_human_review": True,
    "decision_source": "baseline_fallback",
    "summary": "Keys exposed.",
}


def _config(webhooks: dict[str, str]) -> Path:
    path = Path(tempfile.mkdtemp()) / "slack-webhooks.json"
    path.write_text(json.dumps(webhooks), encoding="utf-8")
    return path


class FormatTest(unittest.TestCase):
    def test_message_carries_the_decision_fields(self) -> None:
        text = slack_notifier.format_decision(DECISION)["text"]
        for expected in ("ISSUE-001", "P0", "security", "needs human review", "baseline_fallback", "Keys exposed."):
            self.assertIn(expected, text)


    def test_override_reason_appears_in_the_message(self) -> None:
        text = slack_notifier.format_decision(DECISION, "Owner confirmed it is routine.")["text"]
        self.assertIn("Routing override: Owner confirmed it is routine.", text)
        self.assertNotIn("Routing override", slack_notifier.format_decision(DECISION)["text"])


class SendTest(unittest.TestCase):
    def test_channel_names_are_unchanged(self) -> None:
        self.assertEqual(
            slack_notifier.CHANNELS, {"triage-review": "#triage-review", "team-routing": "#team-routing"}
        )

    def test_posts_to_the_named_webhook_only(self) -> None:
        config = _config({"triage-review": FAKE_TRIAGE_REVIEW, "team-routing": FAKE_TEAM_ROUTING})
        with mock.patch("urllib.request.urlopen") as urlopen:
            slack_notifier.send_decision(DECISION, "team-routing", config_path=config)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, FAKE_TEAM_ROUTING)
        self.assertEqual(json.loads(request.data), slack_notifier.format_decision(DECISION))

    def test_missing_config_raises_without_a_url(self) -> None:
        with self.assertRaises(slack_notifier.SlackNotifierError):
            slack_notifier.send_decision(DECISION, "team-routing", config_path=Path(tempfile.mkdtemp()) / "none.json")

    def test_missing_destination_raises(self) -> None:
        config = _config({"triage-review": FAKE_TRIAGE_REVIEW})
        with self.assertRaises(slack_notifier.SlackNotifierError):
            slack_notifier.send_decision(DECISION, "team-routing", config_path=config)

    def test_errors_never_contain_the_webhook(self) -> None:
        config = _config({"team-routing": FAKE_TEAM_ROUTING})
        failures = [
            urllib.error.HTTPError(FAKE_TEAM_ROUTING, 404, "no_service", {}, io.BytesIO(b"")),
            urllib.error.URLError(f"unreachable {FAKE_TEAM_ROUTING}"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with mock.patch("urllib.request.urlopen", side_effect=failure):
                    with self.assertRaises(slack_notifier.SlackNotifierError) as caught:
                        slack_notifier.send_decision(DECISION, "team-routing", config_path=config)
                self.assertNotIn(FAKE_TEAM_ROUTING, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
