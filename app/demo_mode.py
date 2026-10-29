"""Demo mode helpers: named presets and the Markdown decision report.

Presets are chosen from the frozen fixtures by what they demonstrate, so the
fixtures stay the single source. Every preset still goes through POST /triage
and the Slack destination policy; nothing here classifies or routes.
"""

from typing import Any, Sequence

from app.slack_routing import default_destination

REPORT_FILENAME = "ai-triage-decision-report.md"

ROUTINE_ROUTE = "Routine route"
HUMAN_REVIEW = "Human review"
OVERRIDE_WALKTHROUGH = "Override walkthrough"

PRESENTER_NOTES = {
    ROUTINE_ROUTE: "A low-risk issue. The policy should send it straight to #team-routing.",
    HUMAN_REVIEW: "A high-risk issue. The policy should hold it for a person in #triage-review.",
    OVERRIDE_WALKTHROUGH: (
        "A person has already decided this issue. The API skips the model and keeps their call; "
        "the destination policy still applies."
    ),
}

APPROVED_DEFAULT = "Approved the default destination"
OVERRODE_DEFAULT = "Overrode the default destination"
PENDING = "Pending approval"


def preset_cases(cases: Sequence[Any]) -> dict[str, Any]:
    """Pick one fixture per preset: routine, review-required, and human-overridden."""
    def first(match: Any, name: str) -> Any:
        found = next((case for case in cases if match(case)), None)
        if found is None:
            raise LookupError(f"No fixture available for the {name} preset.")
        return found

    return {
        ROUTINE_ROUTE: first(
            lambda c: not c.expected["requires_human_review"] and c.issue.human_override is None,
            ROUTINE_ROUTE,
        ),
        HUMAN_REVIEW: first(
            lambda c: c.expected["requires_human_review"] and c.issue.human_override is None,
            HUMAN_REVIEW,
        ),
        OVERRIDE_WALKTHROUGH: first(lambda c: c.issue.human_override is not None, OVERRIDE_WALKTHROUGH),
    }


def reviewer_action(default: str, selected: str, approved: bool) -> str:
    if not approved:
        return PENDING
    return APPROVED_DEFAULT if selected == default else OVERRODE_DEFAULT


def build_decision_report(
    issue: Any,
    decision: dict[str, Any],
    destination: str,
    approved: bool,
    reason: str = "",
) -> str:
    """Markdown report of exactly what the presenter saw and chose."""
    default = default_destination(decision)
    action = reviewer_action(default, destination, approved)
    reviewer_reason = reason.strip()
    if reviewer_reason:
        reason_line = f"{reviewer_reason} (reviewer-provided)"
    else:
        reason_line = (
            f"Policy default: requires_human_review={str(decision['requires_human_review']).lower()} "
            f"routes to {default} (no reviewer reason given)"
        )
    lines = [
        "# AI triage decision report",
        "",
        "## Issue",
        "",
        f"- Issue ID: {issue.issue_id}",
        f"- Title: {issue.title}",
        f"- Body: {issue.body}",
        f"- Customer impact: {issue.customer_impact or 'none stated'}",
    ]
    if issue.human_override is not None:
        override = issue.human_override
        lines.append(
            f"- Human override on the issue: {override.priority} / {override.routing_team} "
            f"(reason: {override.reason})"
        )
    lines += [
        "",
        "## Decision (from POST /triage)",
        "",
        f"- Priority: {decision['priority']}",
        f"- Routing team: {decision['routing_team']}",
        f"- Requires human review: {str(decision['requires_human_review']).lower()}",
        f"- Decision source: {decision['decision_source']}",
        f"- Summary: {decision['summary']}",
        "",
        "## Reviewer action",
        "",
        f"- Action: {action}",
        f"- Default destination: {default}",
        f"- Chosen destination: {destination}",
        f"- Reason: {reason_line}",
        "",
    ]
    return "\n".join(lines)
