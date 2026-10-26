from app.model_provider import GeminiModelProvider, ModelProvider, ModelProviderError
from app.models import Issue, ModelSuggestion, TriageDecision
from app.triage_rules import classify_issue, requires_human_review

# Lower rank means more urgent.
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

PROVIDER_UNAVAILABLE_NOTE = "Model provider unavailable; baseline decision kept."


class TriageOrchestrator:
    """Combines the deterministic baseline with an optional model suggestion."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    def triage(self, issue: Issue) -> TriageDecision:
        baseline = classify_issue(issue)

        # TODO 1: Return a human override before calling the provider.
        if baseline.decision_source == "human_override":
            return baseline

        # TODO 2: Catch provider failures and return a baseline fallback.
        # Fall back to the trusted baseline when the provider is unavailable
        try:
            suggestion = self._provider.suggest(issue)
        except ModelProviderError:
            return baseline.model_copy(update={
                "decision_source": "baseline_fallback",
                "summary": f"{baseline.summary} {PROVIDER_UNAVAILABLE_NOTE}",
            })

        return self._apply_suggestion(baseline, suggestion)

    def _apply_suggestion(self, baseline: TriageDecision, suggestion: ModelSuggestion) -> TriageDecision:
        # TODO 3: Accept escalations while rejecting priority downgrades.
        # Accept only suggestions that increase urgency
        is_escalation = _PRIORITY_RANK[suggestion.priority] < _PRIORITY_RANK[baseline.priority]
        priority = suggestion.priority if is_escalation else baseline.priority
        routing_team = suggestion.routing_team if is_escalation else baseline.routing_team

        return baseline.model_copy(update={
            "priority": priority,
            "routing_team": routing_team,
            "requires_human_review": baseline.requires_human_review
            or requires_human_review(priority, routing_team),
            "decision_source": "model-assisted",
            "summary": suggestion.summary,
        })


def build_orchestrator() -> TriageOrchestrator:
    """Production wiring: the real Gemini provider behind the guarded orchestrator."""
    return TriageOrchestrator(GeminiModelProvider())
