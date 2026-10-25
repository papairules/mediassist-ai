"""Runtime configuration for local, Databricks Apps, and hosted Streamlit."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import metadata
import logging
import os
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
SDK_HTTP_TIMEOUT_SECONDS = 30.0
SDK_RETRY_TIMEOUT_SECONDS = 45
BUILD_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
STREAMLIT_SECRET_KEYS = frozenset(
    {
        "MEDIASSIST_HOSTED_MODE",
        "DATABRICKS_HOST",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "MEDIASSIST_WAREHOUSE_ID",
        "MEDIASSIST_VECTOR_INDEX",
        "DATABRICKS_LLM_ENDPOINT",
        "MEDIASSIST_BUILD_ID",
    }
)


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
    is_streamlit_cloud: bool
    databricks_host: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    @property
    def missing_resources(self) -> tuple[str, ...]:
        values = {
            "SQL warehouse": self.warehouse_id,
            "AI Search index": self.vector_index,
            "chat endpoint": self.llm_endpoint,
        }
        if self.is_streamlit_cloud:
            values.update(
                {
                    "Databricks host": self.databricks_host or "",
                    "OAuth client ID": self.client_id or "",
                    "OAuth client secret": self.client_secret or "",
                }
            )
        return tuple(label for label, value in values.items() if not value)

    @property
    def ready(self) -> bool:
        return not self.missing_resources


def streamlit_secret_environment(secrets: Mapping[str, Any]) -> dict[str, str]:
    """Copy only known, non-authorization settings from Streamlit secrets.

    The OAuth secret is intentionally returned only for in-process SDK setup;
    it is never logged or displayed. Unknown secret keys are ignored.
    """

    return {
        key: str(secrets[key]).strip()
        for key in STREAMLIT_SECRET_KEYS
        if key in secrets and str(secrets[key]).strip()
    }


def load_runtime_config(
    environ: Mapping[str, str],
    secrets: Mapping[str, Any] | None = None,
) -> AppRuntimeConfig:
    """Resolve managed App resources or retain the established local defaults.

    Databricks Apps injects a client ID and secret for its dedicated service
    principal. When that identity is present, no named developer profile is
    passed to the SDK and all managed resource bindings are required.
    """

    merged = dict(environ)
    if secrets is not None:
        merged.update(streamlit_secret_environment(secrets))
    is_streamlit_cloud = merged.get("MEDIASSIST_HOSTED_MODE", "").lower() == "streamlit_cloud"
    is_databricks_app = bool(merged.get("DATABRICKS_CLIENT_ID")) and not is_streamlit_cloud
    profile = None if is_databricks_app or is_streamlit_cloud else merged.get(
        "DATABRICKS_CONFIG_PROFILE", DEFAULT_PROFILE
    )
    local_warehouse_default = "" if is_databricks_app or is_streamlit_cloud else DEFAULT_WAREHOUSE_ID
    local_index_default = "" if is_databricks_app or is_streamlit_cloud else DEFAULT_VECTOR_INDEX
    return AppRuntimeConfig(
        profile=profile,
        warehouse_id=merged.get("MEDIASSIST_WAREHOUSE_ID", local_warehouse_default).strip(),
        vector_index=merged.get("MEDIASSIST_VECTOR_INDEX", local_index_default).strip(),
        llm_endpoint=merged.get("DATABRICKS_LLM_ENDPOINT", "").strip(),
        is_databricks_app=is_databricks_app,
        is_streamlit_cloud=is_streamlit_cloud,
        databricks_host=merged.get("DATABRICKS_HOST", "").strip() or None,
        client_id=merged.get("DATABRICKS_CLIENT_ID", "").strip() or None,
        client_secret=merged.get("DATABRICKS_CLIENT_SECRET", "").strip() or None,
    )


def create_workspace_client(
    profile: str | None,
    *,
    host: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> Any:
    """Create an SDK client using explicit hosted or local authentication."""

    from databricks.sdk import WorkspaceClient

    # The SDK reads these standard settings when constructing its Config. Keep
    # the bounds process-local and do not expose or log any credentials.
    os.environ.setdefault("DATABRICKS_HTTP_TIMEOUT_SECONDS", str(SDK_HTTP_TIMEOUT_SECONDS))
    os.environ.setdefault("DATABRICKS_RETRY_TIMEOUT_SECONDS", str(SDK_RETRY_TIMEOUT_SECONDS))

    if host and client_id and client_secret:
        return WorkspaceClient(
            host=host,
            client_id=client_id,
            client_secret=client_secret,
            product="mediassist-streamlit",
            product_version="0.1.0",
        )
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
