"""Clone resolution + snapshot service for the repo ingest MVP.

All subprocess access goes through the injectable :class:`Runner` protocol
(production: :class:`SubprocessRunner`; tests: a FakeRunner defined in the
test module). All path containment goes through
:func:`lib.service.path_validator.resolve_safe_path` and all slugs are
double-checked with :func:`~lib.service.path_validator.sanitize_id`.

Flow (composed by the CLI handler, ``repo_ingest.py``)::

    parse_repo_source (pure)          lib/domain/repo.py
      -> resolve_and_snapshot (I/O)   this module
           -> discover_docs / build_manifest (pure)
      -> write_repo_inventory (I/O)   this module

Security mapping (repo-ingest MVP, 2026-07-03):

* **C-2** every clone subprocess runs with ``GIT_ALLOW_PROTOCOL=https:ssh:git``
  (ghq execs git internally, so the env var covers both routes); the git
  fallback additionally passes ``-c protocol.ext.allow=never``.
* **H-1** predicted/created clone paths are contained under exactly two
  bases — the ghq root or ``{wiki_root}/.cache/repos`` — via
  ``resolve_safe_path``. A ``RepoSource`` forged with traversal segments is
  rejected here even though ``parse_repo_source`` already bans it.
* **H-2** every doc path returned by ``git ls-files`` is re-validated
  against the clone root before entering the manifest, so symlinks that
  escape the clone are silently dropped.
* **H-4** an explicit local path input is never cloned: it is only verified
  to exist and to contain ``.git``, then snapshotted in place. Remote inputs
  can never use the ``file`` transport (not in ``GIT_ALLOW_PROTOCOL``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable

from lib.domain.repo import (
    DEFAULT_MAX_DOCS,
    RepoManifest,
    RepoSource,
    build_manifest,
    discover_docs,
    normalize_repo_slug,
)
from lib.domain.types import Err, Ok
from lib.service.path_validator import resolve_safe_path, sanitize_id


# C-2: transports git may use when cloning on our behalf. ``file`` and
# ``ext`` are deliberately absent.
GIT_ALLOW_PROTOCOL = "https:ssh:git"

DEFAULT_TIMEOUT = 600.0

# Pathspecs handed to ``git ls-files`` for the docs listing. ``*pattern``
# matches at any depth (git wildmatch lets ``*`` cross ``/`` in pathspecs).
DOCS_PATHSPECS = (
    "README*",
    "docs/**",
    "*.md",
    "*openapi*",
    "*swagger*",
)


class RepoIngestError(str, Enum):
    """Discriminator for clone/snapshot failures."""

    LOCAL_NOT_FOUND = "local_not_found"
    NOT_A_GIT_REPO = "not_a_git_repo"
    INVALID_SLUG = "invalid_slug"
    UNSAFE_PATH = "unsafe_path"
    CLONE_FAILED = "clone_failed"
    TIMEOUT = "timeout"
    GIT_COMMAND_FAILED = "git_command_failed"


# ---------------------------------------------------------------------------
# Runner protocol + production implementation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """Outcome of one subprocess execution (CompletedProcess-shaped)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@runtime_checkable
class Runner(Protocol):
    """Protocol for subprocess execution. ``env`` entries are *overlaid* on
    the inherited environment; they never replace it wholesale (git needs
    HOME, PATH, SSH_AUTH_SOCK, ...)."""

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float,
    ) -> RunResult: ...  # pragma: no cover - protocol


class SubprocessRunner:
    """Production :class:`Runner` backed by :func:`subprocess.run`.

    Timeouts are reported as ``RunResult(timed_out=True)`` instead of an
    exception so that callers stay in the Result channel.
    """

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float,
    ) -> RunResult:
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


# ---------------------------------------------------------------------------
# resolve_and_snapshot
# ---------------------------------------------------------------------------


