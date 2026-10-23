"""Load and validate the version-controlled ingestion source registry."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import re
from typing import Any, Mapping

import yaml  # type: ignore[import-untyped]


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUPPORTED_TYPES = frozenset({"string", "integer", "boolean", "date", "timestamp"})


class ConfigurationError(ValueError):
    """Raised when the ingestion registry is unsafe or internally inconsistent."""


@dataclass(frozen=True)
class ColumnConfig:
    name: str
    data_type: str
    required: bool


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    source_file: str
    target_table: str
    landing_subpath: str
    keys: tuple[str, ...]
    columns: tuple[ColumnConfig, ...]
    enabled: bool = True

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class Registry:
    version: int
    sources: tuple[SourceConfig, ...]

    def enabled_sources(self, source_id: str = "") -> tuple[SourceConfig, ...]:
        selected = tuple(
            source
            for source in self.sources
            if source.enabled and (not source_id or source.source_id == source_id)
        )
        if source_id and not selected:
            raise ConfigurationError(f"Unknown or disabled source_id: {source_id}")
        return selected


def _require_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ConfigurationError(f"{label} must be a safe SQL identifier: {value!r}")
    return value


def _parse_column(name: Any, raw: Any, source_id: str) -> ColumnConfig:
    column_name = _require_identifier(name, f"{source_id}.column")
    if not isinstance(raw, Mapping):
        raise ConfigurationError(f"{source_id}.{column_name} must be a mapping")
    data_type = raw.get("type")
    if data_type not in SUPPORTED_TYPES:
        raise ConfigurationError(
            f"{source_id}.{column_name} has unsupported type {data_type!r}"
        )
    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise ConfigurationError(f"{source_id}.{column_name}.required must be boolean")
    return ColumnConfig(column_name, str(data_type), required)


def _parse_source(raw: Any) -> SourceConfig:
    if not isinstance(raw, Mapping):
        raise ConfigurationError("Each source must be a mapping")
    source_id = _require_identifier(raw.get("source_id"), "source_id")
    source_file = raw.get("source_file")
    if (
        not isinstance(source_file, str)
        or Path(source_file).name != source_file
        or not source_file.lower().endswith(".csv")
        or source_file.startswith(".")
    ):
        raise ConfigurationError(f"{source_id}.source_file is unsafe: {source_file!r}")
    target = _require_identifier(raw.get("target_table"), f"{source_id}.target_table")
    subpath = raw.get("landing_subpath")
    if (
        not isinstance(subpath, str)
        or not IDENTIFIER.fullmatch(subpath)
        or subpath.startswith("_")
    ):
        raise ConfigurationError(f"{source_id}.landing_subpath is unsafe: {subpath!r}")
    raw_columns = raw.get("columns")
    if not isinstance(raw_columns, Mapping) or not raw_columns:
        raise ConfigurationError(f"{source_id}.columns must be a non-empty mapping")
    columns = tuple(
        _parse_column(name, definition, source_id)
        for name, definition in raw_columns.items()
    )
    raw_keys = raw.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise ConfigurationError(f"{source_id}.keys must be a non-empty list")
    keys = tuple(_require_identifier(key, f"{source_id}.key") for key in raw_keys)
    unknown_keys = sorted(set(keys) - {column.name for column in columns})
    if unknown_keys:
        raise ConfigurationError(f"{source_id} keys not found in columns: {unknown_keys}")
    if len(keys) != len(set(keys)):
        raise ConfigurationError(f"{source_id} contains duplicate keys")
    required_by_name = {column.name: column.required for column in columns}
    optional_keys = [key for key in keys if not required_by_name[key]]
    if optional_keys:
        raise ConfigurationError(f"{source_id} keys must be required: {optional_keys}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigurationError(f"{source_id}.enabled must be boolean")
    return SourceConfig(
        source_id, source_file, target, subpath, keys, columns, enabled
    )


def load_registry(path: str | Path | None = None) -> Registry:
    """Load the packaged registry, or an explicit external registry override."""
    try:
        if path is None:
            registry_name = "packaged crestline_bronze/sources.yml"
            registry_text = (
                resources.files("crestline_bronze")
                .joinpath("sources.yml")
                .read_text(encoding="utf-8")
            )
        else:
            registry_path = Path(path)
            registry_name = str(registry_path)
            registry_text = registry_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(registry_text)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot load registry {registry_name}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigurationError("Registry root must be a mapping")
    if raw.get("version") != 1:
        raise ConfigurationError("Registry version must be 1")
    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigurationError("Registry sources must be a non-empty list")
    sources = tuple(_parse_source(source) for source in raw_sources)
    source_ids = [source.source_id for source in sources]
    targets = [source.target_table for source in sources]
    subpaths = [source.landing_subpath for source in sources]
    source_files = [source.source_file for source in sources]
    for values, label in (
        (source_ids, "source_id"),
        (targets, "target_table"),
        (subpaths, "landing_subpath"),
        (source_files, "source_file"),
    ):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise ConfigurationError(f"Duplicate {label} values: {duplicates}")
    return Registry(version=1, sources=sources)
