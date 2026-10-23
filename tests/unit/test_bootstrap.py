from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import subprocess

import pytest

from crestline_bronze.bootstrap import (
    BootstrapError,
    DatabricksCliVolume,
    RemoteEntry,
    bootstrap_sources,
    build_upload_plan,
)
from crestline_bronze.config import load_registry


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = load_registry(ROOT / "src" / "crestline_bronze" / "sources.yml")
SOURCE_DIR = ROOT / "crestline_synthetic_v1" / "source_csv"
VOLUME_ROOT = "dbfs:/Volumes/workspace/crestline_bronze/mediassist_ingestion"


class FakeVolume:
    def __init__(self) -> None:
        self.directories = {f"{VOLUME_ROOT}/landing"}
        self.files: dict[str, bytes] = {}
        self.created: list[str] = []
        self.uploaded: list[tuple[str, str, bool]] = []
        self.calls = 0

    def list_directory(self, path: str) -> tuple[RemoteEntry, ...]:
        self.calls += 1
        prefix = path.rstrip("/") + "/"
        entries: list[RemoteEntry] = []
        for directory in sorted(self.directories):
            if directory.startswith(prefix) and "/" not in directory[len(prefix) :]:
                entries.append(RemoteEntry(directory, True))
        for file_path, content in sorted(self.files.items()):
            if file_path.startswith(prefix) and "/" not in file_path[len(prefix) :]:
                entries.append(RemoteEntry(file_path, False, len(content)))
        return tuple(entries)

    def create_directory(self, path: str) -> None:
        self.calls += 1
        self.directories.add(path)
        self.created.append(path)

    def read_file(self, path: str) -> bytes:
        self.calls += 1
        return self.files[path]

    def upload_file(self, local_path: Path, remote_path: str, overwrite: bool) -> None:
        self.calls += 1
        if remote_path in self.files and not overwrite:
            raise BootstrapError(f"already exists: {remote_path}")
        self.files[remote_path] = local_path.read_bytes()
        self.uploaded.append((str(local_path), remote_path, overwrite))


def test_plan_resolves_all_nine_registered_files() -> None:
    plans = build_upload_plan(REGISTRY, SOURCE_DIR, VOLUME_ROOT)
    assert len(plans) == 9
    assert all(plan.local_file.is_file() for plan in plans)
    assert all(plan.remote_file.startswith(f"{VOLUME_ROOT}/landing/") for plan in plans)


def test_missing_local_file_aborts_before_remote_operations(tmp_path: Path) -> None:
    volume = FakeVolume()
    with pytest.raises(BootstrapError, match="Local source preflight failed"):
        bootstrap_sources(REGISTRY, tmp_path, VOLUME_ROOT, volume)
    assert volume.calls == 0


def test_first_run_uploads_and_second_run_skips_unchanged_files() -> None:
    volume = FakeVolume()
    first = bootstrap_sources(REGISTRY, SOURCE_DIR, VOLUME_ROOT, volume)
    assert first.created_directories == 9
    assert first.uploaded_files == 9
    assert first.skipped_files == 0
    assert first.failures == ()

    second = bootstrap_sources(REGISTRY, SOURCE_DIR, VOLUME_ROOT, volume)
    assert second.created_directories == 0
    assert second.uploaded_files == 0
    assert second.skipped_files == 9
    assert second.failures == ()
    assert len(volume.uploaded) == 9


def test_changed_remote_file_fails_without_overwrite() -> None:
    volume = FakeVolume()
    bootstrap_sources(REGISTRY, SOURCE_DIR, VOLUME_ROOT, volume)
    source = REGISTRY.sources[0]
    remote = f"{VOLUME_ROOT}/landing/{source.landing_subpath}/{source.source_file}"
    volume.files[remote] = b"changed"

    result = bootstrap_sources(REGISTRY, SOURCE_DIR, VOLUME_ROOT, volume)
    assert result.uploaded_files == 0
    assert result.skipped_files == 8
    assert len(result.failures) == 1
    assert "--overwrite-changed" in result.failures[0]


def test_changed_remote_file_can_be_explicitly_overwritten() -> None:
    volume = FakeVolume()
    bootstrap_sources(REGISTRY, SOURCE_DIR, VOLUME_ROOT, volume)
    source = REGISTRY.sources[0]
    remote = f"{VOLUME_ROOT}/landing/{source.landing_subpath}/{source.source_file}"
    volume.files[remote] = b"changed"

    result = bootstrap_sources(
        REGISTRY, SOURCE_DIR, VOLUME_ROOT, volume, overwrite_changed=True
    )
    assert result.uploaded_files == 1
    assert result.skipped_files == 8
    assert result.failures == ()
    assert volume.files[remote] == (SOURCE_DIR / source.source_file).read_bytes()
    assert volume.uploaded[-1][2] is True


def test_remote_paths_use_registered_source_directories() -> None:
    plans = build_upload_plan(REGISTRY, SOURCE_DIR, VOLUME_ROOT)
    for plan in plans:
        assert PurePosixPath(plan.remote_directory).name == plan.source.landing_subpath
        assert PurePosixPath(plan.remote_file).name == plan.source.source_file


def test_cli_adapter_uses_profile_and_only_overwrites_when_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        stdout = b""
        if command[1:3] == ["fs", "ls"]:
            stdout = json.dumps(
                [
                    {
                        "name": f"{VOLUME_ROOT}/landing/orders/orders.csv",
                        "is_directory": False,
                        "file_size": 12,
                    }
                ]
            ).encode()
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr("crestline_bronze.bootstrap.subprocess.run", fake_run)
    client = DatabricksCliVolume(profile="abinroy90@gmail.com")
    entries = client.list_directory(f"{VOLUME_ROOT}/landing/orders")
    local = tmp_path / "orders.csv"
    local.write_text("order_id\n", encoding="utf-8")
    client.upload_file(local, f"{VOLUME_ROOT}/landing/orders/orders.csv", False)
    client.upload_file(local, f"{VOLUME_ROOT}/landing/orders/orders.csv", True)

    assert entries[0].name == "orders.csv"
    assert all(command[-2:] == ["--profile", "abinroy90@gmail.com"] for command in commands)
    copy_commands = [command for command in commands if command[1:3] == ["fs", "cp"]]
    assert "--overwrite" not in copy_commands[0]
    assert "--overwrite" in copy_commands[1]


@pytest.mark.parametrize(
    ("listing_name", "expected_name"),
    [
        (
            "dbfs:/Volumes/workspace/crestline_bronze/mediassist_ingestion/"
            "landing/document_manifest",
            "document_manifest",
        ),
        ("document_manifest", "document_manifest"),
    ],
)
def test_cli_listing_name_supports_full_dbfs_path_and_basename(
    monkeypatch: pytest.MonkeyPatch, listing_name: str, expected_name: str
) -> None:
    def fake_run(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[bytes]:
        stdout = json.dumps(
            [{"name": listing_name, "is_directory": True, "file_size": 0}]
        ).encode()
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr("crestline_bronze.bootstrap.subprocess.run", fake_run)
    client = DatabricksCliVolume(profile="abinroy90@gmail.com")

    entries = client.list_directory(f"{VOLUME_ROOT}/landing")

    assert entries == (RemoteEntry(listing_name, True, 0),)
    assert entries[0].name == expected_name