def resolve_and_snapshot(
    source: RepoSource,
    *,
    wiki_root: Path,
    runner: Runner,
    which: Callable[[str], str | None] = shutil.which,
    max_docs: int = DEFAULT_MAX_DOCS,
    full_clone: bool = False,
    refresh: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> Ok[RepoManifest] | Err[RepoIngestError]:
    """Materialize ``source`` on disk (clone if needed) and snapshot it.

    Returns a :class:`~lib.domain.repo.RepoManifest` whose ``revision`` is
    the HEAD commit at the time of snapshot. Existing clones are reused
    without fetching unless ``refresh=True`` (snapshot semantics: the
    recorded revision is honest about what was read).
    """

    if source.kind == "local":
        located = _locate_local(source)
    else:
        located = _resolve_remote(
            source,
            wiki_root=wiki_root,
            runner=runner,
            which=which,
            full_clone=full_clone,
            refresh=refresh,
            timeout=timeout,
        )
    if isinstance(located, Err):
        return located
    slug, clone_path = located.value

    return _snapshot(
        slug=slug,
        source=source,
        clone_path=clone_path,
        runner=runner,
        max_docs=max_docs,
        timeout=timeout,
    )


def _locate_local(
    source: RepoSource,
) -> Ok[tuple[str, Path]] | Err[RepoIngestError]:
    """H-4: explicit local paths are verified (exists + .git), never cloned."""

    path = Path(os.path.expanduser(source.url))
    if not path.is_dir():
        return Err(error=RepoIngestError.LOCAL_NOT_FOUND, detail=source.url)
    path = path.resolve()
    if not (path / ".git").exists():
        return Err(error=RepoIngestError.NOT_A_GIT_REPO, detail=str(path))

    slug = normalize_repo_slug("", "", path.name)
    if isinstance(sanitize_id(slug), Err):
        return Err(
            error=RepoIngestError.INVALID_SLUG,
            detail=f"cannot derive slug from {path.name!r}",
        )
    return Ok(value=(slug, path))


def _resolve_remote(
    source: RepoSource,
    *,
    wiki_root: Path,
    runner: Runner,
    which: Callable[[str], str | None],
    full_clone: bool,
    refresh: bool,
    timeout: float,
) -> Ok[tuple[str, Path]] | Err[RepoIngestError]:
    slug = normalize_repo_slug(source.host, source.owner, source.name)
    if isinstance(sanitize_id(slug), Err):
        return Err(error=RepoIngestError.INVALID_SLUG, detail=slug)

    rel_parts = [source.host, *source.owner.split("/"), source.name]
    rel = "/".join(part for part in rel_parts if part)
    # H-1 defense in depth: parse_repo_source already bans dot segments, but
    # a RepoSource constructed by other means must not smuggle them either.
    # ("host/../x" would *resolve* back inside the base, so resolve_safe_path
    # alone cannot catch a same-base traversal — reject it structurally.)
    if any(seg in (".", "..") for seg in rel.split("/")):
        return Err(error=RepoIngestError.UNSAFE_PATH, detail=rel)

    if which("ghq") is not None:
        ghq_root = _ghq_root(runner, timeout=timeout)
        if ghq_root is not None:
            return _resolve_via_ghq(
                source,
                ghq_root=ghq_root,
                rel=rel,
                slug=slug,
                runner=runner,
                full_clone=full_clone,
                refresh=refresh,
                timeout=timeout,
            )
        # ghq exists but its root could not be determined — fall through to
        # the cache-dir git clone rather than failing the whole ingest.

    return _resolve_via_git(
        source,
        cache_root=wiki_root / ".cache" / "repos",
        rel=rel,
        slug=slug,
        runner=runner,
        full_clone=full_clone,
        refresh=refresh,
        timeout=timeout,
    )


def _ghq_root(runner: Runner, *, timeout: float) -> Path | None:
    result = runner.run(("ghq", "root"), timeout=timeout)
    if result.returncode != 0 or result.timed_out:
        return None
    root = result.stdout.strip()
    return Path(root) if root else None


def _clone_env() -> dict[str, str]:
    return {"GIT_ALLOW_PROTOCOL": GIT_ALLOW_PROTOCOL}


def _check_clone_result(
    result: RunResult, *, what: str
) -> Err[RepoIngestError] | None:
    if result.timed_out:
        return Err(error=RepoIngestError.TIMEOUT, detail=f"{what}: {result.stderr}")
    if result.returncode != 0:
        return Err(
            error=RepoIngestError.CLONE_FAILED,
            detail=f"{what}: {result.stderr.strip()}",
        )
    return None


def _resolve_via_ghq(
    source: RepoSource,
    *,
    ghq_root: Path,
    rel: str,
    slug: str,
    runner: Runner,
    full_clone: bool,
    refresh: bool,
    timeout: float,
) -> Ok[tuple[str, Path]] | Err[RepoIngestError]:
    # H-1: containment base #1 — the ghq root.
    safe = resolve_safe_path(base=ghq_root, relative=rel)
    if isinstance(safe, Err):
        return Err(error=RepoIngestError.UNSAFE_PATH, detail=f"ghq:{rel}")
    dest = safe.value

    already_cloned = (dest / ".git").exists()
    if already_cloned and not refresh:
        return Ok(value=(slug, dest))

    args = ["ghq", "get"]
    if refresh:
        args.append("--update")
    if not full_clone:
        args.append("--shallow")
    args += ["--", source.url]
    result = runner.run(args, env=_clone_env(), timeout=timeout)
    failure = _check_clone_result(result, what="ghq get")
    if failure is not None:
        return failure
    if not (dest / ".git").exists():
        return Err(
            error=RepoIngestError.CLONE_FAILED,
            detail=f"ghq get did not produce {dest}",
        )
    return Ok(value=(slug, dest))


def _resolve_via_git(
    source: RepoSource,
    *,
    cache_root: Path,
    rel: str,
    slug: str,
    runner: Runner,
    full_clone: bool,
    refresh: bool,
    timeout: float,
) -> Ok[tuple[str, Path]] | Err[RepoIngestError]:
    # H-1: containment base #2 — the wiki cache root.
    safe = resolve_safe_path(base=cache_root, relative=rel)
    if isinstance(safe, Err):
        return Err(error=RepoIngestError.UNSAFE_PATH, detail=f"cache:{rel}")
    dest = safe.value

    if (dest / ".git").exists():
        if not refresh:
            return Ok(value=(slug, dest))
        fetch_args = ["git", "fetch"]
        if not full_clone:
            fetch_args += ["--depth", "1"]
        fetched = runner.run(
            fetch_args, env=_clone_env(), cwd=str(dest), timeout=timeout
        )
        failure = _check_clone_result(fetched, what="git fetch")
        if failure is not None:
            return failure
        reset = runner.run(
            ("git", "reset", "--hard", "FETCH_HEAD"), cwd=str(dest), timeout=timeout
        )
        failure = _check_clone_result(reset, what="git reset")
        if failure is not None:
            return failure
        return Ok(value=(slug, dest))

    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "-c", "protocol.ext.allow=never", "clone"]
    if not full_clone:
        args += ["--depth", "1", "--single-branch"]
    args += ["--", source.url, str(dest)]
    result = runner.run(args, env=_clone_env(), timeout=timeout)
    failure = _check_clone_result(result, what="git clone")
    if failure is not None:
        return failure
    if not (dest / ".git").exists():
        return Err(
            error=RepoIngestError.CLONE_FAILED,
            detail=f"git clone did not produce {dest}",
        )
    return Ok(value=(slug, dest))


