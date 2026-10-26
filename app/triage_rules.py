"""Deterministic triage policy.

Rules are checked in order and the first match wins, so a rule's position in
_RULES is its precedence. Matching is case-insensitive substring search over
the issue title, body, and customer impact.
"""

from typing import NamedTuple

from app.models import Issue, Priority, RoutingTeam, TriageDecision

# Phrase sets for urgent signals.
CRITICAL_SECURITY_PHRASES = frozenset({
    "api key exposed", "api keys exposed", "credentials exposed", "exposed credentials",
    "leaked credentials", "leaked api key", "public repository",
})
GLOBAL_IMPACT_PHRASES = frozenset({
    "all customers", "every customer", "every region", "all regions", "globally",
    "worldwide", "entire platform",
})
OUTAGE_PHRASES = frozenset({
    "outage", "is down", "are down", "unavailable", "cannot", "fails", "failing",
})
SECURITY_INVESTIGATION_PHRASES = frozenset({
    "authorization bypass", "bypass authorization", "another tenant", "privilege escalation",
    "unauthorized access", "suspicious login",
})
WIDESPREAD_IMPACT_PHRASES = frozenset({
    "widespread", "many customers", "multiple regions", "hundreds of users",
    "large number of customers",
})

# Phrase sets for broad, routine categories.
PAYMENT_PHRASES = frozenset({"payment", "checkout", "invoice", "billing", "refund", "charged"})
INTEGRATION_PHRASES = frozenset({"webhook", "integration", "callback", "sync"})
PERFORMANCE_PHRASES = frozenset({"slow", "latency", "timeout", "load time"})
DOCUMENTATION_PHRASES = frozenset({"documentation", "docs", "guide", "typo", "tutorial"})
FEATURE_REQUEST_PHRASES = frozenset({
    "feature request", "would like", "please add", "it would be great", "dark mode",
})


class _Rule(NamedTuple):
    """Matches when the text contains at least one phrase from every group."""

    name: str
    phrase_groups: tuple[frozenset[str], ...]
    priority: Priority
    routing_team: RoutingTeam


# Ordered from most to least urgent. The first matching rule wins.
_RULES: tuple[_Rule, ...] = (
    # TODO 2: Apply critical security and global-impact rules before broad category rules.
    # Handle the most urgent and specific policies first
    _Rule("Critical security exposure", (CRITICAL_SECURITY_PHRASES,), "P0", "security"),
    _Rule("Global payment outage", (PAYMENT_PHRASES, GLOBAL_IMPACT_PHRASES, OUTAGE_PHRASES), "P0", "payments"),
    _Rule("Global service outage", (GLOBAL_IMPACT_PHRASES, OUTAGE_PHRASES), "P0", "reliability"),
    _Rule("Security investigation", (SECURITY_INVESTIGATION_PHRASES,), "P1", "security"),
    # TODO 3: Route widespread customer impact before routine fallback behavior. 
    _Rule("Widespread customer impact",
    (WIDESPREAD_IMPACT_PHRASES,), "P1", "reliability"),

    _Rule("General payment issue", (PAYMENT_PHRASES,), "P2", "payments"),
    _Rule("Integration issue", (INTEGRATION_PHRASES,), "P2", "platform"),
    _Rule("Performance issue", (PERFORMANCE_PHRASES,), "P2", "reliability"),
    _Rule("Documentation issue", (DOCUMENTATION_PHRASES,), "P3", "product-support"),
    _Rule("Feature request", (FEATURE_REQUEST_PHRASES,), "P3", "product-support"),
)


def requires_human_review(priority: Priority, routing_team: RoutingTeam) -> bool:
    """Urgent or security-sensitive work always gets a human in the loop."""
    return priority in ("P0", "P1") or routing_team == "security"


def _searchable_text(issue: Issue) -> str:
    return " ".join((issue.title, issue.body, issue.customer_impact)).lower()


def _matches(rule: _Rule, text: str) -> bool:
    return all(any(phrase in text for phrase in group) for group in rule.phrase_groups)


def _decision(issue: Issue, priority: Priority, routing_team: RoutingTeam, summary: str) -> TriageDecision:
    return TriageDecision(
        issue_id=issue.issue_id,
        priority=priority,
        routing_team=routing_team,
        requires_human_review=requires_human_review(priority, routing_team),
        decision_source="baseline",
        summary=summary,
    )


def _override_decision(issue: Issue) -> TriageDecision:
    """Restate a person's explicit decision as a typed triage outcome."""
    override = issue.human_override
    if override is None:
        raise ValueError(f"{issue.issue_id} has no human override.")
    decision = _decision(
        issue,
        override.priority,
        override.routing_team,
        f"Human override: {override.reason}",
    )
    return decision.model_copy(update={"decision_source": "human_override"})


def classify_issue(issue: Issue) -> TriageDecision:
    # TODO 1: Preserve an explicit human override before automated matching.
    if issue.human_override is not None:
        return _override_decision(issue)

    text = _searchable_text(issue)
    for rule in _RULES:
        if _matches(rule, text):
            return _decision(issue, rule.priority, rule.routing_team, f"{rule.name}: {issue.title}.")

    return _decision(issue, "P3", "product-support", f"Routine request: {issue.title}.")
