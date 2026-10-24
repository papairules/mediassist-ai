"""Runtime configuration for local Streamlit and Databricks Apps."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import metadata
import logging
from pathlib import Path
import re
from typing import Any, Mapping

import crestline_agent.graph as agent_graph

from crestline_agent.cli import (
    DEFAULT_PROFILE,
    DEFAULT_VECTOR_INDEX,
    DEFAULT_WAREHOUSE_ID,
)


LOGGER = logging.getLogger(__name__)
BUILD_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@dataclass(frozen=True)
class ApplicationProvenance:
    """Non-sensitive identifiers for the code loaded by the app process."""

    graph_module_path: str
    graph_sha256: str
    package_version: str
    build_id: str


def application_provenance(environ: Mapping[str, str]) -> ApplicationProvenance:
    """Describe loaded agent code without logging requests or authorization state."""

    module_file = getattr(agent_graph, "__file__", None)
    if not module_file:
        raise RuntimeError("crestline_agent.graph has no filesystem location")
    module_path = Path(module_file).resolve()
    graph_sha256 = hashlib.sha256(module_path.read_bytes()).hexdigest()
    try:
        package_version = metadata.version("mediassist-bronze")
    except metadata.PackageNotFoundError:
        package_version = "not-installed"
    configured_build_id = environ.get("MEDIASSIST_BUILD_ID", "").strip()
    build_id = (
        configured_build_id
        if BUILD_ID_PATTERN.fullmatch(configured_build_id)
        else f"mediassist-{package_version}-{graph_sha256[:12]}"
    )
    return ApplicationProvenance(
        graph_module_path=str(module_path),
        graph_sha256=graph_sha256,
        package_version=package_version,
        build_id=build_id,
    )


def log_application_provenance(environ: Mapping[str, str]) -> ApplicationProvenance:
    """Emit one structured startup diagnostic containing code provenance only."""

    provenance = application_provenance(environ)
    LOGGER.info(
        "MediAssist startup provenance graph_module_path=%s graph_sha256=%s "
        "package_version=%s build_id=%s",
        provenance.graph_module_path,
        provenance.graph_sha256,
        provenance.package_version,
        provenance.build_id,
    )
    return provenance


@dataclass(frozen=True)
class AppRuntimeConfig:
    """Resolved app dependencies without credentials or authorization state."""

    profile: str | None
    warehouse_id: str
    vector_index: str
    llm_endpoint: str
    is_databricks_app: bool

    @property
    def missing_resources(self) -> tuple[str, ...]:
        values = {
            "SQL warehouse": self.warehouse_id,
            "AI Search index": self.vector_index,
            "chat endpoint": self.llm_endpoint,
        }
        return tuple(label for label, value in values.items() if not value)

    @property
    def ready(self) -> bool:
        return not self.missing_resources


def load_runtime_config(environ: Mapping[str, str]) -> AppRuntimeConfig:
    """Resolve managed App resources or retain the established local defaults.

    Databricks Apps injects a client ID and secret for its dedicated service
    principal. When that identity is present, no named developer profile is
    passed to the SDK and all managed resource bindings are required.
    """

    is_databricks_app = bool(environ.get("DATABRICKS_CLIENT_ID"))
    profile = None if is_databricks_app else environ.get(
        "DATABRICKS_CONFIG_PROFILE", DEFAULT_PROFILE
    )
    local_warehouse_default = "" if is_databricks_app else DEFAULT_WAREHOUSE_ID
    local_index_default = "" if is_databricks_app else DEFAULT_VECTOR_INDEX
    return AppRuntimeConfig(
        profile=profile,
        warehouse_id=environ.get("MEDIASSIST_WAREHOUSE_ID", local_warehouse_default).strip(),
        vector_index=environ.get("MEDIASSIST_VECTOR_INDEX", local_index_default).strip(),
        llm_endpoint=environ.get("DATABRICKS_LLM_ENDPOINT", "").strip(),
        is_databricks_app=is_databricks_app,
    )


def create_workspace_client(profile: str | None) -> Any:
    """Create an SDK client using managed auth unless a local profile is explicit."""

    from databricks.sdk import WorkspaceClient

    if profile is None:
        return WorkspaceClient(
            product="mediassist-streamlit",
            product_version="0.1.0",
        )
    return WorkspaceClient(
        profile=profile,
        product="mediassist-streamlit",
        product_version="0.1.0",
    )
