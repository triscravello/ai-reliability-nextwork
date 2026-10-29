"""Evaluation tab: scoring, recommendation, handoff and the no-Slack guarantee.

Triage is replaced by fakes or the real orchestrator with the provider offline,
so no server, API key or webhook is involved.
"""

import hashlib
import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

import dashboard
from app import evaluation_dashboard as ev
from app.model_provider import ModelProviderError
from app.models import Issue
from app.orchestrator import TriageOrchestrator
from evals.helpers import FIXTURE_PATH, load_cases

CASES = load_cases()
REVIEW_IDS = [c.issue.issue_id for c in CASES if c.expected["requires_human_review"]]
ROUTINE_IDS = [c.issue.issue_id for c in CASES if not c.expected["requires_human_review"]]


def _decision(issue_id: str, review: bool, source: str = "model-assisted") -> dict:
    return {
        "issue_id": issue_id,
        "priority": "P0" if review else "P3",
        "routing_team": "security" if review else "product-support",
        "requires_human_review": review,
        "decision_source": source,
        "summary": "stub",
    }


def _triage_matching(flips: set[str] = frozenset()):
    """A fake API that answers with each case's frozen label, flipping the chosen ids."""
    expected = {c.issue.issue_id: c.expected["requires_human_review"] for c in CASES}

    def triage(payload: dict) -> dict:
        issue_id = payload["issue_id"]
        return _decision(issue_id, expected[issue_id] != (issue_id in flips))

    return triage


class _Offline:
    def suggest(self, issue: Issue) -> None:
        raise ModelProviderError("offline")


class RunTest(unittest.TestCase):
    def test_every_fixture_is_triaged_and_the_fixtures_are_untouched(self) -> None:
        before = hashlib.sha256(Path(FIXTURE_PATH).read_bytes()).hexdigest()
        seen: list[dict] = []

        def triage(payload: dict) -> dict:
            seen.append(payload)
            return _triage_matching()(payload)

        results = ev.run_evaluation(CASES, triage)

        self.assertEqual([r.issue_id for r in results], [c.issue.issue_id for c in CASES])
        self.assertEqual(len(seen), len(CASES))
        self.assertIn("human_override", next(p for p in seen if p["issue_id"] == "ISSUE-010"))
        self.assertNotIn("human_override", seen[0])
        self.assertEqual(hashlib.sha256(Path(FIXTURE_PATH).read_bytes()).hexdigest(), before)

    def test_progress_is_reported_for_each_case(self) -> None:
        calls: list[tuple] = []
        ev.run_evaluation(CASES, _triage_matching(), on_progress=lambda *a: calls.append(a))
        self.assertEqual([c[0] for c in calls], list(range(1, len(CASES) + 1)))
        self.assertEqual({c[1] for c in calls}, {len(CASES)})

    def test_destinations_come_from_the_policy(self) -> None:
        results = ev.run_evaluation(CASES, _triage_matching())
        for result in results:
            review = result.issue_id in REVIEW_IDS
            expected = "triage-review" if review else "team-routing"
            self.assertEqual((result.expected_destination, result.actual_destination), (expected, expected))

    def test_the_real_orchestrator_offline_has_no_unsafe_automation(self) -> None:
        orchestrator = TriageOrchestrator(_Offline())
        triage = lambda payload: orchestrator.triage(Issue.model_validate(payload)).model_dump()
        metrics = ev.compute_metrics(ev.run_evaluation(CASES, triage))
        self.assertEqual(metrics.unsafe_automation, 0)
        self.assertEqual(metrics.routing_agreement, 1.0)


