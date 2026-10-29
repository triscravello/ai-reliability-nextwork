"""Deterministic Slack destination policy for a guarded triage decision.

The destination depends only on the decision's requires_human_review flag, which
the orchestrator derives from the guarded priority and team. Model output never
reaches this module, so Gemini cannot choose where a decision is posted.
"""

from typing import Any

REVIEW_DESTINATION = "triage-review"
ROUTING_DESTINATION = "team-routing"
DESTINATIONS = (REVIEW_DESTINATION, ROUTING_DESTINATION)


class RoutingPolicyError(Exception):
    """A human-approved destination breaks the review boundary."""


def default_destination(decision: dict[str, Any]) -> str:
    """Review-required decisions go to triage-review; the rest go to team-routing."""
    return REVIEW_DESTINATION if decision["requires_human_review"] else ROUTING_DESTINATION


def is_review_bypass(decision: dict[str, Any], selected: str) -> bool:
    """True when a review-required decision is being sent to team-routing."""
    return decision["requires_human_review"] and selected == ROUTING_DESTINATION


def approve_destination(decision: dict[str, Any], selected: str, reason: str = "") -> str:
    """Return the destination a human may send to, or raise if the policy forbids it."""
    if selected not in DESTINATIONS:
        raise RoutingPolicyError(f"Unknown Slack destination: {selected}.")
    if is_review_bypass(decision, selected) and not reason.strip():
        raise RoutingPolicyError(
            "Routing a review-required decision to team-routing needs a reason."
        )
    return selected
