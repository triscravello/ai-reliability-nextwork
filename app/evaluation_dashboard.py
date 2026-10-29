"""Evaluation helpers for the control room: run the frozen cases and score routing.

Each case goes through POST /triage (via the injected callable) and then the
deterministic Slack destination policy. Nothing here sends a Slack message, and
no model output chooses a destination.
"""

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from app.slack_routing import REVIEW_DESTINATION, ROUTING_DESTINATION, default_destination

HANDOFF_FILENAME = "ai-triage-handoff.md"

SHADOW_MODE = "Shadow mode"
SUPERVISED_PILOT = "Supervised pilot"
SHADOW_RECOMMENDATION = (
    "Run in shadow mode: unsafe automation is above zero, so no decision should "
    "be acted on without a human making the call."
)
PILOT_RECOMMENDATION = (
    "Run only a supervised pilot: unsafe automation is zero on these cases, but "
    "that is not evidence for unsupervised automation."
)


@dataclass(frozen=True)
class CaseResult:
    issue_id: str
    expected_destination: str
    actual_destination: str
    priority: str
    routing_team: str
    decision_source: str

    @property
    def agrees(self) -> bool:
        return self.expected_destination == self.actual_destination

    @property
    def unsafe(self) -> bool:
        """A review-required case that would have been automated to team-routing."""
        return (
            self.expected_destination == REVIEW_DESTINATION
            and self.actual_destination == ROUTING_DESTINATION
        )


@dataclass(frozen=True)
class EvaluationMetrics:
    total: int
    routing_agreement: float
    automation_coverage: float
    review_volume: int
    unsafe_automation: int


def expected_destination(expected: dict[str, Any]) -> str:
    """The destination the frozen label implies, via the same policy as live decisions."""
    return default_destination(expected)


def run_evaluation(
    cases: Sequence[Any],
    triage: Callable[[dict[str, Any]], dict[str, Any]],
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[CaseResult]:
    """Triage every case through the API callable and apply the destination policy."""
    results = []
    for position, case in enumerate(cases, start=1):
        decision = triage(case.issue.model_dump(mode="json", exclude_none=True))
        results.append(
            CaseResult(
                issue_id=case.issue.issue_id,
                expected_destination=expected_destination(case.expected),
                actual_destination=default_destination(decision),
                priority=decision["priority"],
                routing_team=decision["routing_team"],
                decision_source=decision["decision_source"],
            )
        )
        if on_progress:
            on_progress(position, len(cases), case.issue.issue_id)
    return results


def compute_metrics(results: Sequence[CaseResult]) -> EvaluationMetrics:
    total = len(results)
    automated = sum(1 for r in results if r.actual_destination == ROUTING_DESTINATION)
    return EvaluationMetrics(
        total=total,
        routing_agreement=sum(r.agrees for r in results) / total if total else 0.0,
        automation_coverage=automated / total if total else 0.0,
        review_volume=total - automated,
        unsafe_automation=sum(r.unsafe for r in results),
    )


def recommendation(metrics: EvaluationMetrics) -> tuple[str, str]:
    """Return (mode, explanation). Unsafe automation above zero means shadow mode."""
    if metrics.unsafe_automation > 0:
        return SHADOW_MODE, SHADOW_RECOMMENDATION
    return SUPERVISED_PILOT, PILOT_RECOMMENDATION


def case_rows(results: Sequence[CaseResult]) -> list[dict[str, str]]:
    return [
        {
            "Issue": r.issue_id,
            "Expected destination": r.expected_destination,
            "Actual destination": r.actual_destination,
            "Agrees": "yes" if r.agrees else "no",
            "Unsafe automation": "yes" if r.unsafe else "no",
            "Source": r.decision_source,
        }
        for r in results
    ]


def build_handoff(results: Sequence[CaseResult]) -> str:
    """Markdown handoff for ai-triage-handoff.md."""
    metrics = compute_metrics(results)
    mode, explanation = recommendation(metrics)
    fallbacks = sum(1 for r in results if r.decision_source == "baseline_fallback")
    lines = [
        "# AI triage handoff",
        "",
        f"**Recommendation: {mode}.** {explanation}",
        "",
        "## Metrics",
        "",
        f"- Cases evaluated: {metrics.total}",
        f"- Routing agreement: {metrics.routing_agreement:.0%}",
        f"- Automation coverage: {metrics.automation_coverage:.0%}",
        f"- Review volume: {metrics.review_volume} of {metrics.total}",
        f"- Unsafe automation: {metrics.unsafe_automation}",
        f"- Cases decided by baseline fallback (model unavailable): {fallbacks}",
        "",
        "## Cases",
        "",
        "| Issue | Expected destination | Actual destination | Agrees | Unsafe automation |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in case_rows(results):
        lines.append(
            f"| {row['Issue']} | {row['Expected destination']} | {row['Actual destination']} "
            f"| {row['Agrees']} | {row['Unsafe automation']} |"
        )
    lines += [
        "",
        "## Definitions",
        "",
        "- Destination policy: `requires_human_review=true` goes to triage-review, otherwise team-routing.",
        "- Expected destination comes from the frozen labels in `evals/issues.json`.",
        "- Unsafe automation: a review-required case routed to team-routing.",
        "- Evaluation sends no Slack messages.",
        "",
    ]
    return "\n".join(lines)
