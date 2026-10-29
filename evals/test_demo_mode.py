"""Demo mode: preset selection, the decision report and the guided flow.

The API is faked at urlopen, so no server, API key or webhook is involved.
"""

import hashlib
import io
import json
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

import dashboard
from app import demo_mode
from app.models import Issue
from app.slack_routing import default_destination
from evals.helpers import FIXTURE_PATH, load_cases

CASES = load_cases()
PRESETS = demo_mode.preset_cases(CASES)
DASHBOARD = str(Path(dashboard.__file__))
CONFIG = Path(dashboard.__file__).parent / ".streamlit" / "config.toml"


def _decision(issue_id: str, review: bool, source: str = "model-assisted") -> dict:
    return {
        "issue_id": issue_id,
        "priority": "P1" if review else "P3",
        "routing_team": "security" if review else "product-support",
        "requires_human_review": review,
        "decision_source": source,
        "summary": "stub summary",
    }


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass


def _by_label(elements, label: str):
    return next(element for element in elements if element.label == label)


class PresetTest(unittest.TestCase):
    def test_three_named_presets_in_order(self) -> None:
        self.assertEqual(list(PRESETS), ["Routine route", "Human review", "Override walkthrough"])

    def test_presets_are_distinct_fixtures_from_evals_issues_json(self) -> None:
        ids = [case.issue.issue_id for case in PRESETS.values()]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(set(ids) <= {case.issue.issue_id for case in CASES})

    def test_each_preset_demonstrates_its_name(self) -> None:
        routine, review, override = (PRESETS[name] for name in PRESETS)
        self.assertFalse(routine.expected["requires_human_review"])
        self.assertIsNone(routine.issue.human_override)
        self.assertTrue(review.expected["requires_human_review"])
        self.assertIsNone(review.issue.human_override)
        self.assertIsNotNone(override.issue.human_override)

    def test_every_preset_has_presenter_notes(self) -> None:
        self.assertEqual(set(demo_mode.PRESENTER_NOTES), set(PRESETS))

    def test_missing_fixture_kind_is_reported(self) -> None:
        only_routine = [case for case in CASES if not case.expected["requires_human_review"]]
        with self.assertRaises(LookupError):
            demo_mode.preset_cases(only_routine)

    def test_selection_leaves_the_fixtures_untouched(self) -> None:
        before = hashlib.sha256(Path(FIXTURE_PATH).read_bytes()).hexdigest()
        demo_mode.preset_cases(load_cases())
        self.assertEqual(hashlib.sha256(Path(FIXTURE_PATH).read_bytes()).hexdigest(), before)


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.issue = PRESETS["Human review"].issue
        self.decision = _decision(self.issue.issue_id, True)

    def test_report_has_issue_decision_action_destination_and_reason(self) -> None:
        report = demo_mode.build_decision_report(
            self.issue, self.decision, "team-routing", True, "Owner confirmed it is routine."
        )
        for expected in (
            self.issue.issue_id,
            self.issue.title,
            "Priority: P1",
            "Routing team: security",
            "Requires human review: true",
            "Decision source: model-assisted",
            "Action: Overrode the default destination",
            "Default destination: triage-review",
            "Chosen destination: team-routing",
            "Owner confirmed it is routine. (reviewer-provided)",
        ):
            self.assertIn(expected, report)

    def test_default_approval_cites_the_policy_when_no_reason_is_given(self) -> None:
        report = demo_mode.build_decision_report(self.issue, self.decision, "triage-review", True)
        self.assertIn("Action: Approved the default destination", report)
        self.assertIn("Policy default: requires_human_review=true routes to triage-review", report)

    def test_unapproved_report_says_pending(self) -> None:
        report = demo_mode.build_decision_report(self.issue, self.decision, "triage-review", False)
        self.assertIn("Action: Pending approval", report)

    def test_override_preset_report_shows_the_human_override(self) -> None:
        issue = PRESETS["Override walkthrough"].issue
        decision = _decision(issue.issue_id, True, source="human_override")
        report = demo_mode.build_decision_report(issue, decision, "triage-review", True)
        self.assertIn("Human override on the issue:", report)
        self.assertIn("Decision source: human_override", report)

    def test_report_contains_no_endpoint_or_secret_material(self) -> None:
        report = demo_mode.build_decision_report(self.issue, self.decision, "triage-review", True)
        self.assertNotIn("hooks.slack.com", report)
        self.assertNotIn("GEMINI", report)

    def test_filename(self) -> None:
        self.assertEqual(demo_mode.REPORT_FILENAME, "ai-triage-decision-report.md")


