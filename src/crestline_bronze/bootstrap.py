"""Bootstrap registered synthetic CSVs into a Unity Catalog Volume.

The module uses the Databricks CLI as a subprocess so it shares the user's
existing authentication profile without reading or handling credentials.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Protocol, Sequence

from crestline_bronze.config import Registry, SourceConfig, load_registry


LOGGER = logging.getLogger(__name__)
DEFAULT_PROFILE = "abinroy90@gmail.com"
DEFAULT_VOLUME_ROOT = (
    "dbfs:/Volumes/workspace/crestline_bronze/mediassist_ingestion"
)


class BootstrapError(RuntimeError):
    """Raised when preflight validation or a Volume operation fails."""


@dataclass(frozen=True)
class RemoteEntry:
    path: str
    is_directory: bool
    size: int | None = None

    @property
    def name(self) -> str:
        return PurePosixPath(self.path.removeprefix("dbfs:")).name


@dataclass(frozen=True)
class UploadPlan:
    source: SourceConfig
    local_file: Path
    remote_directory: str
    remote_file: str


@dataclass(frozen=True)
class BootstrapSummary:
    created_directories: int
    uploaded_files: int
    skipped_files: int
    failures: tuple[str, ...]


class VolumeOperations(Protocol):
    def list_directory(self, path: str) -> tuple[RemoteEntry, ...]: ...

    def create_directory(self, path: str) -> None: ...

    def read_file(self, path: str) -> bytes: ...

    def upload_file(self, local_path: Path, remote_path: str, overwrite: bool) -> None: ...


class DatabricksCliVolume:
    """Small Databricks CLI adapter with a fixed, non-secret profile name."""

    def __init__(self, profile: str, executable: str = "databricks") -> None:
        if not profile.strip():
            raise BootstrapError("Databricks CLI profile cannot be empty")
        self.profile = profile
        self.executable = executable

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        command = [self.executable, *arguments, "--profile", self.profile]
        LOGGER.debug("Running CLI command: %s", " ".join(command[:-2] + ["--profile", self.profile]))
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=False,
            )
        except OSError as exc:
            raise BootstrapError(f"Unable to execute Databricks CLI: {exc}") from exc
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise BootstrapError(
                f"Databricks CLI failed ({result.returncode}): {message or command[1]}"
            )
        return result

    def list_directory(self, path: str) -> tuple[RemoteEntry, ...]:
        result = self._run(["fs", "ls", path, "--long", "--absolute", "-o", "json"])
        try:
            payload: Any = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BootstrapError(f"Invalid JSON returned while listing {path}") from exc
        if isinstance(payload, dict):
            payload = payload.get("files", payload.get("entries", []))
        if not isinstance(payload, list):
            raise BootstrapError(f"Unexpected listing response for {path}")
        entries: list[RemoteEntry] = []
        for item in payload:
            if not isinstance(item, dict):
                raise BootstrapError(f"Malformed listing entry for {path}: {item!r}")
            entry_path = item.get("path", item.get("name"))
            if not isinstance(entry_path, str) or not entry_path:
                raise BootstrapError(f"Malformed listing entry for {path}: {item!r}")
            is_directory = bool(item.get("is_directory", item.get("is_dir", False)))
            raw_size = item.get("file_size", item.get("size"))
            size = int(raw_size) if raw_size is not None else None
            entries.append(RemoteEntry(entry_path, is_directory, size))
        return tuple(entries)

    def create_directory(self, path: str) -> None:
        self._run(["fs", "mkdir", path])

    def read_file(self, path: str) -> bytes:
        return self._run(["fs", "cat", path]).stdout

    def upload_file(self, local_path: Path, remote_path: str, overwrite: bool) -> None:
        arguments = ["fs", "cp", str(local_path), remote_path]
        if overwrite:
            arguments.append("--overwrite")
        self._run(arguments)


def _join_remote(root: str, *parts: str) -> str:
    return "/".join([root.rstrip("/"), *(part.strip("/") for part in parts)])


def build_upload_plan(
    registry: Registry, local_source_directory: Path, volume_root: str
) -> tuple[UploadPlan, ...]:
    """Validate every local input before any remote operation is attempted."""
    if not volume_root.startswith("dbfs:/Volumes/"):
        raise BootstrapError("volume_root must use dbfs:/Volumes/... syntax")
    plans: list[UploadPlan] = []
    missing: list[Path] = []
    invalid: list[Path] = []
    for source in registry.enabled_sources():
        local_file = local_source_directory / source.source_file
        if not local_file.exists():
            missing.append(local_file)
        elif not local_file.is_file():
            invalid.append(local_file)
        remote_directory = _join_remote(
            volume_root, "landing", source.landing_subpath
        )
        plans.append(
            UploadPlan(
                source=source,
                local_file=local_file,
                remote_directory=remote_directory,
                remote_file=_join_remote(remote_directory, source.source_file),
            )
        )
    if missing or invalid:
        details = [*(f"missing: {path}" for path in missing)]
        details.extend(f"not a file: {path}" for path in invalid)
        raise BootstrapError("Local source preflight failed: " + "; ".join(details))
    return tuple(plans)


def bootstrap_sources(
    registry: Registry,
    local_source_directory: Path,
    volume_root: str,
    volume: VolumeOperations,
    overwrite_changed: bool = False,
) -> BootstrapSummary:
    """Create source directories and upload or skip all registered CSV files."""
    plans = build_upload_plan(registry, local_source_directory, volume_root)
    landing_root = _join_remote(volume_root, "landing")
    try:
        landing_entries = {entry.name: entry for entry in volume.list_directory(landing_root)}
    except BootstrapError:
        LOGGER.exception("Failed to list landing directory %s", landing_root)
        raise

    created = 0
    uploaded = 0
    skipped = 0
    failures: list[str] = []
    for plan in plans:
        source_id = plan.source.source_id
        try:
            directory_entry = landing_entries.get(plan.source.landing_subpath)
            if directory_entry is None:
                volume.create_directory(plan.remote_directory)
                LOGGER.info("created directory source=%s path=%s", source_id, plan.remote_directory)
                created += 1
                remote_files: dict[str, RemoteEntry] = {}
            elif not directory_entry.is_directory:
                raise BootstrapError(
                    f"Landing path is not a directory: {plan.remote_directory}"
                )
            else:
                LOGGER.info("directory exists source=%s path=%s", source_id, plan.remote_directory)
                remote_files = {
                    entry.name: entry
                    for entry in volume.list_directory(plan.remote_directory)
                    if not entry.is_directory
                }

            existing = remote_files.get(plan.source.source_file)
            if existing is None:
                volume.upload_file(plan.local_file, plan.remote_file, overwrite=False)
                LOGGER.info("uploaded source=%s file=%s", source_id, plan.remote_file)
                uploaded += 1
                continue

            local_content = plan.local_file.read_bytes()
            remote_content = volume.read_file(plan.remote_file)
            if local_content == remote_content:
                LOGGER.info("skipped unchanged source=%s file=%s", source_id, plan.remote_file)
                skipped += 1
            elif overwrite_changed:
                volume.upload_file(plan.local_file, plan.remote_file, overwrite=True)
                LOGGER.info("uploaded changed source=%s file=%s", source_id, plan.remote_file)
                uploaded += 1
            else:
                raise BootstrapError(
                    "Remote file differs; rerun with --overwrite-changed only after review: "
                    f"{plan.remote_file}"
                )
        except (BootstrapError, OSError) as exc:
            message = f"source={source_id}: {exc}"
            LOGGER.error("bootstrap failure %s", message)
            failures.append(message)

    return BootstrapSummary(created, uploaded, skipped, tuple(failures))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create registered Volume landing directories and upload synthetic CSVs."
    )
    parser.add_argument("--config", default="src/crestline_bronze/sources.yml")
    parser.add_argument(
        "--source-directory", default="crestline_synthetic_v1/source_csv"
    )
    parser.add_argument("--volume-root", default=DEFAULT_VOLUME_ROOT)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument(
        "--overwrite-changed",
        action="store_true",
        help="Replace a same-named remote file only when its bytes differ.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        registry = load_registry(args.config)
        summary = bootstrap_sources(
            registry=registry,
            local_source_directory=Path(args.source_directory),
            volume_root=args.volume_root,
            volume=DatabricksCliVolume(profile=args.profile),
            overwrite_changed=args.overwrite_changed,
        )
    except BootstrapError:
        LOGGER.exception("Bootstrap aborted")
        return 1
    LOGGER.info(
        "bootstrap complete created=%d uploaded=%d skipped=%d failures=%d",
        summary.created_directories,
        summary.uploaded_files,
        summary.skipped_files,
        len(summary.failures),
    )
    return 1 if summary.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
