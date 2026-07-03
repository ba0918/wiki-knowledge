"""Tests for repo_ingest.py (thin CLI handler).

The CLI only composes: parse (pure) → resolve_and_snapshot (service) →
manifest JSON + repo-inventory.md file output + stdout summary. End-to-end
tests run against real throwaway git repositories under ``tmp_path``
(no network).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import repo_ingest


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _make_real_repo(path: Path, *, docs: int = 2) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    (path / "README.md").write_text("# proj\n", encoding="utf-8")
    (path / "docs").mkdir()
    for i in range(docs):
        (path / "docs" / f"guide-{i}.md").write_text(f"g{i}\n", encoding="utf-8")
    (path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_cli_local_repo_writes_manifest_and_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_real_repo(tmp_path / "proj")
    wiki_root = tmp_path / "wiki"

    code = repo_ingest.main([str(repo), "--wiki-root", str(wiki_root)])

    assert code == 0
    manifest_path = wiki_root / ".cache" / "manifests" / "proj.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["slug"] == "proj"
    assert len(manifest["revision"]) == 40
    assert manifest["docs_total"] == 3  # README + 2 guides
    inventory = wiki_root / "raw" / "files" / "proj" / "repo-inventory.md"
    assert inventory.is_file()

    out = capsys.readouterr().out
    assert "proj" in out
    assert str(manifest_path) in out
    # stdout is a summary (short hash at most), not the manifest body
    assert manifest["revision"] not in out


def test_cli_manifest_json_is_deterministic(tmp_path: Path) -> None:
    repo = _make_real_repo(tmp_path / "proj")
    wiki_root = tmp_path / "wiki"
    manifest_path = wiki_root / ".cache" / "manifests" / "proj.json"

    assert repo_ingest.main([str(repo), "--wiki-root", str(wiki_root)]) == 0
    first = manifest_path.read_bytes()
    assert repo_ingest.main([str(repo), "--wiki-root", str(wiki_root)]) == 0
    assert manifest_path.read_bytes() == first


def test_cli_max_docs_truncates(tmp_path: Path) -> None:
    repo = _make_real_repo(tmp_path / "proj", docs=4)
    wiki_root = tmp_path / "wiki"

    code = repo_ingest.main(
        [str(repo), "--wiki-root", str(wiki_root), "--max-docs", "2"]
    )

    assert code == 0
    manifest = json.loads(
        (wiki_root / ".cache" / "manifests" / "proj.json").read_text(encoding="utf-8")
    )
    assert len(manifest["docs"]) == 2
    assert manifest["docs_truncated"] is True
    assert manifest["docs_total"] == 5


def test_cli_output_dir_override(tmp_path: Path) -> None:
    repo = _make_real_repo(tmp_path / "proj")
    wiki_root = tmp_path / "wiki"
    out_dir = tmp_path / "custom-manifests"

    code = repo_ingest.main(
        [str(repo), "--wiki-root", str(wiki_root), "--output", str(out_dir)]
    )

    assert code == 0
    assert (out_dir / "proj.json").is_file()


# ---------------------------------------------------------------------------
# Multi-repo: one failure must not stop the batch
# ---------------------------------------------------------------------------


def test_cli_continues_after_per_repo_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _make_real_repo(tmp_path / "goodproj")
    bad = tmp_path / "does-not-exist"
    wiki_root = tmp_path / "wiki"

    code = repo_ingest.main([str(bad), str(good), "--wiki-root", str(wiki_root)])

    assert code == 1
    # The good repo was still processed.
    assert (wiki_root / ".cache" / "manifests" / "goodproj.json").is_file()
    out = capsys.readouterr().out
    assert "[ok]" in out
    assert "[failed]" in out


def test_cli_parse_rejection_is_reported_per_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wiki_root = tmp_path / "wiki"

    code = repo_ingest.main(["ext::sh -c whoami", "--wiki-root", str(wiki_root)])

    assert code == 1
    out = capsys.readouterr().out
    assert "[failed]" in out
    assert "unsupported_scheme" in out


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def test_cli_exits_2_without_sources() -> None:
    with pytest.raises(SystemExit) as excinfo:
        repo_ingest.main([])
    assert excinfo.value.code == 2


def test_cli_exits_2_without_wiki_root(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        repo_ingest.main([str(tmp_path)])
    assert excinfo.value.code == 2


def test_cli_keyboard_interrupt_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _interrupt(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(repo_ingest, "resolve_and_snapshot", _interrupt)
    code = repo_ingest.main(["/some/repo", "--wiki-root", str(tmp_path / "wiki")])
    assert code == 130
