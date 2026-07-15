"""Pure domain logic for source code classification.

Classifies repository source files into domain-knowledge categories
(schema, routes, rules, state, tests, entry) using deterministic path
pattern matching. No I/O, no LLM — this is a static scanner that feeds
the discover workflow.

Counterpart to ``discover_docs()`` in ``repo.py`` which classifies
*documentation* files. source_scan classifies *source code* files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class SourceCategory(str, Enum):
    SCHEMA = "schema"
    ROUTES = "routes"
    RULES = "rules"
    STATE = "state"
    TESTS = "tests"
    ENTRY = "entry"


CATEGORY_PRECEDENCE: tuple[SourceCategory, ...] = (
    SourceCategory.SCHEMA,
    SourceCategory.ROUTES,
    SourceCategory.RULES,
    SourceCategory.STATE,
    SourceCategory.TESTS,
    SourceCategory.ENTRY,
)

LARGE_FILE_THRESHOLD = 100 * 1024  # 100KB

_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".flac",
    ".pyc", ".pyo", ".class", ".jar", ".war",
    ".wasm", ".map",
    ".sqlite", ".db",
})

_DENY_DIR_SEGMENTS = frozenset({
    "dist", "build", "vendor", "node_modules", ".git",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".tox",
    ".eggs", "*.egg-info", "coverage", ".next", ".nuxt",
})


# --- Category patterns ---
# Each category has (dir_patterns, file_patterns) as compiled regexes.
# dir_patterns match against directory segments in the path.
# file_patterns match against the full relative path.


def _compile_patterns(
    dir_keywords: Sequence[str],
    file_patterns: Sequence[str],
) -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    return (
        tuple(re.compile(rf"(?:^|/)({kw})(?:/|$)", re.IGNORECASE) for kw in dir_keywords),
        tuple(re.compile(p, re.IGNORECASE) for p in file_patterns),
    )


_CATEGORY_PATTERNS: dict[
    SourceCategory,
    tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]],
] = {
    SourceCategory.SCHEMA: _compile_patterns(
        dir_keywords=[
            r"migrations?", r"migrate", r"db", r"database",
            r"models?", r"schema", r"entities?", r"prisma",
            r"alembic", r"sequelize", r"typeorm", r"knex",
        ],
        file_patterns=[
            r".*\.schema\.\w+$",
            r".*/schema\.(?:rb|py|ts|js|go|rs)$",
            r".*/models?\.(?:rb|py|ts|js|go|rs)$",
            r".*/entity\.(?:rb|py|ts|js|go|rs)$",
        ],
    ),
    SourceCategory.ROUTES: _compile_patterns(
        dir_keywords=[
            r"routes?", r"controllers?", r"handlers?", r"views?",
            r"endpoints?", r"api", r"resolvers?", r"pages",
        ],
        file_patterns=[
            r".*/routes?\.(?:rb|py|ts|js|go|rs)$",
            r".*/urls?\.py$",
            r".*/router\.(?:ts|js|go|rs)$",
            r".*\.controller\.(?:ts|js|rb)$",
            r".*\.handler\.(?:ts|js|go)$",
            r".*\.resolver\.(?:ts|js)$",
        ],
    ),
    SourceCategory.RULES: _compile_patterns(
        dir_keywords=[
            r"validators?", r"rules?", r"constants?", r"config",
            r"policies?", r"constraints?", r"domain",
            r"services?", r"use_?cases?",
        ],
        file_patterns=[
            r".*\.validator\.(?:ts|js|rb|py)$",
            r".*/validator\.(?:ts|js|rb|py)$",
            r".*/constants?\.(?:ts|js|rb|py|go|rs)$",
            r".*\.policy\.(?:ts|js|rb|py)$",
        ],
    ),
    SourceCategory.STATE: _compile_patterns(
        dir_keywords=[
            r"enums?", r"states?", r"status", r"machines?",
            r"workflows?", r"fsm",
        ],
        file_patterns=[
            r".*\.enum\.(?:ts|js|rb|py|go|rs)$",
            r".*/enums?\.(?:ts|js|rb|py|go|rs)$",
            r".*/status\.(?:ts|js|rb|py|go|rs)$",
            r".*/state_?machine\.(?:ts|js|rb|py|go|rs)$",
        ],
    ),
    SourceCategory.TESTS: _compile_patterns(
        dir_keywords=[
            r"__tests__", r"tests?", r"specs?", r"test_helpers?",
            r"fixtures?", r"factories?",
        ],
        file_patterns=[
            r".*/test_[^/]+\.(?:py|rb|go|rs)$",
            r".*/[^/]+_test\.(?:py|rb|go|rs)$",
            r".*/[^/]+\.(?:test|spec)\.(?:ts|tsx|js|jsx)$",
            r".*/[^/]+_spec\.rb$",
        ],
    ),
    SourceCategory.ENTRY: _compile_patterns(
        dir_keywords=[],
        file_patterns=[
            r"^(?:src/)?main\.(?:py|go|rs|ts|js|rb)$",
            r"^(?:src/)?app\.(?:py|ts|js|rb)$",
            r"^(?:src/)?index\.(?:ts|js|tsx|jsx)$",
            r"^(?:src/)?server\.(?:py|ts|js|go|rb)$",
            r"^manage\.py$",
            r"^(?:cmd|bin)/[^/]+\.(?:go|py|rb|rs)$",
        ],
    ),
}


@dataclass(frozen=True)
class SourceCandidate:
    path: str
    category: SourceCategory
    confidence: float
    size_bytes: int
    large_file_warning: bool


@dataclass(frozen=True)
class ScanResult:
    slug: str
    revision: str
    candidates: tuple[SourceCandidate, ...]
    stats: dict[str, int]
    skipped_count: int


def _is_binary(path: str) -> bool:
    dot_idx = path.rfind(".")
    if dot_idx == -1:
        return False
    return path[dot_idx:].lower() in _BINARY_EXTENSIONS


def _in_denied_dir(path: str) -> bool:
    segments = path.split("/")
    for seg in segments[:-1]:
        if seg in _DENY_DIR_SEGMENTS:
            return True
        if seg.endswith(".egg-info"):
            return True
    return False


def _score_file(
    path: str, category: SourceCategory,
) -> float:
    dir_patterns, file_patterns = _CATEGORY_PATTERNS[category]
    score = 0.0
    for p in dir_patterns:
        if p.search(path):
            score += 0.4
            break
    for p in file_patterns:
        if p.search(path):
            score += 0.6
            break
    return score


def classify_source_files(
    file_list: Sequence[tuple[str, int]],
    *,
    slug: str,
    revision: str,
    exclude_paths: frozenset[str] = frozenset(),
    categories: frozenset[SourceCategory] | None = None,
    max_files_per_category: int = 100,
) -> ScanResult:
    """Classify repository source files into domain-knowledge categories.

    ``file_list`` is a sequence of ``(relative_path, size_bytes)`` tuples.
    ``exclude_paths`` contains paths already classified by discover_docs.
    ``categories`` limits which categories to scan (None = all).
    ``max_files_per_category`` caps per-category output.
    """
    active_categories = categories or frozenset(SourceCategory)
    candidates: list[SourceCandidate] = []
    category_counts: dict[str, int] = {c.value: 0 for c in active_categories}
    skipped = 0

    for path, size_bytes in file_list:
        if path in exclude_paths:
            skipped += 1
            continue
        if _is_binary(path):
            skipped += 1
            continue
        if _in_denied_dir(path):
            skipped += 1
            continue

        best_category: SourceCategory | None = None
        best_score = 0.0

        for cat in CATEGORY_PRECEDENCE:
            if cat not in active_categories:
                continue
            score = _score_file(path, cat)
            if score > best_score:
                best_score = score
                best_category = cat

        if best_category is None or best_score <= 0.0:
            skipped += 1
            continue

        clamped_confidence = min(1.0, best_score)

        if category_counts[best_category.value] >= max_files_per_category:
            skipped += 1
            continue

        candidates.append(SourceCandidate(
            path=path,
            category=best_category,
            confidence=clamped_confidence,
            size_bytes=size_bytes,
            large_file_warning=size_bytes > LARGE_FILE_THRESHOLD,
        ))
        category_counts[best_category.value] += 1

    candidates.sort(key=lambda c: (-c.confidence, c.path))

    return ScanResult(
        slug=slug,
        revision=revision,
        candidates=tuple(candidates),
        stats=category_counts,
        skipped_count=skipped,
    )
