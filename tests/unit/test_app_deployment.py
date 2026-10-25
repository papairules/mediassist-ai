from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pytest import LogCaptureFixture, MonkeyPatch

from crestline_agent.cli import (
    DEFAULT_PROFILE,
    DEFAULT_VECTOR_INDEX,
    DEFAULT_WAREHOUSE_ID,
)
import crestline_agent.graph as agent_graph
from crestline_ui.app import (
    BACKEND_UNAVAILABLE_MESSAGE,
    MAX_PROMPT_CHARS,
    RATE_LIMIT_REQUESTS,
    _append_message,
    _allow_request,
    _prompt_limit_message,
)
from crestline_ui.runtime import (
    application_provenance,
    create_workspace_client,
    load_runtime_config,
    log_application_provenance,
    streamlit_secret_environment,
)


ROOT = Path(__file__).resolve().parents[2]


def test_app_manifest_uses_working_fixed_port_and_managed_resources() -> None:
    manifest = yaml.safe_load((ROOT / "app.yaml").read_text(encoding="utf-8"))

    assert manifest["command"] == [
        "streamlit",
        "run",
        "src/crestline_ui/app.py",
        "--server.address=0.0.0.0",
        "--server.port=8000",
    ]
    env = {item["name"]: item for item in manifest["env"]}
    assert env["PYTHONPATH"]["value"] == "src"
    assert env["MEDIASSIST_WAREHOUSE_ID"]["valueFrom"] == "sql-warehouse"
    assert env["MEDIASSIST_VECTOR_INDEX"]["valueFrom"] == "vector-search-index"
    assert env["DATABRICKS_LLM_ENDPOINT"]["valueFrom"] == "chat-endpoint"
    assert "DATABRICKS_CONFIG_PROFILE" not in env


def test_app_resource_bindings_are_read_only_and_match_manifest() -> None:
    config = json.loads(
        (ROOT / "conf" / "databricks_app_resources.json").read_text(encoding="utf-8")
    )
    resources = {item["name"]: item for item in config["resources"]}

    assert resources["sql-warehouse"]["sql_warehouse"] == {
        "id": "d249a9e5325ec021",
        "permission": "CAN_USE",
    }
    assert resources["chat-endpoint"]["serving_endpoint"] == {
        "name": "databricks-qwen3-next-80b-a3b-instruct",
        "permission": "CAN_QUERY",
    }
    vector = resources["vector-search-index"]["uc_securable"]
    assert vector == {
        "securable_full_name": "workspace.crestline_silver.document_retrieval_chunks_index",
        "securable_type": "TABLE",
        "permission": "SELECT",
    }
    uc_resources = [item["uc_securable"] for item in config["resources"] if "uc_securable" in item]
    assert len(uc_resources) == 7
    assert {item["permission"] for item in uc_resources} == {"SELECT"}


def test_databricks_app_uses_managed_identity_and_requires_bindings() -> None:
    missing = load_runtime_config(
        {
            "DATABRICKS_CLIENT_ID": "app-service-principal",
            "DATABRICKS_CLIENT_SECRET": "not-inspected",
        }
    )
    assert missing.profile is None
    assert not missing.ready
    assert set(missing.missing_resources) == {
        "SQL warehouse",
        "AI Search index",
        "chat endpoint",
    }
    configured = load_runtime_config(
        {
            "DATABRICKS_CLIENT_ID": "app-service-principal",
            "DATABRICKS_CLIENT_SECRET": "not-inspected",
            "DATABRICKS_CONFIG_PROFILE": "must-not-be-used",
            "MEDIASSIST_WAREHOUSE_ID": "warehouse",
            "MEDIASSIST_VECTOR_INDEX": "catalog.schema.index",
            "DATABRICKS_LLM_ENDPOINT": "endpoint",
        }
    )
    assert configured.ready
    assert configured.profile is None


def test_local_runtime_preserves_existing_profile_and_resource_defaults() -> None:
    config = load_runtime_config({"DATABRICKS_LLM_ENDPOINT": "local-endpoint"})

    assert config.profile == DEFAULT_PROFILE
    assert config.warehouse_id == DEFAULT_WAREHOUSE_ID
    assert config.vector_index == DEFAULT_VECTOR_INDEX
    assert not config.is_streamlit_cloud


def test_streamlit_cloud_secrets_use_explicit_oauth_and_never_local_profile() -> None:
    secrets = {
        "MEDIASSIST_HOSTED_MODE": "streamlit_cloud",
        "DATABRICKS_HOST": "https://dbc-example.cloud.databricks.com",
        "DATABRICKS_CLIENT_ID": "demo-client",
        "DATABRICKS_CLIENT_SECRET": "demo-secret",
        "MEDIASSIST_WAREHOUSE_ID": "warehouse",
        "MEDIASSIST_VECTOR_INDEX": "catalog.schema.index",
        "DATABRICKS_LLM_ENDPOINT": "endpoint",
        "UNRELATED_SECRET": "ignored",
    }
    config = load_runtime_config(
        {"DATABRICKS_CONFIG_PROFILE": "must-not-be-used"},
        secrets,
    )

    assert config.is_streamlit_cloud
    assert config.profile is None
    assert config.ready
    assert config.databricks_host == secrets["DATABRICKS_HOST"]
    assert config.client_id == secrets["DATABRICKS_CLIENT_ID"]
    assert config.client_secret == secrets["DATABRICKS_CLIENT_SECRET"]
    assert "UNRELATED_SECRET" not in streamlit_secret_environment(secrets)


