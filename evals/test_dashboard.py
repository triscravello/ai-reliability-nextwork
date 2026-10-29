"""Dashboard: request building, formatting and Session State, with no network.

The API call is replaced by a fake, so no server and no API key are needed.
"""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

import dashboard
from evals.helpers import load_cases

DASHBOARD_PATH = str(Path(dashboard.__file__))


def _decision(issue_id: str, priority: str, team: str, review: bool, source: str = "baseline") -> dict:
    return {
        "issue_id": issue_id,
        "priority": priority,
        "routing_team": team,
        "requires_human_review": review,
        "decision_source": source,
        "summary": "stub",
    }


HISTORY = [
    _decision("A", "P0", "security", True),
    _decision("B", "P0", "payments", True),
    _decision("C", "P2", "payments", False),
]


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class RequestTest(unittest.TestCase):
    def test_payload_omits_override_when_absent(self) -> None:
        payload = dashboard.build_payload(" X-1 ", "Title", "Body", "Impact")
        self.assertEqual(
            payload,
            {"issue_id": "X-1", "title": "Title", "body": "Body", "customer_impact": "Impact"},
        )

    def test_payload_includes_override(self) -> None:
        override = {"priority": "P1", "routing_team": "security", "reason": "Triaged by on-call."}
        payload = dashboard.build_payload("X-1", "T", "B", "", override=override)
        self.assertEqual(payload["human_override"], override)

    def test_post_triage_sends_json_to_the_triage_endpoint(self) -> None:
        body = json.dumps(HISTORY[0]).encode("utf-8")
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)) as urlopen:
            result = dashboard.post_triage({"issue_id": "A"})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8000/triage")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"issue_id": "A"})
        self.assertEqual(result, HISTORY[0])

    def test_http_error_becomes_request_error(self) -> None:
        error = urllib.error.HTTPError(dashboard.TRIAGE_URL, 422, "Unprocessable", {}, io.BytesIO(b"bad title"))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(dashboard.TriageRequestError, "422"):
                dashboard.post_triage({})

    def test_unreachable_api_becomes_request_error(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(dashboard.TriageRequestError):
                dashboard.post_triage({})


class FormattingTest(unittest.TestCase):
    def test_kpis(self) -> None:
        self.assertEqual(
            dashboard.compute_kpis(HISTORY),
            {"Issues triaged": 3, "Critical issues": 2, "Needs review": 2, "Teams reached": 2},
        )

    def test_kpis_for_empty_history(self) -> None:
        self.assertEqual(set(dashboard.compute_kpis([]).values()), {0})

    def test_distribution_lists_every_priority_in_order(self) -> None:
        counts = dashboard.priority_distribution(HISTORY)
        self.assertEqual(list(counts.items()), [("P0", 2), ("P1", 0), ("P2", 1), ("P3", 0)])

    def test_history_rows_are_newest_first(self) -> None:
        rows = dashboard.history_rows(HISTORY)
        self.assertEqual([row["Issue"] for row in rows], ["C", "B", "A"])
        self.assertEqual(rows[0]["Human review"], "no")
        self.assertEqual(rows[2]["Human review"], "yes")


class StateTest(unittest.TestCase):
    def test_record_decision_appends(self) -> None:
        history: list[dict] = []
        dashboard.record_decision(history, HISTORY[0])
        dashboard.record_decision(history, HISTORY[1])
        self.assertEqual(history, HISTORY[:2])

    def test_fixture_selector_lists_every_record_and_loads_editable_fields(self) -> None:
        cases = load_cases()
        app = AppTest.from_file(DASHBOARD_PATH).run()
        self.assertFalse(app.exception)

        selector = app.selectbox(key="fixture")
        self.assertEqual(list(selector.options), [case.issue.issue_id for case in cases])

        for case in cases:
            app.selectbox(key="fixture").select(case.issue.issue_id).run()
            self.assertEqual(app.text_input(key="issue_id").value, case.issue.issue_id)
            self.assertEqual(app.text_input(key="title").value, case.issue.title)
            self.assertEqual(app.text_area(key="body").value, case.issue.body)
            self.assertEqual(app.checkbox(key="use_override").value, case.issue.human_override is not None)

    def test_submit_sends_edited_fields_and_stores_history_in_session_state(self) -> None:
        sent: list[dict] = []

        def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
            payload = json.loads(request.data)  # type: ignore[attr-defined]
            sent.append(payload)
            return _FakeResponse(json.dumps(_decision(payload["issue_id"], "P1", "security", True)).encode())

        # AppTest re-executes dashboard.py, so patch the network edge, not the module.
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            app = AppTest.from_file(DASHBOARD_PATH).run()
            app.text_input(key="title").set_value("Edited title").run()
            _button(app, "Triage issue").click().run()

        self.assertEqual(sent[0]["title"], "Edited title")
        self.assertEqual(len(app.session_state[dashboard.HISTORY_KEY]), 1)
        self.assertEqual(app.session_state[dashboard.CURRENT_KEY]["priority"], "P1")

    def test_api_failure_leaves_history_unchanged(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            app = AppTest.from_file(DASHBOARD_PATH).run()
            _button(app, "Triage issue").click().run()

        self.assertEqual(app.session_state[dashboard.HISTORY_KEY], [])
        self.assertEqual(len(app.error), 1)

    def test_load_review_fixture_selects_a_fixture_that_expects_review(self) -> None:
        app = AppTest.from_file(DASHBOARD_PATH).run()
        _button(app, "Load review fixture").click().run()

        expected = next(case for case in load_cases() if case.expected["requires_human_review"])
        self.assertEqual(app.selectbox(key="fixture").value, expected.issue.issue_id)
        self.assertEqual(app.text_input(key="title").value, expected.issue.title)

    def _triaged_app(self, review: bool) -> AppTest:
        decision = _decision("A", "P1" if review else "P3", "security", review)
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(json.dumps(decision).encode())):
            app = AppTest.from_file(DASHBOARD_PATH).run()
            _button(app, "Triage issue").click().run()
        return app

    def test_selecting_a_fixture_clears_the_current_decision(self) -> None:
        app = self._triaged_app(True)
        self.assertIsNotNone(app.session_state[dashboard.CURRENT_KEY])

        app.selectbox(key="fixture").select("ISSUE-005").run()

        self.assertNotIn(dashboard.CURRENT_KEY, app.session_state)
        self.assertFalse([box for box in app.selectbox if box.label == "Slack destination"])
        self.assertEqual(len(app.session_state[dashboard.HISTORY_KEY]), 1)  # history is kept

    def test_load_review_fixture_clears_the_current_decision(self) -> None:
        app = self._triaged_app(False)
        _button(app, "Load review fixture").click().run()
        self.assertNotIn(dashboard.CURRENT_KEY, app.session_state)

    def test_slack_controls_name_the_issue_they_belong_to(self) -> None:
        app = self._triaged_app(True)
        self.assertIn("**Slack controls for A**", [md.value for md in app.markdown])

    def test_default_slack_destination_follows_requires_human_review(self) -> None:
        self.assertEqual(self._triaged_app(True).selectbox(key="slack_destination_1").value, "triage-review")
        self.assertEqual(self._triaged_app(False).selectbox(key="slack_destination_1").value, "team-routing")

    def test_send_is_disabled_until_the_destination_is_approved(self) -> None:
        app = self._triaged_app(True)
        self.assertTrue(_button(app, "Send to Slack").disabled)
        app.checkbox(key="slack_approved_1").check().run()
        self.assertFalse(_button(app, "Send to Slack").disabled)

    def test_review_bypass_without_a_reason_is_blocked_and_never_posts(self) -> None:
        app = self._triaged_app(True)
        app.selectbox(key="slack_destination_1").select("team-routing").run()
        app.checkbox(key="slack_approved_1").check().run()
        with mock.patch("urllib.request.urlopen") as urlopen:
            _button(app, "Send to Slack").click().run()

        urlopen.assert_not_called()
        self.assertEqual(len(app.error), 1)

    def test_approved_default_send_reports_missing_config(self) -> None:
        app = self._triaged_app(False)
        app.checkbox(key="slack_approved_1").check().run()
        missing = Path(tempfile.mkdtemp()) / "slack-webhooks.json"
        # Point at an empty config so the test can never reach a real webhook.
        with mock.patch("app.slack_notifier.CONFIG_PATH", missing):
            _button(app, "Send to Slack").click().run()
        self.assertEqual(len(app.error), 1)


if __name__ == "__main__":
    unittest.main()
