"""Unit + smoke tests for lib/service/repo_clone.py.

Unit tests inject a :class:`FakeRunner` (records every subprocess request,
returns scripted results, optionally simulates side effects such as "the
clone directory now exists"). Smoke tests at the bottom use the real
:class:`SubprocessRunner` against throwaway git repositories under
``tmp_path`` — no network access is required.

Security mapping (repo-ingest MVP, 2026-07-03):

* C-2  GIT_ALLOW_PROTOCOL=https:ssh:git on both ghq and git paths,
       plus ``-c protocol.ext.allow=never`` on the fallback clone
* H-1  clone destinations are contained via resolve_safe_path
       (ghq root base / cache root base)
* H-2  doc paths are re-validated against the clone root (symlink escape)
* H-4  explicit local paths are only existence+.git checked (never cloned)
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pytest

from lib.domain.repo import RepoSource, parse_repo_source
from lib.domain.types import Err, Ok
from lib.service.repo_clone import (
    DEFAULT_TIMEOUT,
    GIT_ALLOW_PROTOCOL,
    RepoIngestError,
    RunResult,
    SubprocessRunner,
    render_repo_inventory,
    resolve_and_snapshot,
    write_repo_inventory,
)


# ---------------------------------------------------------------------------
# FakeRunner — test double (kept in the test module by design)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RecordedCall:
    args: tuple[str, ...]
    env: dict[str, str] | None
    cwd: str | None
    timeout: float


class FakeRunner:
    """Scripted Runner. Rules are matched by argv prefix, first match wins.

    ``effect`` callables simulate the *side effects* the real command would
    have (e.g. ``ghq get`` creating the clone directory) so that the code
    under test can keep its real post-conditions instead of having them
    mocked away.
    """

    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []
        self._rules: list[
            tuple[tuple[str, ...], RunResult, Callable[[], None] | None]
        ] = []

    def on(
        self,
        *prefix: str,
        result: RunResult,
        effect: Callable[[], None] | None = None,
    ) -> "FakeRunner":
        self._rules.append((tuple(prefix), result, effect))
        return self

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float,
    ) -> RunResult:
        call = RecordedCall(
            args=tuple(args),
            env=dict(env) if env is not None else None,
            cwd=cwd,
            timeout=timeout,
        )
        self.calls.append(call)
        for prefix, result, effect in self._rules:
            if call.args[: len(prefix)] == prefix:
                if effect is not None:
                    effect()
                return result
        return RunResult(returncode=0, stdout="")

    def commands(self) -> list[tuple[str, ...]]:
        return [c.args for c in self.calls]


def _mkrepo(path: Path) -> Path:
    """Create a directory that structurally looks like a git work tree."""

    (path / ".git").mkdir(parents=True)
    return path


def _snapshot_rules(
    runner: FakeRunner,
    *,
    revision: str = "abc123def456",
    docs: str = "README.md\ndocs/guide.md\n",
    all_files: str = "README.md\ndocs/guide.md\nsrc/main.rs\nCargo.toml\n",
) -> FakeRunner:
    runner.on(
        "git", "rev-parse", "HEAD", result=RunResult(returncode=0, stdout=revision + "\n")
    )
    # Docs listing (with pathspec) must be registered before the generic
    # ls-files rule — first match wins.
    runner.on(
        "git", "ls-files", "--", result=RunResult(returncode=0, stdout=docs)
    )
    runner.on("git", "ls-files", result=RunResult(returncode=0, stdout=all_files))
    return runner


def _https_source(url: str = "https://github.com/Owner/Repo") -> RepoSource:
    result = parse_repo_source(url)
    assert isinstance(result, Ok)
    return result.value


NO_GHQ: Callable[[str], str | None] = lambda name: None  # noqa: E731


# ---------------------------------------------------------------------------
# Local path input (H-4: verify only, never clone)
# ---------------------------------------------------------------------------


def test_local_path_snapshot_without_cloning(tmp_path: Path) -> None:
    repo = _mkrepo(tmp_path / "My.Repo")
    runner = _snapshot_rules(FakeRunner())
    source = RepoSource(kind="local", url=str(repo))

    result = resolve_and_snapshot(
        source, wiki_root=tmp_path / "wiki", runner=runner, which=NO_GHQ
    )

    assert isinstance(result, Ok)
    manifest = result.value
    assert manifest.slug == "my-repo"
    assert manifest.revision == "abc123def456"
    assert manifest.clone_path == str(repo.resolve())
    # No clone/fetch command was ever issued.
    assert all(c[:2] not in {("git", "clone"), ("ghq", "get")} for c in runner.commands())
    assert not any("clone" in c for c in runner.commands())
    # Snapshot commands ran inside the repo.
    assert runner.calls[0].cwd == str(repo.resolve())


def test_local_path_missing_returns_err(tmp_path: Path) -> None:
    source = RepoSource(kind="local", url=str(tmp_path / "nope"))
    result = resolve_and_snapshot(
        source, wiki_root=tmp_path / "wiki", runner=FakeRunner(), which=NO_GHQ
    )
    assert isinstance(result, Err)
    assert result.error == RepoIngestError.LOCAL_NOT_FOUND


def test_local_path_without_dot_git_returns_err(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    source = RepoSource(kind="local", url=str(plain))
    result = resolve_and_snapshot(
        source, wiki_root=tmp_path / "wiki", runner=FakeRunner(), which=NO_GHQ
    )
    assert isinstance(result, Err)
    assert result.error == RepoIngestError.NOT_A_GIT_REPO


def test_local_path_with_unsluggable_name_returns_err(tmp_path: Path) -> None:
    repo = _mkrepo(tmp_path / "日本語")
    source = RepoSource(kind="local", url=str(repo))
    result = resolve_and_snapshot(
        source, wiki_root=tmp_path / "wiki", runner=FakeRunner(), which=NO_GHQ
    )
    assert isinstance(result, Err)
    assert result.error == RepoIngestError.INVALID_SLUG


# ---------------------------------------------------------------------------
# ghq path (C-2 / H-1)
# ---------------------------------------------------------------------------


def _ghq_setup(tmp_path: Path) -> tuple[FakeRunner, Path, Callable[[str], str | None]]:
    ghq_root = tmp_path / "ghq"
    ghq_root.mkdir()
    runner = FakeRunner()
    runner.on("ghq", "root", result=RunResult(returncode=0, stdout=str(ghq_root) + "\n"))
    which = lambda name: "/usr/bin/ghq" if name == "ghq" else None  # noqa: E731
    return runner, ghq_root, which


def test_ghq_shallow_clone_with_protocol_env(tmp_path: Path) -> None:
    runner, ghq_root, which = _ghq_setup(tmp_path)
    dest = ghq_root / "github.com" / "Owner" / "Repo"
    runner.on(
        "ghq",
        "get",
        result=RunResult(returncode=0),
        effect=lambda: _mkrepo(dest),
    )
    _snapshot_rules(runner)

    result = resolve_and_snapshot(
        _https_source(), wiki_root=tmp_path / "wiki", runner=runner, which=which
    )

    assert isinstance(result, Ok)
    assert result.value.clone_path == str(dest)
    get_calls = [c for c in runner.calls if c.args[:2] == ("ghq", "get")]
    assert len(get_calls) == 1
    call = get_calls[0]
    assert call.args == (
        "ghq", "get", "--shallow", "--", "https://github.com/Owner/Repo",
    )
    # C-2: the protocol allowlist must reach the subprocess environment.
    assert call.env is not None
    assert call.env["GIT_ALLOW_PROTOCOL"] == "https:ssh:git"


def test_ghq_full_clone_drops_shallow_flag(tmp_path: Path) -> None:
    runner, ghq_root, which = _ghq_setup(tmp_path)
    dest = ghq_root / "github.com" / "Owner" / "Repo"
    runner.on(
        "ghq", "get", result=RunResult(returncode=0), effect=lambda: _mkrepo(dest)
    )
    _snapshot_rules(runner)

    result = resolve_and_snapshot(
        _https_source(),
        wiki_root=tmp_path / "wiki",
        runner=runner,
        which=which,
        full_clone=True,
    )

    assert isinstance(result, Ok)
    (call,) = [c for c in runner.calls if c.args[:2] == ("ghq", "get")]
    assert "--shallow" not in call.args


def test_ghq_existing_clone_is_reused_without_fetch(tmp_path: Path) -> None:
    runner, ghq_root, which = _ghq_setup(tmp_path)
    dest = _mkrepo(ghq_root / "github.com" / "Owner" / "Repo")
    _snapshot_rules(runner)

    result = resolve_and_snapshot(
        _https_source(), wiki_root=tmp_path / "wiki", runner=runner, which=which
    )

    assert isinstance(result, Ok)
    assert result.value.clone_path == str(dest)
    # Snapshot semantics: no ghq get, no fetch — HEAD is recorded as-is.
    assert all(c[:2] != ("ghq", "get") for c in runner.commands())
    assert result.value.revision == "abc123def456"


def test_ghq_refresh_updates_existing_clone(tmp_path: Path) -> None:
    runner, ghq_root, which = _ghq_setup(tmp_path)
    _mkrepo(ghq_root / "github.com" / "Owner" / "Repo")
    runner.on("ghq", "get", result=RunResult(returncode=0))
    _snapshot_rules(runner)

    result = resolve_and_snapshot(
        _https_source(),
        wiki_root=tmp_path / "wiki",
        runner=runner,
        which=which,
        refresh=True,
    )

    assert isinstance(result, Ok)
    (call,) = [c for c in runner.calls if c.args[:2] == ("ghq", "get")]
    assert "--update" in call.args


def test_ghq_clone_timeout_returns_err(tmp_path: Path) -> None:
    runner, _, which = _ghq_setup(tmp_path)
    runner.on("ghq", "get", result=RunResult(returncode=-1, timed_out=True))

    result = resolve_and_snapshot(
        _https_source(), wiki_root=tmp_path / "wiki", runner=runner, which=which
    )

    assert isinstance(result, Err)
    assert result.error == RepoIngestError.TIMEOUT


def test_default_timeout_is_600s_and_propagates_to_runner(tmp_path: Path) -> None:
    assert DEFAULT_TIMEOUT == 600.0
    runner, ghq_root, which = _ghq_setup(tmp_path)
    dest = ghq_root / "github.com" / "Owner" / "Repo"
    runner.on(
        "ghq", "get", result=RunResult(returncode=0), effect=lambda: _mkrepo(dest)
    )
    _snapshot_rules(runner)

    resolve_and_snapshot(
        _https_source(), wiki_root=tmp_path / "wiki", runner=runner, which=which
    )

    assert all(c.timeout == 600.0 for c in runner.calls)


def test_custom_timeout_propagates(tmp_path: Path) -> None:
    runner, ghq_root, which = _ghq_setup(tmp_path)
    dest = ghq_root / "github.com" / "Owner" / "Repo"
    runner.on(
        "ghq", "get", result=RunResult(returncode=0), effect=lambda: _mkrepo(dest)
    )
    _snapshot_rules(runner)

    resolve_and_snapshot(
        _https_source(),
        wiki_root=tmp_path / "wiki",
        runner=runner,
        which=which,
        timeout=30.0,
    )

    assert all(c.timeout == 30.0 for c in runner.calls)


# ---------------------------------------------------------------------------
# git fallback path (C-2 / H-1)
# ---------------------------------------------------------------------------


def test_git_fallback_shallow_clone_into_cache(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    dest = wiki_root / ".cache" / "repos" / "github.com" / "Owner" / "Repo"
    runner = FakeRunner()
    runner.on(
        "git",
        "-c",
        "protocol.ext.allow=never",
        "clone",
        result=RunResult(returncode=0),
        effect=lambda: _mkrepo(dest),
    )
    _snapshot_rules(runner)

    result = resolve_and_snapshot(
        _https_source(), wiki_root=wiki_root, runner=runner, which=NO_GHQ
    )

    assert isinstance(result, Ok)
    assert result.value.clone_path == str(dest)
    (clone,) = [c for c in runner.calls if "clone" in c.args]
    assert clone.args == (
        "git", "-c", "protocol.ext.allow=never", "clone",
        "--depth", "1", "--single-branch",
        "--", "https://github.com/Owner/Repo", str(dest),
    )
    assert clone.env is not None
    assert clone.env["GIT_ALLOW_PROTOCOL"] == "https:ssh:git"


def test_git_fallback_full_clone_drops_depth_flags(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    dest = wiki_root / ".cache" / "repos" / "github.com" / "Owner" / "Repo"
    runner = FakeRunner()
    runner.on(
        "git", "-c", "protocol.ext.allow=never", "clone",
        result=RunResult(returncode=0),
        effect=lambda: _mkrepo(dest),
    )
    _snapshot_rules(runner)

    result = resolve_and_snapshot(
        _https_source(),
        wiki_root=wiki_root,
        runner=runner,
        which=NO_GHQ,
        full_clone=True,
    )

    assert isinstance(result, Ok)
    (clone,) = [c for c in runner.calls if "clone" in c.args]
    assert "--depth" not in clone.args
    assert "--single-branch" not in clone.args


def test_git_fallback_existing_cache_reused_without_fetch(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    dest = _mkrepo(wiki_root / ".cache" / "repos" / "github.com" / "Owner" / "Repo")
    runner = _snapshot_rules(FakeRunner())

    result = resolve_and_snapshot(
        _https_source(), wiki_root=wiki_root, runner=runner, which=NO_GHQ
    )

    assert isinstance(result, Ok)
    assert result.value.clone_path == str(dest)
    assert all("clone" not in c and "fetch" not in c for c in runner.commands())


def test_git_fallback_refresh_fetches_and_resets(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    dest = _mkrepo(wiki_root / ".cache" / "repos" / "github.com" / "Owner" / "Repo")
    runner = FakeRunner()
    runner.on("git", "fetch", result=RunResult(returncode=0))
    runner.on("git", "reset", result=RunResult(returncode=0))
    _snapshot_rules(runner)

    result = resolve_and_snapshot(
        _https_source(),
        wiki_root=wiki_root,
        runner=runner,
        which=NO_GHQ,
        refresh=True,
    )

    assert isinstance(result, Ok)
    fetch_calls = [c for c in runner.calls if c.args[:2] == ("git", "fetch")]
    reset_calls = [c for c in runner.calls if c.args[:2] == ("git", "reset")]
    assert len(fetch_calls) == 1 and fetch_calls[0].cwd == str(dest)
    assert len(reset_calls) == 1 and reset_calls[0].cwd == str(dest)


def test_git_fallback_clone_failure_returns_err(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.on(
        "git", "-c", "protocol.ext.allow=never", "clone",
        result=RunResult(returncode=128, stderr="fatal: repository not found"),
    )

    result = resolve_and_snapshot(
        _https_source(), wiki_root=tmp_path / "wiki", runner=runner, which=NO_GHQ
    )

    assert isinstance(result, Err)
    assert result.error == RepoIngestError.CLONE_FAILED
    assert "not found" in result.detail


def test_rev_parse_failure_returns_err(tmp_path: Path) -> None:
    repo = _mkrepo(tmp_path / "proj")
    runner = FakeRunner()
    runner.on(
        "git", "rev-parse", "HEAD",
        result=RunResult(returncode=128, stderr="fatal: not a git repository"),
    )

    source = RepoSource(kind="local", url=str(repo))
    result = resolve_and_snapshot(
        source, wiki_root=tmp_path / "wiki", runner=runner, which=NO_GHQ
    )

    assert isinstance(result, Err)
    assert result.error == RepoIngestError.GIT_COMMAND_FAILED


# ---------------------------------------------------------------------------
# H-1 containment (defense in depth against a forged RepoSource)
# ---------------------------------------------------------------------------


def test_traversal_owner_is_rejected_by_containment(tmp_path: Path) -> None:
    # parse_repo_source already rejects ".." segments; this pins the second
    # line of defense for a RepoSource constructed by other means.
    forged = RepoSource(
        kind="https",
        url="https://example.com/../../escape",
        host="example.com",
        owner="..",
        name="escape",
    )
    result = resolve_and_snapshot(
        forged, wiki_root=tmp_path / "wiki", runner=FakeRunner(), which=NO_GHQ
    )
    assert isinstance(result, Err)
    assert result.error == RepoIngestError.UNSAFE_PATH


# ---------------------------------------------------------------------------
# H-2: doc paths re-validated against the clone root
# ---------------------------------------------------------------------------


def test_symlink_escaping_doc_is_dropped_from_manifest(tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("outside", encoding="utf-8")
    repo = _mkrepo(tmp_path / "proj")
    (repo / "README.md").write_text("hi", encoding="utf-8")
    try:
        (repo / "evil.md").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")

    runner = _snapshot_rules(
        FakeRunner(),
        docs="README.md\nevil.md\n",
        all_files="README.md\nevil.md\n",
    )
    source = RepoSource(kind="local", url=str(repo))

    result = resolve_and_snapshot(
        source, wiki_root=tmp_path / "wiki", runner=runner, which=NO_GHQ
    )

    assert isinstance(result, Ok)
    paths = [d.path for d in result.value.docs]
    assert "README.md" in paths
    assert "evil.md" not in paths


# ---------------------------------------------------------------------------
# C-2 constant pin
# ---------------------------------------------------------------------------


def test_git_allow_protocol_excludes_file_and_ext() -> None:
    assert GIT_ALLOW_PROTOCOL == "https:ssh:git"


# ---------------------------------------------------------------------------
# repo-inventory.md (machine-generated primary source)
# ---------------------------------------------------------------------------


def _sample_manifest(tmp_path: Path):
    repo = _mkrepo(tmp_path / "proj")
    runner = _snapshot_rules(FakeRunner())
    source = RepoSource(kind="local", url=str(repo))
    result = resolve_and_snapshot(
        source, wiki_root=tmp_path / "wiki", runner=runner, which=NO_GHQ
    )
    assert isinstance(result, Ok)
    return result.value


def test_render_repo_inventory_is_deterministic(tmp_path: Path) -> None:
    manifest = _sample_manifest(tmp_path)
    first = render_repo_inventory(manifest)
    second = render_repo_inventory(manifest)
    assert first == second
    assert manifest.revision in first
    assert manifest.slug in first
    assert "source_revision:" in first  # frontmatter field


def test_write_repo_inventory_writes_under_raw_files_slug(tmp_path: Path) -> None:
    manifest = _sample_manifest(tmp_path)
    wiki_root = tmp_path / "wiki"

    result = write_repo_inventory(manifest, wiki_root=wiki_root)

    assert isinstance(result, Ok)
    expected = wiki_root / "raw" / "files" / manifest.slug / "repo-inventory.md"
    assert result.value == expected
    assert expected.is_file()
    assert manifest.revision in expected.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# SubprocessRunner — real subprocess behavior
# ---------------------------------------------------------------------------


def test_subprocess_runner_captures_stdout() -> None:
    result = SubprocessRunner().run(["echo", "hello"], timeout=10.0)
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.timed_out is False


def test_subprocess_runner_merges_env_over_os_environ() -> None:
    result = SubprocessRunner().run(
        ["sh", "-c", 'test -n "$PATH" && printf %s "$REPO_CLONE_TEST"'],
        env={"REPO_CLONE_TEST": "injected"},
        timeout=10.0,
    )
    assert result.returncode == 0
    assert result.stdout == "injected"


def test_subprocess_runner_reports_timeout() -> None:
    result = SubprocessRunner().run(["sleep", "5"], timeout=0.2)
    assert result.timed_out is True
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Smoke integration (plan step 10): real git, no network
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _make_real_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    (path / "README.md").write_text("# proj\n", encoding="utf-8")
    (path / "docs").mkdir()
    (path / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    (path / "openapi.yaml").write_text("openapi: 3.0.0\n", encoding="utf-8")
    (path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (path / "src").mkdir()
    (path / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


def test_smoke_local_repo_end_to_end(tmp_path: Path) -> None:
    repo = _make_real_repo(tmp_path / "proj")
    wiki_root = tmp_path / "wiki"
    parsed = parse_repo_source(str(repo))
    assert isinstance(parsed, Ok)

    result = resolve_and_snapshot(
        parsed.value, wiki_root=wiki_root, runner=SubprocessRunner(), which=NO_GHQ
    )

    assert isinstance(result, Ok)
    manifest = result.value
    assert re.fullmatch(r"[0-9a-f]{40}", manifest.revision)
    assert manifest.slug == "proj"
    tiers = {d.path: d.tier for d in manifest.docs}
    assert tiers["README.md"] == 1
    assert tiers["openapi.yaml"] == 2
    assert tiers["docs/guide.md"] == 3
    assert manifest.total_files == 5
    assert "Cargo.toml" in manifest.entrypoints
    assert "src/main.rs" in manifest.entrypoints

    written = write_repo_inventory(manifest, wiki_root=wiki_root)
    assert isinstance(written, Ok)
    content = written.value.read_text(encoding="utf-8")
    assert manifest.revision in content


def test_smoke_precloned_remote_cache_detection(tmp_path: Path) -> None:
    # A remote URL whose predicted cache path already contains a clone must
    # be snapshotted as-is (no network, no fetch). The pre-clone is done by
    # the test itself with plain git.
    origin = _make_real_repo(tmp_path / "origin")
    wiki_root = tmp_path / "wiki"
    dest = wiki_root / ".cache" / "repos" / "example.com" / "team" / "proj"
    dest.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(dest)],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=dest,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    parsed = parse_repo_source("https://example.com/team/proj")
    assert isinstance(parsed, Ok)
    result = resolve_and_snapshot(
        parsed.value, wiki_root=wiki_root, runner=SubprocessRunner(), which=NO_GHQ
    )

    assert isinstance(result, Ok)
    assert result.value.revision == head
    assert result.value.clone_path == str(dest)
    assert result.value.slug == "example-com-team-proj"
