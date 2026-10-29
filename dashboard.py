"""Streamlit control room for the triage service.

Start the API first:  uvicorn app.main:app --reload
Then run:             streamlit run dashboard.py

Every decision comes from POST /triage. This file only builds requests and
displays responses; classification, override, fallback and guardrail logic
stay in the API.
"""

import json
import urllib.error
import urllib.request
from typing import Any

import pandas as pd
import streamlit as st

from app.demo_mode import (
    PRESENTER_NOTES,
    REPORT_FILENAME,
    build_decision_report,
    preset_cases,
)
from app.evaluation_dashboard import (
    HANDOFF_FILENAME,
    build_handoff,
    case_rows,
    compute_metrics,
    recommendation,
    run_evaluation,
)
from app.slack_notifier import CHANNELS, SlackNotifierError, send_decision
from app.slack_routing import (
    DESTINATIONS,
    RoutingPolicyError,
    approve_destination,
    default_destination,
    is_review_bypass,
)
from evals.helpers import load_cases

TRIAGE_URL = "http://127.0.0.1:8000/triage"
REQUEST_TIMEOUT_SECONDS = 30

PRIORITIES = ("P0", "P1", "P2", "P3")
ROUTING_TEAMS = ("security", "payments", "reliability", "platform", "product-support")
CRITICAL_PRIORITY = "P0"

HISTORY_KEY = "history"
EVALUATION_KEY = "evaluation_results"
DEMO_PRESET_KEY = "demo_preset"
DEMO_DECISION_KEY = "demo_decision"
DEMO_SLACK_PREFIX = "demo_slack"
CURRENT_KEY = "current_decision"
FORM_FIELDS = ("issue_id", "title", "body", "customer_impact")


class TriageRequestError(Exception):
    """The triage API could not produce a decision."""


