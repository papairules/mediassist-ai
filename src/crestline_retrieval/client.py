"""Mockable Databricks SQL and Vector Search client boundary."""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol, Sequence

from .models import SqlParameter


class RetrievalBackendError(RuntimeError):
    """Raised when Databricks returns an incomplete or failed response."""


class RetrievalBackend(Protocol):
    def query(
        self,
        statement: str,
        parameters: Sequence[SqlParameter] = (),
    ) -> list[Mapping[str, Any]]: ...

    def vector_search(
        self,
        *,
        index_name: str,
        query_text: str,
        columns: Sequence[str],
        num_results: int,
        query_type: str,
        filters: Mapping[str, Any] | None = None,
    ) -> list[Mapping[str, Any]]: ...


def _decode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("[", "{")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


def _response_rows(response: Any) -> list[Mapping[str, Any]]:
    manifest = getattr(response, "manifest", None)
    result = getattr(response, "result", None)
    schema = getattr(manifest, "schema", None)
    columns = getattr(schema, "columns", None) if schema is not None else None
    if columns is None:
        columns = getattr(manifest, "columns", None)
    data = getattr(result, "data_array", None) if result is not None else None
    if not data:
        return []
    names = [getattr(column, "name", None) for column in columns or []]
    if not names or any(name is None for name in names):
        raise RetrievalBackendError("Databricks response omitted result column names")
    if any(len(row) != len(names) for row in data):
        raise RetrievalBackendError("Databricks response row width does not match its schema")
    safe_names = [str(name) for name in names]
    return [dict(zip(safe_names, (_decode(value) for value in row))) for row in data]


class DatabricksBackend:
    """Thin SDK adapter; authentication remains with the injected WorkspaceClient."""

    def __init__(self, workspace_client: Any, warehouse_id: str) -> None:
        if not warehouse_id:
            raise ValueError("warehouse_id is required")
        self._workspace = workspace_client
        self._warehouse_id = warehouse_id

    def query(
        self,
        statement: str,
        parameters: Sequence[SqlParameter] = (),
    ) -> list[Mapping[str, Any]]:
        from databricks.sdk.service.sql import (
            ExecuteStatementRequestOnWaitTimeout,
            StatementParameterListItem,
            StatementState,
        )

        response = self._workspace.statement_execution.execute_statement(
            statement=statement,
            warehouse_id=self._warehouse_id,
            parameters=[
                StatementParameterListItem(
                    name=parameter.name,
                    value=parameter.value,
                    type=parameter.type,
                )
                for parameter in parameters
            ],
            wait_timeout="30s",
            on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
        )
        state = getattr(getattr(response, "status", None), "state", None)
        if state != StatementState.SUCCEEDED:
            raise RetrievalBackendError(f"Databricks SQL statement failed closed: {state}")
        return _response_rows(response)

    def vector_search(
        self,
        *,
        index_name: str,
        query_text: str,
        columns: Sequence[str],
        num_results: int,
        query_type: str,
        filters: Mapping[str, Any] | None = None,
    ) -> list[Mapping[str, Any]]:
        response = self._workspace.vector_search_indexes.query_index(
            index_name=index_name,
            query_text=query_text,
            columns=list(columns),
            num_results=num_results,
            query_type=query_type,
            filters_json=(json.dumps(filters, sort_keys=True) if filters else None),
        )
        return _response_rows(response)