class DemoFlowTest(unittest.TestCase):
    def _app(self, demo: bool, preset: str | None = None, review: bool | None = None):
        """Open the dashboard; with a preset, also run live triage against a fake API."""
        sent: list[dict] = []

        def fake_urlopen(request: object, timeout: float) -> _Response:
            payload = json.loads(request.data)  # type: ignore[attr-defined]
            sent.append(payload)
            return _Response(json.dumps(_decision(payload["issue_id"], bool(review))).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen) as urlopen:
            app = AppTest.from_file(DASHBOARD).run()
            if demo:
                app.sidebar.checkbox(key="demo_mode").check().run()
            if preset:
                app.radio(key=dashboard.DEMO_PRESET_KEY).set_value(preset).run()
                _by_label(app.button, "Run live triage").click().run()
        return app, sent, urlopen

    def test_normal_mode_is_unchanged(self) -> None:
        app, _, _ = self._app(demo=False)
        self.assertEqual([tab.label for tab in app.tabs], ["Control room", "Evaluation"])
        self.assertFalse(app.radio)
        self.assertFalse([b for b in app.button if b.label == "Run live triage"])
        self.assertFalse(app.exception)

    def test_demo_mode_replaces_the_control_room_tab(self) -> None:
        app, _, _ = self._app(demo=True)
        self.assertEqual([tab.label for tab in app.tabs], ["Demo", "Evaluation"])
        self.assertEqual(list(app.radio(key=dashboard.DEMO_PRESET_KEY).options), list(PRESETS))

    def test_every_preset_posts_its_fixture_to_triage(self) -> None:
        for name, case in PRESETS.items():
            with self.subTest(preset=name):
                _, sent, urlopen = self._app(True, name, case.expected["requires_human_review"])
                self.assertEqual([c.args[0].full_url for c in urlopen.call_args_list], [dashboard.TRIAGE_URL])
                self.assertEqual(sent, [case.issue.model_dump(mode="json", exclude_none=True)])

    def test_override_preset_sends_the_human_override(self) -> None:
        _, sent, _ = self._app(True, "Override walkthrough", True)
        self.assertIn("human_override", sent[0])

    def test_default_destination_follows_the_policy_for_each_preset(self) -> None:
        for name, case in PRESETS.items():
            with self.subTest(preset=name):
                review = case.expected["requires_human_review"]
                app, _, _ = self._app(True, name, review)
                expected = default_destination({"requires_human_review": review})
                self.assertEqual(app.selectbox(key="demo_slack_destination_1").value, expected)

    def test_safety_fields_are_displayed(self) -> None:
        app, _, _ = self._app(True, "Human review", True)
        labels = [metric.label for metric in app.metric]
        for label in ("Priority", "Routing team", "Requires human review", "Decision source"):
            self.assertIn(label, labels)

    def test_report_download_is_offered_after_triage(self) -> None:
        before, _, _ = self._app(True)
        after, _, _ = self._app(True, "Routine route", False)
        self.assertEqual(len(before.get("download_button")), 0)
        self.assertEqual(len(after.get("download_button")), 1)

    def test_review_bypass_is_still_blocked_without_a_reason(self) -> None:
        app, _, _ = self._app(True, "Human review", True)
        app.selectbox(key="demo_slack_destination_1").select("team-routing").run()
        app.checkbox(key="demo_slack_approved_1").check().run()
        with mock.patch("urllib.request.urlopen") as urlopen:
            _by_label(app.button, "Send to Slack").click().run()
        urlopen.assert_not_called()
        self.assertEqual(len(app.error), 1)

    def test_changing_the_preset_clears_the_decision(self) -> None:
        app, _, _ = self._app(True, "Routine route", False)
        self.assertIn(dashboard.DEMO_DECISION_KEY, app.session_state)
        app.radio(key=dashboard.DEMO_PRESET_KEY).set_value("Human review").run()
        self.assertNotIn(dashboard.DEMO_DECISION_KEY, app.session_state)


class BrandingTest(unittest.TestCase):
    def test_config_sets_a_theme_and_hides_nothing(self) -> None:
        text = CONFIG.read_text(encoding="utf-8")
        config = tomllib.loads(text)
        self.assertIn("primaryColor", config["theme"])
        for hiding in ("toolbarMode", "hideTopBar", "showSidebarNavigation", "display: none"):
            self.assertNotIn(hiding, text)

    def test_config_holds_no_secrets(self) -> None:
        text = CONFIG.read_text(encoding="utf-8")
        for secret in ("hooks.slack.com", "GEMINI", "api_key", "webhook"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
