"""Premium Streamlit workspace for the MediAssist LangGraph agent."""

from __future__ import annotations

from datetime import date
import html
from importlib.resources import files
import logging
import os
import time
from typing import Any
import uuid

import streamlit as st

from crestline_agent.cli import SYNTHETIC_AS_OF, build_demo_only_authorization
from crestline_agent.graph import MediAssistAgent
from crestline_agent.llm import DatabricksIntentClassifier
from crestline_retrieval.client import DatabricksBackend
from crestline_retrieval.service import RetrievalService

from crestline_ui.presentation import Citation, ResponseView, parse_response
from crestline_ui.runtime import (
    create_workspace_client,
    load_runtime_config,
    log_application_provenance,
    streamlit_secret_environment,
)


LOGGER = logging.getLogger(__name__)
DEMO_SCOPES = ("east", "west")
ACTION_PROMPTS = {
    "Track Orders": "Where is ORD-1001?",
    "Supplier Updates": "What is the supplier ETA for ORD-1003 line 1?",
    "Substitution Checks": (
        "For ORD-1002 line 1, can BR-IV22-BLU-LF replace "
        "AV-IV22-BLU-LF for hospital H-E102?"
    ),
    "Document Intelligence": "Find current document evidence for AV-IV22-BLU-LF.",
}
OUTCOME_LABELS = {
    "BLOCKED": ("Blocked", "blocked"),
    "INSUFFICIENT_EVIDENCE": ("Insufficient evidence", "insufficient"),
    "ELIGIBLE_FOR_HUMAN_REVIEW": ("Conditionally eligible — human review required", "review"),
    "ACCESS_DENIED": ("Access denied", "denied"),
}
MAX_PROMPT_CHARS = 1200
RATE_LIMIT_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 600.0
BACKEND_UNAVAILABLE_MESSAGE = (
    "Backend temporarily unavailable. Your question was preserved; please try again shortly."
)


@st.cache_resource(show_spinner=False)
def _log_startup_provenance() -> None:
    log_application_provenance(os.environ)


def _load_css() -> None:
    stylesheet = files("crestline_ui").joinpath("styles.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{stylesheet}</style>", unsafe_allow_html=True)


def _environment_has_managed_runtime(environ: dict[str, str]) -> bool:
    """Return whether Databricks supplies the complete app configuration."""

    required = (
        "DATABRICKS_CLIENT_ID",
        "MEDIASSIST_WAREHOUSE_ID",
        "MEDIASSIST_VECTOR_INDEX",
        "DATABRICKS_LLM_ENDPOINT",
    )
    return all(environ.get(key, "").strip() for key in required)


def _streamlit_cloud_secrets() -> dict[str, str]:
    """Read allow-listed Community Cloud settings when needed."""

    if _environment_has_managed_runtime(dict(os.environ)):
        return {}
    try:
        secrets = st.secrets
        load_if_toml_exists = getattr(secrets, "load_if_toml_exists", None)
        if load_if_toml_exists is not None and not load_if_toml_exists():
            return {}
        return streamlit_secret_environment(secrets)
    except (FileNotFoundError, KeyError):
        return {}


@st.cache_resource(show_spinner=False)
def _retrieval_dependencies(
    profile: str | None,
    databricks_host: str | None,
    client_id: str | None,
    client_secret: str | None,
    warehouse_id: str,
    vector_index: str,
    llm_endpoint: str,
) -> tuple[RetrievalService, DatabricksIntentClassifier]:
    workspace = create_workspace_client(
        profile,
        host=databricks_host,
        client_id=client_id,
        client_secret=client_secret,
    )
    service = RetrievalService(
        DatabricksBackend(workspace, warehouse_id),
        vector_index=vector_index,
    )
    return service, DatabricksIntentClassifier(workspace, llm_endpoint)


def _allow_request(state: Any, now: float | None = None) -> bool:
    """Limit requests per Streamlit session, not a global anonymous-user quota."""

    current = time.monotonic() if now is None else now
    timestamps = [
        float(value)
        for value in state.get("mediassist_request_timestamps", [])
        if current - float(value) < RATE_LIMIT_WINDOW_SECONDS
    ]
    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        state["mediassist_request_timestamps"] = timestamps
        return False
    timestamps.append(current)
    state["mediassist_request_timestamps"] = timestamps
    return True