def test_streamlit_cloud_without_oauth_secret_fails_closed() -> None:
    config = load_runtime_config(
        {},
        {
            "MEDIASSIST_HOSTED_MODE": "streamlit_cloud",
            "DATABRICKS_HOST": "https://dbc-example.cloud.databricks.com",
            "DATABRICKS_CLIENT_ID": "demo-client",
            "MEDIASSIST_WAREHOUSE_ID": "warehouse",
            "MEDIASSIST_VECTOR_INDEX": "catalog.schema.index",
            "DATABRICKS_LLM_ENDPOINT": "endpoint",
        },
    )
    assert not config.ready
    assert "OAuth client secret" in config.missing_resources


def test_missing_hosted_configuration_reports_names_without_values() -> None:
    config = load_runtime_config(
        {},
        {
            "MEDIASSIST_HOSTED_MODE": "streamlit_cloud",
            "DATABRICKS_HOST": "https://dbc-example.cloud.databricks.com",
            "DATABRICKS_CLIENT_ID": "demo-client",
        },
    )

    assert not config.ready
    assert set(config.missing_resources) == {
        "SQL warehouse",
        "AI Search index",
        "chat endpoint",
        "OAuth client secret",
    }
    assert "demo-client" not in ", ".join(config.missing_resources)


def test_initialization_failure_preserves_prompt_and_uses_backend_message() -> None:
    state: dict[str, list[dict[str, str]]] = {"messages": []}

    _append_message("user", "Where is ORD-1001?", state)
    state["messages"].append({"role": "assistant", "content": BACKEND_UNAVAILABLE_MESSAGE})

    assert state["messages"][0]["content"] == "Where is ORD-1001?"
    assert "Backend temporarily unavailable" in state["messages"][1]["content"]
    assert "INSUFFICIENT_EVIDENCE" not in state["messages"][1]["content"]


def test_successful_submission_can_record_prompt_once() -> None:
    state: dict[str, list[dict[str, str]]] = {"messages": []}
    _append_message("user", "Where is ORD-1001?", state)
    state["messages"].append({"role": "assistant", "content": "ORD-1001 evidence"})

    assert [message["role"] for message in state["messages"]] == ["user", "assistant"]
    assert state["messages"][0]["content"] == "Where is ORD-1001?"


def test_workspace_client_omits_profile_for_managed_app_identity(
    monkeypatch: MonkeyPatch,
) -> None:
    import databricks.sdk

    calls: list[dict[str, str]] = []

    def fake_workspace_client(**kwargs: str) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", fake_workspace_client)

    create_workspace_client(None)
    create_workspace_client("local-profile")

    assert "profile" not in calls[0]
    assert calls[1]["profile"] == "local-profile"


def test_workspace_client_accepts_explicit_streamlit_oauth_configuration(
    monkeypatch: MonkeyPatch,
) -> None:
    import databricks.sdk

    calls: list[dict[str, str]] = []

    def fake_workspace_client(**kwargs: str) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", fake_workspace_client)
    create_workspace_client(
        None,
        host="https://dbc-example.cloud.databricks.com",
        client_id="demo-client",
        client_secret="demo-secret",
    )

    assert calls[0]["host"] == "https://dbc-example.cloud.databricks.com"
    assert calls[0]["client_id"] == "demo-client"
    assert calls[0]["client_secret"] == "demo-secret"
    assert "profile" not in calls[0]


def test_apps_requirements_installs_project_non_editably() -> None:
    lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines == [
        ".[ui]",
        "streamlit==1.37.1",
        "numpy>=2.0,<3",
        "databricks-sdk==0.156.0",
        "langgraph==1.2.16",
    ]
    assert all(not line.startswith(("-e", "--editable")) for line in lines)


def test_application_provenance_identifies_loaded_agent_source() -> None:
    provenance = application_provenance({"MEDIASSIST_BUILD_ID": "release-v3-test"})
    graph_path = Path(agent_graph.__file__).resolve()

    assert provenance.graph_module_path == str(graph_path)
    assert provenance.graph_sha256 == hashlib.sha256(graph_path.read_bytes()).hexdigest()
    assert provenance.package_version
    assert provenance.build_id == "release-v3-test"


def test_startup_provenance_log_contains_only_code_identifiers(
    caplog: LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="crestline_ui.runtime"):
        provenance = log_application_provenance(
            {
                "MEDIASSIST_BUILD_ID": "invalid build id with spaces",
                "DATABRICKS_CLIENT_SECRET": "must-not-appear",
            }
        )

    assert provenance.build_id.startswith("mediassist-")
    message = caplog.messages[-1]
    assert provenance.graph_module_path in message
    assert provenance.graph_sha256 in message
    assert provenance.package_version in message
    assert provenance.build_id in message
    assert "must-not-appear" not in message


def test_prompt_size_limit_rejects_oversized_requests_without_calling_services() -> None:
    assert _prompt_limit_message("x" * MAX_PROMPT_CHARS) is None
    message = _prompt_limit_message("x" * (MAX_PROMPT_CHARS + 1))
    assert message is not None
    assert "No Databricks or model request was made" in message


def test_request_limit_is_scoped_to_one_session_window() -> None:
    state: dict[str, list[float]] = {}
    for index in range(RATE_LIMIT_REQUESTS):
        assert _allow_request(state, float(index))
    assert not _allow_request(state, float(RATE_LIMIT_REQUESTS))
    assert _allow_request(state, 601.0)