# ---------------------------------------------------------------------------
# Snapshot (rev-parse + ls-files → manifest)
# ---------------------------------------------------------------------------


def _git_lines(result: RunResult) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.strip()]


def _snapshot(
    *,
    slug: str,
    source: RepoSource,
    clone_path: Path,
    runner: Runner,
    max_docs: int,
    timeout: float,
) -> Ok[RepoManifest] | Err[RepoIngestError]:
    cwd = str(clone_path)

    rev = runner.run(("git", "rev-parse", "HEAD"), cwd=cwd, timeout=timeout)
    if rev.timed_out:
        return Err(error=RepoIngestError.TIMEOUT, detail="git rev-parse")
    if rev.returncode != 0:
        return Err(
            error=RepoIngestError.GIT_COMMAND_FAILED,
            detail=f"git rev-parse HEAD: {rev.stderr.strip()}",
        )
    revision = rev.stdout.strip()

    docs_res = runner.run(
        ("git", "ls-files", "--", *DOCS_PATHSPECS), cwd=cwd, timeout=timeout
    )
    if docs_res.timed_out or docs_res.returncode != 0:
        return Err(
            error=RepoIngestError.GIT_COMMAND_FAILED,
            detail=f"git ls-files (docs): {docs_res.stderr.strip()}",
        )

    all_res = runner.run(("git", "ls-files"), cwd=cwd, timeout=timeout)
    if all_res.timed_out or all_res.returncode != 0:
        return Err(
            error=RepoIngestError.GIT_COMMAND_FAILED,
            detail=f"git ls-files: {all_res.stderr.strip()}",
        )

    # H-2: only doc paths that resolve *inside* the clone survive. Symlinks
    # pointing outside (or traversal smuggled into ls-files output) drop out.
    safe_doc_paths = [
        path
        for path in _git_lines(docs_res)
        if isinstance(resolve_safe_path(base=clone_path, relative=path), Ok)
    ]

    manifest = build_manifest(
        slug=slug,
        source_url=source.url,
        clone_path=str(clone_path),
        revision=revision,
        all_files=_git_lines(all_res),
        docs=discover_docs(safe_doc_paths),
        max_docs=max_docs,
    )
    return Ok(value=manifest)