def _prompt_limit_message(prompt: str) -> str | None:
    if len(prompt) > MAX_PROMPT_CHARS:
        return (
            f"Please shorten your request to {MAX_PROMPT_CHARS} characters or fewer. "
            "No Databricks or model request was made."
        )
    return None


def _new_conversation(scope: str) -> None:
    st.session_state.messages = []
    st.session_state.thread_id = f"ui-{uuid.uuid4()}"
    st.session_state.agent = MediAssistAgent()
    st.session_state.active_scope = scope


def _initialize_state(scope: str) -> None:
    if "messages" not in st.session_state or st.session_state.get("active_scope") != scope:
        _new_conversation(scope)


def _header(ready: bool) -> None:
    status = "Databricks configured" if ready else "Configuration required"
    dot_class = "" if ready else " pending"
    st.markdown(
        f"""
        <header class="ma-header">
          <div class="ma-brand">
            <div class="ma-mark" aria-hidden="true">MA</div>
            <div>
              <div class="ma-brand-name">MediAssist AI</div>
              <div class="ma-brand-org">Crestline Medical Distribution</div>
            </div>
          </div>
          <div class="ma-status" role="status">
            <span class="ma-status-dot{dot_class}"></span>{status}
          </div>
        </header>
        """,
        unsafe_allow_html=True,
    )


