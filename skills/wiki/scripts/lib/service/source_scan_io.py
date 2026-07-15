"""I/O layer for source_scan — manifest loading and git ls-files execution.

All subprocess access goes through the injectable :class:`Runner` protocol
(same as repo_clone.py). All path operations use ``resolve_safe_path``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

from lib.domain.types import Err, Ok


class ScanIOError(str, Enum):
    MANIFEST_NOT_FOUND = "manifest_not_found"
    MANIFEST_PARSE_ERROR = "manifest_parse_error"
    CLONE_PATH_NOT_FOUND = "clone_path_not_found"
    LS_FILES_FAILED = "ls_files_failed"


@dataclass(frozen=True)
class FileEntry:
    path: str
    size_bytes: int


@runtime_checkable
class Runner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float,
    ) -> "RunResult": ...


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class SubprocessRunner:
    import os
    import subprocess

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float,
    ) -> RunResult:
        import os
        import subprocess

        merged = {**os.environ, **(env or {})}
        try:
            proc = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                env=merged,
                cwd=cwd,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                returncode=-1,
                stderr=f"timed out after {timeout}s: {args[0]}",
                timed_out=True,
            )
        return RunResult(
            returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
        )


DEFAULT_TIMEOUT = 120.0


def load_manifest(
    wiki_root: Path, slug: str,
) -> Ok[dict] | Err[ScanIOError]:
    manifest_path = wiki_root / ".cache" / "manifests" / f"{slug}.json"
    if not manifest_path.exists():
        return Err(
            error=ScanIOError.MANIFEST_NOT_FOUND,
            detail=str(manifest_path),
        )
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return Err(
            error=ScanIOError.MANIFEST_PARSE_ERROR,
            detail=str(exc),
        )
    return Ok(value=data)


def resolve_clone_path(manifest: dict) -> Ok[Path] | Err[ScanIOError]:
    clone_path_str = manifest.get("clone_path", "")
    if not clone_path_str:
        return Err(
            error=ScanIOError.CLONE_PATH_NOT_FOUND,
            detail="manifest has no clone_path",
        )
    clone_path = Path(clone_path_str)
    if not clone_path.exists():
        return Err(
            error=ScanIOError.CLONE_PATH_NOT_FOUND,
            detail=str(clone_path),
        )
    return Ok(value=clone_path)


def list_files_with_sizes(
    clone_path: Path,
    runner: Runner,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Ok[tuple[FileEntry, ...]] | Err[ScanIOError]:
    """Run ``git ls-files`` and stat each file for its size."""
    result = runner.run(
        ["git", "ls-files", "-z"],
        cwd=str(clone_path),
        timeout=timeout,
    )
    if result.returncode != 0:
        return Err(
            error=ScanIOError.LS_FILES_FAILED,
            detail=result.stderr[:200],
        )
    if result.timed_out:
        return Err(
            error=ScanIOError.LS_FILES_FAILED,
            detail="timed out",
        )

    entries: list[FileEntry] = []
    for path_str in result.stdout.split("\0"):
        if not path_str:
            continue
        full_path = clone_path / path_str
        try:
            size = full_path.stat().st_size
        except OSError:
            size = 0
        entries.append(FileEntry(path=path_str, size_bytes=size))

    return Ok(value=tuple(entries))


def get_doc_paths_from_manifest(manifest: dict) -> frozenset[str]:
    """Extract paths already classified as docs by repo_ingest."""
    docs = manifest.get("docs", [])
    return frozenset(d.get("path", "") for d in docs if isinstance(d, dict))
