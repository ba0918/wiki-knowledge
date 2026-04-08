"""Unit tests for lib/service/wiki_repo.py.

``WikiRepo`` is the single canonical CRUD + atomic-allocation adapter for
``.wiki/concepts/*.md``. It combines:

* filesystem layout knowledge (``concepts/{article_id}.md``)
* dependency injection of :class:`FileLock` and :class:`Clock` so that
  allocation races can be simulated deterministically
* atomic write via a ``.tmp`` sibling + ``rename`` so that a crash mid-save
  leaves either the old or the new content on disk — never a half-written
  file.

These tests use ``FakeFileLock`` and ``FixedClock`` to keep them
hermetic and fast. Cross-process semantics of the real lock are verified
separately in ``test_file_lock.py``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lib.domain.types import (
    Article,
    Err,
    GeneratedBy,
    KnowledgeTime,
    Ok,
    Relations,
)
from lib.service.clock import FixedClock
from lib.service.file_lock import FakeFileLock, FileLockTimeout
from lib.service.schema import dump_article, load_article
from lib.service.wiki_repo import RepoError, WikiRepo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, *, lock: FakeFileLock | None = None) -> WikiRepo:
    wiki_root = tmp_path / ".wiki"
    (wiki_root / "concepts").mkdir(parents=True)
    return WikiRepo(
        wiki_root=wiki_root,
        file_lock=lock or FakeFileLock(),
        clock=FixedClock(now="2026-04-08T09:12:00Z"),
    )


def _make_sample_article(article_id: str = "20260408091200-test") -> Article:
    return Article(
        schema_version=1,
        article_id=article_id,
        article_type="concept",
        title="Sample",
        captured_at="2026-04-08",
        knowledge_time=KnowledgeTime(valid_from="2026-04-08", valid_to=None),
        status="current",
        sources=(),
        relations=Relations(),
        claims=(),
        claim_refs=(),
        generated_by=GeneratedBy(
            tool="wiki-compile",
            version=1,
            generated_at="2026-04-08T09:12:00Z",
        ),
        extensions={},
        tags=("sample",),
        body="Hello body\n",
    )


# ---------------------------------------------------------------------------
# allocate_id — happy path
# ---------------------------------------------------------------------------


def test_allocate_id_returns_base_on_empty_repo(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = repo.allocate_id(slug="customer-a-ops", timestamp="20260408163658")
    assert isinstance(result, Ok)
    assert result.value == "20260408163658-customer-a-ops"


def test_allocate_id_writes_stub_article_to_claim_id(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = repo.allocate_id(slug="customer-a-ops", timestamp="20260408163658")
    assert isinstance(result, Ok)
    stub_path = tmp_path / ".wiki" / "concepts" / f"{result.value}.md"
    assert stub_path.exists()


def test_allocate_id_stub_is_valid_v1_article(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = repo.allocate_id(slug="customer-a-ops", timestamp="20260408163658")
    assert isinstance(result, Ok)
    stub_path = tmp_path / ".wiki" / "concepts" / f"{result.value}.md"
    loaded = load_article(stub_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, Ok), f"stub not valid v1: {loaded!r}"
    assert loaded.value.article_id == "20260408163658-customer-a-ops"
    assert loaded.value.status == "unverified"  # stub is always unverified


# ---------------------------------------------------------------------------
# allocate_id — collision suffix
# ---------------------------------------------------------------------------


def test_allocate_id_appends_suffix_on_conflict(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    first = repo.allocate_id(slug="ops", timestamp="20260408163658")
    assert isinstance(first, Ok)
    assert first.value == "20260408163658-ops"

    second = repo.allocate_id(slug="ops", timestamp="20260408163658")
    assert isinstance(second, Ok)
    assert second.value == "20260408163658-ops-2"

    third = repo.allocate_id(slug="ops", timestamp="20260408163658")
    assert isinstance(third, Ok)
    assert third.value == "20260408163658-ops-3"


def test_allocate_id_collision_counts_pre_existing_files(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # Pre-place an article at the base id so allocation must suffix.
    existing = _make_sample_article(article_id="20260408163658-ops")
    (tmp_path / ".wiki" / "concepts" / "20260408163658-ops.md").write_text(
        dump_article(existing), encoding="utf-8"
    )
    result = repo.allocate_id(slug="ops", timestamp="20260408163658")
    assert isinstance(result, Ok)
    assert result.value == "20260408163658-ops-2"


# ---------------------------------------------------------------------------
# allocate_id — validation
# ---------------------------------------------------------------------------


def test_allocate_id_rejects_bad_slug(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = repo.allocate_id(slug="Bad Slug!!", timestamp="20260408163658")
    assert isinstance(result, Err)
    assert result.error == RepoError.INVALID_ID


def test_allocate_id_rejects_bad_timestamp(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = repo.allocate_id(slug="ops", timestamp="2026-04-08")
    assert isinstance(result, Err)
    assert result.error == RepoError.INVALID_ID


# ---------------------------------------------------------------------------
# allocate_id — lock handling
# ---------------------------------------------------------------------------


def test_allocate_id_surfaces_lock_timeout_as_err(tmp_path: Path) -> None:
    lock = FakeFileLock(always_times_out=True)
    repo = _make_repo(tmp_path, lock=lock)
    result = repo.allocate_id(slug="ops", timestamp="20260408163658")
    assert isinstance(result, Err)
    assert result.error == RepoError.LOCK_TIMEOUT


def test_allocate_id_acquires_lock_before_listing(tmp_path: Path) -> None:
    """The lock is taken before listing candidates, so that two concurrent
    allocate_id calls cannot hand out the same suffix."""
    lock = FakeFileLock()
    repo = _make_repo(tmp_path, lock=lock)
    repo.allocate_id(slug="ops", timestamp="20260408163658")
    repo.allocate_id(slug="ops", timestamp="20260408163658")
    # Two allocations -> two lock acquisitions, in order.
    assert len(lock.history) == 2
    # Both acquires target the same lock path (per-repo allocation lock).
    assert lock.history[0][0] == lock.history[1][0]


# ---------------------------------------------------------------------------
# save / load
# ---------------------------------------------------------------------------


def test_save_writes_atomically_and_load_returns_same_article(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    article = _make_sample_article()
    saved = repo.save(article)
    assert isinstance(saved, Ok)

    loaded = repo.load(article.article_id)
    assert isinstance(loaded, Ok)
    assert loaded.value == article


def test_save_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    article = _make_sample_article()
    repo.save(article)
    concepts = tmp_path / ".wiki" / "concepts"
    tmp_files = list(concepts.glob("*.tmp"))
    assert tmp_files == []


def test_save_rejects_bad_article_id(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    article = _make_sample_article(article_id="Bad Id!")
    result = repo.save(article)
    assert isinstance(result, Err)
    assert result.error == RepoError.INVALID_ID


def test_load_missing_returns_not_found(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = repo.load("20260408163658-does-not-exist")
    assert isinstance(result, Err)
    assert result.error == RepoError.NOT_FOUND


def test_load_legacy_v0_returns_schema_error(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    legacy_path = tmp_path / ".wiki" / "concepts" / "20260101000000-legacy.md"
    legacy_path.write_text(
        "---\n"
        "title: legacy\n"
        "type: wiki\n"
        "source_refs: [raw/foo.md]\n"
        "created: 2024-01-01\n"
        "updated: 2024-01-02\n"
        "category: concepts\n"
        "tags: []\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    result = repo.load("20260101000000-legacy")
    assert isinstance(result, Err)
    assert result.error == RepoError.SCHEMA_ERROR
    # Detail mentions legacy so that migrate.py can route accordingly.
    assert "legacy" in result.detail.lower()


# ---------------------------------------------------------------------------
# list_article_ids
# ---------------------------------------------------------------------------


def test_list_article_ids_returns_sorted_slugs(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ids = ["20260101-c", "20260101-a", "20260101-b"]
    for i in ids:
        art = _make_sample_article(article_id=i)
        repo.save(art)
    listed = repo.list_article_ids()
    assert listed == sorted(ids)


def test_list_article_ids_ignores_non_md_files(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (tmp_path / ".wiki" / "concepts" / "README.txt").write_text("x")
    (tmp_path / ".wiki" / "concepts" / ".hidden").write_text("x")
    assert repo.list_article_ids() == []


# ---------------------------------------------------------------------------
# Dependency Injection sanity
# ---------------------------------------------------------------------------


def test_wiki_repo_does_not_touch_filesystem_outside_wiki_root(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    repo.save(_make_sample_article())
    # Nothing leaked outside .wiki
    sibling_entries = [p for p in tmp_path.iterdir() if p.name != ".wiki"]
    assert sibling_entries == []


def test_save_and_overwrite_does_not_duplicate_files(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    article = _make_sample_article()
    repo.save(article)
    repo.save(replace(article, body="Updated\n"))
    concepts = tmp_path / ".wiki" / "concepts"
    assert len(list(concepts.glob("*.md"))) == 1
    loaded = repo.load(article.article_id)
    assert isinstance(loaded, Ok)
    assert loaded.value.body == "Updated\n"