# ---------------------------------------------------------------------------
# repo-inventory.md — machine-generated primary source
# ---------------------------------------------------------------------------


def render_repo_inventory(manifest: RepoManifest) -> str:
    """Render the deterministic ``repo-inventory.md`` for a manifest.

    Pure function of the manifest: no timestamps, no LLM interpretation —
    the snapshot moment is pinned by ``source_revision`` alone, so repeated
    runs over the same revision are byte-identical.
    """

    ext_rows = sorted(
        manifest.file_count_by_extension.items(), key=lambda kv: (-kv[1], kv[0])
    )
    tier_counts = {1: 0, 2: 0, 3: 0}
    for doc in manifest.docs:
        tier_counts[doc.tier] = tier_counts.get(doc.tier, 0) + 1

    lines: list[str] = [
        "---",
        "source_type: repo",
        f"source_url: {manifest.source_url}",
        f"source_revision: {manifest.revision}",
        "generated_by: repo_ingest.py",
        "---",
        "",
        f"# Repository Inventory: {manifest.slug}",
        "",
        "機械生成の一次情報（決定論的なツール出力、LLM 解釈なし）。",
        "",
        "## リポジトリ",
        "",
        f"- source_url: {manifest.source_url}",
        f"- revision: `{manifest.revision}`",
        f"- clone_path: `{manifest.clone_path}`",
        "",
        "## トップレベルディレクトリ",
        "",
    ]
    lines += [f"- `{d}/`" for d in manifest.top_level_dirs] or ["- （なし）"]
    lines += [
        "",
        "## 拡張子別ファイル数",
        "",
        "| 拡張子 | ファイル数 |",
        "|---|---|",
    ]
    lines += [f"| {ext} | {count} |" for ext, count in ext_rows]
    lines += [
        "",
        f"総ファイル数: {manifest.total_files}",
        "",
        "## エントリポイント候補",
        "",
    ]
    lines += [f"- `{e}`" for e in manifest.entrypoints] or ["- （なし）"]
    lines += [
        "",
        "## docs 候補",
        "",
        f"- tier1: {tier_counts[1]} 件 / tier2: {tier_counts[2]} 件 / "
        f"tier3: {tier_counts[3]} 件",
        f"- docs_total: {manifest.docs_total}"
        + ("（max_docs で truncate 済み）" if manifest.docs_truncated else ""),
        "",
    ]
    tier1_docs = [d for d in manifest.docs if d.tier == 1]
    if tier1_docs:
        lines += ["### tier1", ""]
        lines += [f"- `{d.path}`" for d in tier1_docs]
        lines.append("")
    return "\n".join(lines)


def write_repo_inventory(
    manifest: RepoManifest, *, wiki_root: Path
) -> Ok[Path] | Err[RepoIngestError]:
    """Write ``raw/files/{slug}/repo-inventory.md`` and return its path."""

    if isinstance(sanitize_id(manifest.slug), Err):
        return Err(error=RepoIngestError.INVALID_SLUG, detail=manifest.slug)
    safe = resolve_safe_path(
        base=wiki_root, relative=f"raw/files/{manifest.slug}/repo-inventory.md"
    )
    if isinstance(safe, Err):
        return Err(error=RepoIngestError.UNSAFE_PATH, detail=manifest.slug)

    target = safe.value
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_repo_inventory(manifest), encoding="utf-8")
    return Ok(value=target)