class MetricsTest(unittest.TestCase):
    def test_perfect_agreement_recommends_only_a_supervised_pilot(self) -> None:
        metrics = ev.compute_metrics(ev.run_evaluation(CASES, _triage_matching()))
        self.assertEqual(metrics.total, len(CASES))
        self.assertEqual(metrics.routing_agreement, 1.0)
        self.assertEqual(metrics.review_volume, len(REVIEW_IDS))
        self.assertAlmostEqual(metrics.automation_coverage, len(ROUTINE_IDS) / len(CASES))
        self.assertEqual(metrics.unsafe_automation, 0)
        self.assertEqual(ev.recommendation(metrics)[0], ev.SUPERVISED_PILOT)

    def test_automating_a_review_case_is_unsafe_and_recommends_shadow_mode(self) -> None:
        results = ev.run_evaluation(CASES, _triage_matching(flips={REVIEW_IDS[0]}))
        metrics = ev.compute_metrics(results)
        self.assertEqual(metrics.unsafe_automation, 1)
        self.assertLess(metrics.routing_agreement, 1.0)
        self.assertEqual(ev.recommendation(metrics)[0], ev.SHADOW_MODE)

    def test_extra_review_disagrees_but_is_not_unsafe(self) -> None:
        results = ev.run_evaluation(CASES, _triage_matching(flips={ROUTINE_IDS[0]}))
        metrics = ev.compute_metrics(results)
        self.assertEqual(metrics.unsafe_automation, 0)
        self.assertLess(metrics.routing_agreement, 1.0)
        self.assertEqual(ev.recommendation(metrics)[0], ev.SUPERVISED_PILOT)

    def test_empty_results_do_not_divide_by_zero(self) -> None:
        metrics = ev.compute_metrics([])
        self.assertEqual((metrics.total, metrics.routing_agreement, metrics.automation_coverage), (0, 0.0, 0.0))

    def test_case_rows_show_expected_and_actual_destinations(self) -> None:
        rows = ev.case_rows(ev.run_evaluation(CASES, _triage_matching(flips={REVIEW_IDS[0]})))
        flipped = next(row for row in rows if row["Issue"] == REVIEW_IDS[0])
        self.assertEqual(flipped["Expected destination"], "triage-review")
        self.assertEqual(flipped["Actual destination"], "team-routing")
        self.assertEqual(flipped["Unsafe automation"], "yes")


class HandoffTest(unittest.TestCase):
    def test_filename_and_content(self) -> None:
        self.assertEqual(ev.HANDOFF_FILENAME, "ai-triage-handoff.md")
        text = ev.build_handoff(ev.run_evaluation(CASES, _triage_matching(flips={REVIEW_IDS[0]})))
        for expected in ("# AI triage handoff", "Shadow mode", "Unsafe automation: 1", REVIEW_IDS[0], "sends no Slack messages"):
            self.assertIn(expected, text)

    def test_handoff_for_a_clean_run_recommends_a_pilot(self) -> None:
        text = ev.build_handoff(ev.run_evaluation(CASES, _triage_matching()))
        self.assertIn("Supervised pilot", text)
        self.assertNotIn("Shadow mode", text)


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass


class EvaluationTabTest(unittest.TestCase):
    def _run_tab(self, flips: set[str] = frozenset()) -> tuple[AppTest, mock.MagicMock]:
        triage = _triage_matching(flips)

        def fake_urlopen(request: object, timeout: float) -> _Response:
            return _Response(json.dumps(triage(json.loads(request.data))).encode())  # type: ignore[attr-defined]

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen) as urlopen:
            app = AppTest.from_file(str(Path(dashboard.__file__))).run()
            next(b for b in app.button if b.label == "Run evaluation").click().run()
        return app, urlopen

    def test_tab_posts_every_fixture_to_triage_and_never_to_slack(self) -> None:
        app, urlopen = self._run_tab()

        self.assertFalse(app.exception)
        urls = [call.args[0].full_url for call in urlopen.call_args_list]
        self.assertEqual(urls, [dashboard.TRIAGE_URL] * len(CASES))
        self.assertEqual(len(app.session_state[dashboard.EVALUATION_KEY]), len(CASES))

    def test_tab_shows_metrics_and_the_recommendation(self) -> None:
        app, _ = self._run_tab()
        labels = [m.label for m in app.metric]
        for label in ("Routing agreement", "Automation coverage", "Review volume", "Unsafe automation"):
            self.assertIn(label, labels)
        self.assertIn("Supervised pilot", app.success[0].value)

    def test_tab_recommends_shadow_mode_when_unsafe(self) -> None:
        app, _ = self._run_tab(flips={REVIEW_IDS[0]})
        self.assertIn("Shadow mode", app.error[0].value)

    def test_tab_offers_the_handoff_download(self) -> None:
        app, _ = self._run_tab()
        self.assertEqual(len(app.get("download_button")), 1)

    def test_api_failure_stops_the_run_without_results(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            app = AppTest.from_file(str(Path(dashboard.__file__))).run()
            next(b for b in app.button if b.label == "Run evaluation").click().run()

        self.assertNotIn(dashboard.EVALUATION_KEY, app.session_state)
        self.assertTrue(any("Evaluation stopped" in e.value for e in app.error))


if __name__ == "__main__":
    unittest.main()
