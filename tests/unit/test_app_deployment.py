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
from crestline_ui.runtime import (
    application_provenance,
    create_workspace_client,
    load_runtime_config,
    log_application_provenance,
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


def test_apps_requirements_installs_project_non_editably() -> None:
    lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines == [".[ui]"]
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