def build_payload(
    issue_id: str,
    title: str,
    body: str,
    customer_impact: str,
    override: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Shape form values into the Issue JSON the API expects."""
    payload: dict[str, Any] = {
        "issue_id": issue_id.strip(),
        "title": title.strip(),
        "body": body,
        "customer_impact": customer_impact,
    }
    if override is not None:
        payload["human_override"] = override
    return payload


def post_triage(payload: dict[str, Any], url: str = TRIAGE_URL) -> dict[str, Any]:
    """POST one issue to the triage API and return its decision as a dict."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise TriageRequestError(f"API returned {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise TriageRequestError(f"Could not reach {url}: {error}") from error


def record_decision(history: list[dict[str, Any]], decision: dict[str, Any]) -> None:
    """Append a decision to the session history, newest last."""
    history.append(decision)


def compute_kpis(history: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "Issues triaged": len(history),
        "Critical issues": sum(1 for d in history if d["priority"] == CRITICAL_PRIORITY),
        "Needs review": sum(1 for d in history if d["requires_human_review"]),
        "Teams reached": len({d["routing_team"] for d in history}),
    }


def priority_distribution(history: list[dict[str, Any]]) -> dict[str, int]:
    """Count decisions per priority, always listing P0 to P3 in order."""
    counts = dict.fromkeys(PRIORITIES, 0)
    for decision in history:
        counts[decision["priority"]] += 1
    return counts


def history_rows(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Table rows for the history view, most recent first."""
    return [
        {
            "Issue": d["issue_id"],
            "Priority": d["priority"],
            "Team": d["routing_team"],
            "Human review": "yes" if d["requires_human_review"] else "no",
            "Source": d["decision_source"],
        }
        for d in reversed(history)
    ]


def review_fixture_id(cases: dict[str, Any]) -> str:
    """The first fixture whose frozen label expects human review."""
    return next(id_ for id_, case in cases.items() if case.expected["requires_human_review"])


def _load_review_fixture(cases: dict[str, Any]) -> None:
    st.session_state["fixture"] = review_fixture_id(cases)
    _load_fixture(cases)


def _load_fixture(cases: dict[str, Any]) -> None:
    """Copy the selected fixture into the editable form fields."""
    # A decision belongs to the issue that produced it; drop it so it can't outlive the fixture.
    st.session_state.pop(CURRENT_KEY, None)
    issue = cases[st.session_state["fixture"]].issue
    st.session_state["issue_id"] = issue.issue_id
    st.session_state["title"] = issue.title
    st.session_state["body"] = issue.body
    st.session_state["customer_impact"] = issue.customer_impact
    override = issue.human_override
    st.session_state["use_override"] = override is not None
    st.session_state["override_priority"] = override.priority if override else PRIORITIES[0]
    st.session_state["override_team"] = override.routing_team if override else ROUTING_TEAMS[0]
    st.session_state["override_reason"] = override.reason if override else ""


def _override_from_state() -> dict[str, str] | None:
    if not st.session_state["use_override"]:
        return None
    return {
        "priority": st.session_state["override_priority"],
        "routing_team": st.session_state["override_team"],
        "reason": st.session_state["override_reason"],
    }


def _render_slack_controls(decision: dict[str, Any], suffix: int, prefix: str = "slack") -> None:
    """Approve or override the Slack destination, then send. Keys change with each new decision."""
    default = default_destination(decision)
    st.markdown(f"**Slack controls for {decision['issue_id']}**")
    selected = st.selectbox(
        "Slack destination",
        DESTINATIONS,
        index=DESTINATIONS.index(default),
        format_func=lambda name: CHANNELS[name],
        key=f"{prefix}_destination_{suffix}",
    )
    st.caption(f"Default for this decision: {CHANNELS[default]}")
    reason = ""
    if is_review_bypass(decision, selected):
        reason = st.text_input("Reason for routing a review-required decision to team-routing", key=f"{prefix}_reason_{suffix}")
    approved = st.checkbox(f"Approve sending to {CHANNELS[selected]}", key=f"{prefix}_approved_{suffix}")

    if st.button("Send to Slack", disabled=not approved):
        try:
            destination = approve_destination(decision, selected, reason)
            send_decision(decision, destination, override_reason=reason)
        except (RoutingPolicyError, SlackNotifierError) as error:
            st.error(str(error))
        else:
            st.success(f"Sent to {CHANNELS[destination]}.")


def _run_and_store_evaluation(cases: dict[str, Any]) -> None:
    progress = st.progress(0.0, text="Starting evaluation")

    def on_progress(done: int, total: int, issue_id: str) -> None:
        progress.progress(done / total, text=f"Evaluated {done} of {total} ({issue_id})")

    try:
        st.session_state[EVALUATION_KEY] = run_evaluation(list(cases.values()), post_triage, on_progress)
    except TriageRequestError as error:
        st.session_state.pop(EVALUATION_KEY, None)
        st.error(f"Evaluation stopped: {error}")


def _clear_demo_decision() -> None:
    st.session_state.pop(DEMO_DECISION_KEY, None)


def _render_demo(history: list[dict[str, Any]], cases: dict[str, Any]) -> None:
    """Guided walkthrough on the same POST /triage and Slack policy paths as normal mode."""
    presets = preset_cases(list(cases.values()))
    st.subheader("Demo mode")
    st.caption("Five steps: select an issue, run live triage, review, choose the Slack destination, read the takeaway.")

    st.markdown("**Step 1. Select an issue**")
    name = st.radio("Preset", list(presets), key=DEMO_PRESET_KEY, horizontal=True, on_change=_clear_demo_decision)
    issue = presets[name].issue
    st.caption(PRESENTER_NOTES[name])
    st.text(f"{issue.issue_id}: {issue.title}")
    st.text_area("Body", issue.body, disabled=True, key=f"demo_body_{issue.issue_id}")
    st.text_area("Customer impact", issue.customer_impact, disabled=True, key=f"demo_impact_{issue.issue_id}")
    if issue.human_override is not None:
        override = issue.human_override
        st.info(f"Human override on this issue: {override.priority} / {override.routing_team}. Reason: {override.reason}")

    st.markdown("**Step 2. Run live triage**")
    if st.button("Run live triage", type="primary", key="demo_triage"):
        try:
            decision = post_triage(issue.model_dump(mode="json", exclude_none=True))
        except TriageRequestError as error:
            st.error(str(error))
        else:
            record_decision(history, decision)
            st.session_state[DEMO_DECISION_KEY] = decision

    decision = st.session_state.get(DEMO_DECISION_KEY)
    if decision is None:
        st.info("Run live triage to see the decision.")
    else:
        for column, (label, value) in zip(
            st.columns(4),
            (
                ("Priority", decision["priority"]),
                ("Routing team", decision["routing_team"]),
                ("Requires human review", str(decision["requires_human_review"]).lower()),
                ("Decision source", decision["decision_source"]),
            ),
        ):
            column.metric(label, value)
        st.json(decision)
        if decision["decision_source"] == "baseline_fallback":
            st.warning("The model was unavailable; this is the deterministic baseline.")

        st.markdown("**Steps 3 and 4. Review action and Slack destination**")
        st.caption("Approve the policy's destination, or change it. Sending a review-required decision to team-routing needs a reason.")
        suffix = len(history)
        _render_slack_controls(decision, suffix, prefix=DEMO_SLACK_PREFIX)
        destination = st.session_state[f"{DEMO_SLACK_PREFIX}_destination_{suffix}"]
        report = build_decision_report(
            issue,
            decision,
            destination,
            approved=st.session_state.get(f"{DEMO_SLACK_PREFIX}_approved_{suffix}", False),
            reason=st.session_state.get(f"{DEMO_SLACK_PREFIX}_reason_{suffix}", ""),
        )
        st.download_button("Download decision report", report, file_name=REPORT_FILENAME, mime="text/markdown")

    st.markdown("**Step 5. Evaluation takeaway**")
    if st.button("Run evaluation for the takeaway", key="demo_evaluation"):
        _run_and_store_evaluation(cases)
    results = st.session_state.get(EVALUATION_KEY)
    if results is None:
        st.info("Run the evaluation to see whether automation is safe to widen.")
    else:
        mode, explanation = recommendation(compute_metrics(results))
        st.markdown(f"**{mode}.** {explanation}")


def _render_evaluation(cases: dict[str, Any]) -> None:
    """Run every frozen case through POST /triage and score the routing. Sends nothing to Slack."""
    st.caption("Runs all fixtures through POST /triage and the Slack destination policy. No Slack messages are sent.")
    if st.button("Run evaluation"):
        _run_and_store_evaluation(cases)

    results = st.session_state.get(EVALUATION_KEY)
    if results is None:
        st.info("Run the evaluation to see routing metrics.")
        return

    metrics = compute_metrics(results)
    cards = {
        "Routing agreement": f"{metrics.routing_agreement:.0%}",
        "Automation coverage": f"{metrics.automation_coverage:.0%}",
        "Review volume": f"{metrics.review_volume} of {metrics.total}",
        "Unsafe automation": metrics.unsafe_automation,
    }
    for column, (label, value) in zip(st.columns(len(cards)), cards.items()):
        column.metric(label, value)

    mode, explanation = recommendation(metrics)
    (st.error if metrics.unsafe_automation else st.success)(f"**{mode}.** {explanation}")

    st.dataframe(case_rows(results), width="stretch")
    st.download_button(
        "Download handoff", build_handoff(results), file_name=HANDOFF_FILENAME, mime="text/markdown"
    )


def main() -> None:
    st.set_page_config(page_title="Triage control room", layout="wide")
    st.title("Triage control room")

    history: list[dict[str, Any]] = st.session_state.setdefault(HISTORY_KEY, [])
    cases = {case.issue.issue_id: case for case in load_cases()}

    if "fixture" not in st.session_state:
        st.session_state["fixture"] = next(iter(cases))
        _load_fixture(cases)

    demo_mode = st.sidebar.checkbox("Demo mode", key="demo_mode")
    labels = ["Demo", "Evaluation"] if demo_mode else ["Control room", "Evaluation"]
    first_tab, evaluation = st.tabs(labels)
    with first_tab:
        if demo_mode:
            _render_demo(history, cases)
        else:
            _render_control_room(history, cases)
    with evaluation:
        _render_evaluation(cases)


def _render_control_room(history: list[dict[str, Any]], cases: dict[str, Any]) -> None:
    kpis = compute_kpis(history)
    for column, (label, value) in zip(st.columns(len(kpis)), kpis.items()):
        column.metric(label, value)

    left, right = st.columns(2)
    with left:
        st.subheader("Issue")
        st.selectbox("Fixture", list(cases), key="fixture", on_change=_load_fixture, args=(cases,))
        st.button("Load review fixture", on_click=_load_review_fixture, args=(cases,))
        st.text_input("Issue ID", key="issue_id")
        st.text_input("Title", key="title")
        st.text_area("Body", key="body")
        st.text_area("Customer impact", key="customer_impact")
        st.checkbox("Human override", key="use_override")
        if st.session_state["use_override"]:
            st.selectbox("Override priority", PRIORITIES, key="override_priority")
            st.selectbox("Override team", ROUTING_TEAMS, key="override_team")
            st.text_input("Override reason", key="override_reason")

        if st.button("Triage issue", type="primary"):
            payload = build_payload(
                *(st.session_state[field] for field in FORM_FIELDS),
                override=_override_from_state(),
            )
            try:
                decision = post_triage(payload)
            except TriageRequestError as error:
                st.error(str(error))
            else:
                record_decision(history, decision)
                st.session_state[CURRENT_KEY] = decision

    with right:
        st.subheader("Current decision")
        current = st.session_state.get(CURRENT_KEY)
        if current is None:
            st.info("Submit an issue to see its decision.")
        else:
            st.json(current)
            if current["decision_source"] == "baseline_fallback":
                st.warning("The model was unavailable; this is the deterministic baseline.")
            _render_slack_controls(current, suffix=len(history))

    st.subheader("Priority distribution")
    counts = priority_distribution(history)
    st.bar_chart(pd.DataFrame({"Issues": list(counts.values())}, index=list(counts)))

    st.subheader("Session history")
    if history:
        st.dataframe(history_rows(history), width="stretch")
    else:
        st.caption("No issues triaged in this session yet.")


if __name__ == "__main__":
    main()
