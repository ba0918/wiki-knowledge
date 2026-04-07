"""Pure inventory builder: concepts/*.md -> ArticleInventory list.

This module is the single source of truth for parsing wiki articles. It is
I/O-isolated via the ``parse_articles()`` function which accepts a wiki_root
Path. All parsing helpers (frontmatter, wikilinks, body extraction) are pure.

Design notes
------------
- ``ArticleInventory`` keeps both ``text`` and ``body`` in-memory because
  downstream lint checks (article_quality) need them. However when we
  serialize to ``inventory.json`` via :func:`to_json_dict`, both fields are
  excluded to prevent on-disk bloat.
- SHA-256 is computed over the *normalized* (LF) file content so CRLF / LF
  differences do not affect determinism.
- Output ordering: articles are sorted by slug; wikilinks preserve appearance
  order (not deduplicated) so later checks can reason about raw references.
- No PyYAML dependency: a minimal frontmatter parser is embedded here (moved
  from lint-wiki.py).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Frontmatter / body helpers (pure)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_WIKILINK_RE = re.compile(r"\[\[([a-z0-9-]+)(?:\|[^\]]*)?\]\]")


def normalize_newlines(text: str) -> str:
    """Return text with CRLF/CR normalized to LF for deterministic hashing."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def compute_sha256(text: str) -> str:
    """Compute SHA-256 of the normalized text content."""
    normalized = normalize_newlines(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_wikilinks(text: str) -> list[str]:
    """Extract ``[[slug]]`` and ``[[slug|alias]]`` references from text.

    Wikilinks inside fenced code blocks and inline code spans are excluded.
    Appearance order is preserved; duplicates are kept.
    """
    stripped = _FENCE_RE.sub("", text)
    stripped = _INLINE_CODE_RE.sub("", stripped)
    return _WIKILINK_RE.findall(stripped)


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter parser (no PyYAML)."""
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm: dict = {}
    last_key: str | None = None
    for line in text[3:end].strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and last_key is not None:
            if not isinstance(fm.get(last_key), list):
                fm[last_key] = [] if not fm.get(last_key) else [fm[last_key]]
            fm[last_key].append(
                stripped.removeprefix("- ").strip('"').strip("'")
            )
        elif ":" in line and not stripped.startswith("-"):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            last_key = key
            if value.startswith("[") and value.endswith("]"):
                fm[key] = [
                    v.strip().strip('"').strip("'")
                    for v in value[1:-1].split(",")
                    if v.strip()
                ]
            elif value == "":
                fm[key] = []
            else:
                fm[key] = value.strip('"').strip("'")
    return fm


def extract_body(text: str) -> str:
    """Return content after frontmatter (or the full text if absent)."""
    if not text.startswith("---"):
        return text
    end = text.find("---", 3)
    if end == -1:
        return text
    return text[end + 3:].strip()


# ---------------------------------------------------------------------------
# ArticleInventory
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleInventory:
    """Immutable metadata + content for a single wiki article.

    Kept in-memory for lint (body / text required for article_quality). When
    serialized to disk via :func:`to_json_dict`, ``text`` and ``body`` are
    excluded to keep ``inventory.json`` small.
    """

    slug: str
    path: str                 # wiki_root-relative, POSIX separator
    sha256: str
    title: str
    category: str
    type: str
    updated: str
    tags: tuple[str, ...]
    wikilinks: tuple[str, ...]
    source_refs: tuple[str, ...]
    frontmatter: dict
    text: str                 # full file (LF-normalized)
    body: str                 # content after frontmatter


def parse_article(
    md_path: Path, *, wiki_root: Path, text: str | None = None
) -> ArticleInventory:
    """Pure constructor: build an ArticleInventory from a markdown file.

    ``text`` may be injected for unit tests; otherwise the file is read from
    disk once.
    """
    if text is None:
        text = md_path.read_text(encoding="utf-8")
    text = normalize_newlines(text)

    fm = parse_frontmatter(text)
    body = extract_body(text)
    wikilinks = find_wikilinks(text)

    tags_raw = fm.get("tags", [])
    if isinstance(tags_raw, str):
        tags_raw = [tags_raw]
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, list) else ()

    source_refs_raw = fm.get("source_refs", [])
    if isinstance(source_refs_raw, str):
        source_refs_raw = [source_refs_raw]
    source_refs = tuple(
        str(s) for s in source_refs_raw
    ) if isinstance(source_refs_raw, list) else ()

    try:
        rel = md_path.resolve().relative_to(wiki_root.resolve())
    except ValueError as exc:
        # Path traversal guard: do not allow paths outside wiki_root.
        raise ValueError(
            f"Article path {md_path} is outside wiki_root {wiki_root}"
        ) from exc

    return ArticleInventory(
        slug=md_path.stem,
        path=rel.as_posix(),
        sha256=compute_sha256(text),
        title=str(fm.get("title", md_path.stem)),
        category=str(fm.get("category", "")),
        type=str(fm.get("type", "")),
        updated=str(fm.get("updated", "")),
        tags=tags,
        wikilinks=tuple(wikilinks),
        source_refs=source_refs,
        frontmatter=fm,
        text=text,
        body=body,
    )


def parse_articles(wiki_root: Path) -> list[ArticleInventory]:
    """Parse all concepts/*.md under ``wiki_root``, sorted by slug.

    Returns an empty list when ``concepts/`` does not exist.
    """
    concepts_dir = wiki_root / "concepts"
    if not concepts_dir.exists():
        return []

    articles: list[ArticleInventory] = []
    for md_file in sorted(concepts_dir.glob("*.md")):
        articles.append(parse_article(md_file, wiki_root=wiki_root))
    articles.sort(key=lambda a: a.slug)
    return articles


# ---------------------------------------------------------------------------
# JSON serialization (metadata only; body/text excluded)
# ---------------------------------------------------------------------------

def article_to_json_dict(article: ArticleInventory) -> dict:
    """Serialize an article for inventory.json (excludes text/body)."""
    return {
        "slug": article.slug,
        "path": article.path,
        "sha256": article.sha256,
        "title": article.title,
        "category": article.category,
        "type": article.type,
        "updated": article.updated,
        "tags": list(article.tags),
        "wikilinks": list(article.wikilinks),
        "source_refs": list(article.source_refs),
        "frontmatter": article.frontmatter,
    }


def to_json_dict(
    articles: list[ArticleInventory], *, wiki_root: Path, generated_at: str
) -> dict:
    """Build the top-level inventory.json dict (deterministic)."""
    return {
        "version": "1.0",
        "generated_at": generated_at,
        "wiki_root": str(wiki_root),
        "articles": [article_to_json_dict(a) for a in articles],
    }


def to_json(
    articles: list[ArticleInventory], *, wiki_root: Path, generated_at: str
) -> str:
    """Serialize to a canonical JSON string (sort_keys=True, 2-space indent)."""
    return json.dumps(
        to_json_dict(articles, wiki_root=wiki_root, generated_at=generated_at),
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Legacy compatibility shim
# ---------------------------------------------------------------------------

def build_inventory(wiki_root: Path) -> dict[str, ArticleInventory]:
    """Return inventory as a ``{slug: ArticleInventory}`` mapping.

    This matches the signature of the legacy ``_build_inventory`` previously
    living inside ``lint-wiki.py``.
    """
    return {a.slug: a for a in parse_articles(wiki_root)}