def _sidebar(
    scope: str,
    ready: bool,
    missing_resources: tuple[str, ...] = (),
) -> str:
    with st.sidebar:
        st.markdown("### MediAssist AI")
        st.caption("Evidence-grounded distribution intelligence")
        if st.button("New conversation", type="primary", use_container_width=True):
            _new_conversation(scope)
            st.rerun()
        selected_value = st.selectbox(
            "Synthetic demo access",
            DEMO_SCOPES,
            index=DEMO_SCOPES.index(scope),
            format_func=lambda value: f"{value.title()} region",
            help="DEMO_ONLY scope. Production access must come from trusted server-side identity.",
        )
        selected = str(selected_value or scope)
        authorization = build_demo_only_authorization(selected)
        hospitals = ", ".join(sorted(authorization.hospital_regions))
        st.markdown(
            f"""
            <div class="ma-scope-card">
              <div class="ma-scope-title">Authorized synthetic scope</div>
              <div class="ma-scope-value">{html.escape(selected.title())} · {html.escape(hospitals)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.caption("Authorization is rebuilt server-side for every request. Conversation memory is not an access source.")
        st.divider()
        st.caption(f"Session {str(st.session_state.thread_id)[-8:]}")
        st.caption(f"{len(st.session_state.messages)} messages")
        if not ready:
            st.warning("Configure the required Databricks resources to enable conversations.")
            if missing_resources:
                st.caption("Missing configuration: " + ", ".join(missing_resources))
        st.caption("Clinical and contractual decisions always require authoritative evidence and qualified review.")
    return selected


def _welcome(ready: bool) -> str | None:
    st.markdown(
        """
        <section class="ma-welcome">
          <div class="ma-eyebrow">Trusted operational intelligence</div>
          <h1>How can MediAssist help?</h1>
          <p>Explore order fulfillment, supplier evidence, inventory, and controlled document intelligence with source-level citations.</p>
        </section>
        """,
        unsafe_allow_html=True,
    )
    columns = st.columns(4)
    descriptions = {
        "Track Orders": "Shipment and fulfillment visibility",
        "Supplier Updates": "Restock evidence and uncertainty",
        "Substitution Checks": "Deterministic evidence validation",
        "Document Intelligence": "Current and historical sources",
    }
    selected: str | None = None
    for index, (label, prompt) in enumerate(ACTION_PROMPTS.items()):
        with columns[index]:
            if st.button(
                f"{label}\n\n{descriptions[label]}",
                key=f"action_{index}",
                use_container_width=True,
                disabled=not ready,
            ):
                selected = prompt
    return selected


def _metric_cards(view: ResponseView) -> None:
    if not view.metrics:
        return
    columns = st.columns(min(4, len(view.metrics)))
    for index, metric in enumerate(view.metrics):
        with columns[index % len(columns)]:
            st.markdown(
                f"""
                <div class="ma-metric">
                  <div class="ma-metric-label">{html.escape(metric.label)}</div>
                  <div class="ma-metric-value">{html.escape(metric.value)}</div>
                  <div class="ma-metric-detail">{html.escape(metric.detail)}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def _candidate_outcome_cards(view: ResponseView) -> None:
    if not view.candidate_outcomes:
        return
    columns = st.columns(min(2, len(view.candidate_outcomes)))
    for index, candidate in enumerate(view.candidate_outcomes):
        label, css_class = OUTCOME_LABELS[candidate.outcome]
        if candidate.inventory_observed is True:
            inventory = f"{candidate.inventory_available or 'Unknown'} observed units available"
        elif candidate.inventory_observed is False:
            inventory = "No current inventory observation"
        else:
            inventory = "Inventory evidence unavailable"
        reasons = "".join(
            f"<li>{html.escape(reason)}</li>" for reason in candidate.human_reasons
        ) or "<li>No reason detail supplied.</li>"
        clinical_review = (
            '<div class="ma-review-required">Requires qualified clinical review — not approved</div>'
            if candidate.outcome == "ELIGIBLE_FOR_HUMAN_REVIEW"
            else ""
        )
        with columns[index % len(columns)]:
            st.markdown(
                f"""
                <div class="ma-candidate">
                  <div class="ma-candidate-sku">{html.escape(candidate.sku)}</div>
                  <div class="ma-outcome {css_class}">{html.escape(label)}</div>
                  {clinical_review}
                  <ul class="ma-candidate-reasons">{reasons}</ul>
                  <div class="ma-candidate-inventory">{html.escape(inventory)}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def _source_card(citation: Citation) -> None:
    if citation.document_id:
        if citation.is_historical:
            historical_kind = (
                "revoked"
                if (citation.row_status or "").upper() in {"REVOKED", "APPROVED (REVOKED)"}
                else "superseded"
            )
            card_class, badge, badge_class = (
                "historical",
                f"Historical / {historical_kind}",
                "historical",
            )
        elif citation.is_current:
            card_class, badge, badge_class = "current", "Current authoritative evidence", "current"
        else:
            card_class, badge, badge_class = "", "Evidence reviewed", "reviewed"
        title = html.escape(citation.document_id)
        metadata = (
            f"Version {html.escape(citation.version or 'unknown')} · "
            f"Page {html.escape(citation.page or 'unknown')} · "
            f"Record {html.escape(citation.record_id or 'unknown')} · "
            f"SHA-256 {html.escape(citation.sha256 or 'unknown')}"
        )
        if citation.row_status:
            metadata += f" · Row status {html.escape(citation.row_status)}"
        st.markdown(
            f"""
            <div class="ma-source {card_class}">
              <div class="ma-source-title">{title}<span class="ma-badge {badge_class}">{badge}</span></div>
              <div class="ma-source-meta">{metadata}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return
    details = " · ".join(f"{key}: {value}" for key, value in citation.details)
    st.markdown(
        f"""
        <div class="ma-source">
          <div class="ma-source-title">Operational source record</div>
          <div class="ma-source-meta">{html.escape(details or citation.record_id or 'Record available')}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _assistant_message(text: str) -> None:
    view = parse_response(text)
    if view.candidate_outcomes:
        st.markdown(
            f'<div class="ma-discovery-summary">{html.escape(view.candidate_headline or "Alternatives evaluated")}</div>',
            unsafe_allow_html=True,
        )
        if view.candidate_summary and view.candidate_summary != view.candidate_headline:
            st.caption(view.candidate_summary)
        st.caption(
            "Semantic search discovered exact candidate pairs; deterministic verification produced each outcome. No candidate is clinically approved."
        )
        _candidate_outcome_cards(view)
        evidence_count = sum(len(candidate.citations) for candidate in view.candidate_outcomes)
        with st.expander(f"View supporting evidence · {evidence_count}", expanded=False):
            if view.supporting_text:
                st.markdown(view.supporting_text)
            for candidate in view.candidate_outcomes:
                st.markdown(f"**{candidate.sku} · {candidate.outcome}**")
                if candidate.reason_codes:
                    st.caption(f"Deterministic reason codes: {', '.join(candidate.reason_codes)}")
                for citation in candidate.citations:
                    _source_card(citation)
        return
    if view.outcome:
        label, css_class = OUTCOME_LABELS[view.outcome]
        st.markdown(
            f'<div class="ma-outcome {css_class}" role="status">{html.escape(label)}</div>',
            unsafe_allow_html=True,
        )
    if view.summary_text:
        st.markdown(f"### {html.escape(view.summary_text)}")
    if view.brief_explanation:
        st.markdown(html.escape(view.brief_explanation))
    _metric_cards(view)
    if view.supporting_text or view.citations:
        with st.expander(f"View supporting evidence · {len(view.citations)}", expanded=False):
            if view.supporting_text:
                st.markdown(view.supporting_text)
            for citation in view.citations:
                _source_card(citation)


def _render_history() -> None:
    for message in st.session_state.messages:
        role = str(message["role"])
        with st.chat_message(role):
            if role == "assistant":
                _assistant_message(str(message["content"]))
            else:
                st.markdown(str(message["content"]))


def _append_message(role: str, content: str, state: Any | None = None) -> None:
    target = st.session_state if state is None else state
    target["messages"].append({"role": role, "content": content})
    if state is None:
        LOGGER.info("message_saved role=%s count=%d", role, len(target["messages"]))


def _invoke(
    prompt: str,
    scope: str,
    service: RetrievalService,
    classifier: DatabricksIntentClassifier,
    as_of: date,
    *,
    user_message_recorded: bool = False,
) -> None:
    LOGGER.info("prompt_received chars=%d", len(prompt))
    prompt_error = _prompt_limit_message(prompt)
    if prompt_error:
        st.warning(prompt_error)
        return
    if not _allow_request(st.session_state):
        st.warning(
            "This demo session has reached its request limit. Please wait a few minutes "
            "before trying again."
        )
        return
    if not user_message_recorded:
        _append_message("user", prompt)
        with st.chat_message("user"):
            st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Reviewing authoritative evidence…"):
            try:
                authorization = build_demo_only_authorization(scope)
                started = time.monotonic()
                LOGGER.info("agent_invoked")
                reply = st.session_state.agent.invoke(
                    prompt,
                    thread_id=st.session_state.thread_id,
                    service=service,
                    authorization=authorization,
                    classifier=classifier,
                    as_of=as_of,
                )
                response = reply.text
                LOGGER.info(
                    "response_received chars=%d elapsed_ms=%d",
                    len(response),
                    int((time.monotonic() - started) * 1000),
                )
            except Exception:
                LOGGER.exception("MediAssist request failed")
                response = BACKEND_UNAVAILABLE_MESSAGE
            _assistant_message(response)
    _append_message("assistant", response)


def main() -> None:
    st.set_page_config(
        page_title="MediAssist AI · Crestline",
        page_icon=None,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _log_startup_provenance()
    _load_css()
    runtime = load_runtime_config(os.environ, _streamlit_cloud_secrets())
    ready = runtime.ready
    LOGGER.info("app_initialized ready=%s message_count=%d", ready, len(st.session_state.get("messages", [])))

    initial_scope = str(st.session_state.get("active_scope", "east"))
    if initial_scope not in DEMO_SCOPES:
        initial_scope = "east"
    _initialize_state(initial_scope)
    selected_scope = _sidebar(initial_scope, ready, runtime.missing_resources)
    if selected_scope != st.session_state.active_scope:
        _new_conversation(selected_scope)
        st.rerun()

    _header(ready)
    action_prompt = _welcome(ready) if not st.session_state.messages else None
    _render_history()
    if not st.session_state.messages and not ready:
        st.markdown(
            '<div class="ma-empty">Configure the existing Databricks warehouse, AI Search index, and model endpoint to begin. No infrastructure is provisioned by this application.</div>',
            unsafe_allow_html=True,
        )

    prompt = st.chat_input(
        "Ask about an order, supplier ETA, inventory, contract, or exact substitution…",
        disabled=not ready,
    )
    submitted = action_prompt or prompt
    if not submitted or not ready:
        return
    _append_message("user", submitted)
    with st.chat_message("user"):
        st.markdown(submitted)
    LOGGER.info("prompt_received chars=%d", len(submitted))
    try:
        LOGGER.info("backend_initialization_started")
        service, classifier = _retrieval_dependencies(
            runtime.profile,
            runtime.databricks_host,
            runtime.client_id,
            runtime.client_secret,
            runtime.warehouse_id,
            runtime.vector_index,
            runtime.llm_endpoint,
        )
        LOGGER.info("backend_initialized")
    except Exception:
        LOGGER.exception("MediAssist configuration failed")
        with st.chat_message("assistant"):
            st.warning(BACKEND_UNAVAILABLE_MESSAGE)
        _append_message("assistant", BACKEND_UNAVAILABLE_MESSAGE)
        return
    _invoke(
        submitted,
        selected_scope,
        service,
        classifier,
        SYNTHETIC_AS_OF,
        user_message_recorded=True,
    )


if __name__ == "__main__":
    main()
