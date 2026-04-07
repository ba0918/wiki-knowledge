#!/usr/bin/env python3
"""wikilink_render: convert ``[[slug]]`` to ``[[slug]] ([↗](slug.md))``.

Pure transform + thin CLI layer. Idempotent: re-running on already-rendered
text is a no-op. Excludes fenced (``` ``` ```) and inline code spans, matching
the existing parser in ``lib/inventory.py``. Tilde fences (``~~~``), indented
code blocks, and HTML comments are NOT excluded — this is a known inherited
limitation; see ``[[wikilink-link-parser-spec]]``.

Usage:
    python wikilink_render.py --check PATH [PATH ...]
    python wikilink_render.py --write PATH [PATH ...]

Paths must live under a ``.wiki/`` directory (path-traversal guard).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Reuse the canonical regex constants from lib.inventory so the slug grammar
# and code-fence/inline-code exclusion stay in lockstep with the rest of the
# pipeline.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lib.inventory import _FENCE_RE, _INLINE_CODE_RE  # noqa: E402


# ---------------------------------------------------------------------------
# Pure transform
# ---------------------------------------------------------------------------

# Wikilink with span info — captures slug + optional alias as a whole.
# Mirrors lib.inventory._WIKILINK_RE but kept local because the renderer needs
# match positions (findall in lib does not preserve them).
_WIKILINK_SPAN_RE = re.compile(r"\[\[([a-z0-9-]+)(\|[^\]]*)?\]\]")

# Detects an already-rendered link of the form ``[[…]] ([↗](slug.md))``.
RENDERED_PATTERN = re.compile(
    r"\[\[[a-z0-9-]+(?:\|[^\]]*)?\]\] \(\[↗\]\([a-z0-9-]+\.md\)\)"
)

# Marker used to mask out code spans/fences during transformation. Chosen so
# it cannot collide with normal markdown content.
_MASK = "\x00WLR_MASK_{idx}\x00"
_MASK_RE = re.compile(r"\x00WLR_MASK_(\d+)\x00")


def _mask_code(text: str) -> tuple[str, list[str]]:
    """Replace code fences and inline code with placeholders.

    Returns ``(masked_text, segments)`` where segments[i] is the i-th
    replaced span. Order matters: fences first (multi-line), then inline.
    """
    segments: list[str] = []

    def _grab(match: re.Match) -> str:
        segments.append(match.group(0))
        return _MASK.format(idx=len(segments) - 1)

    masked = _FENCE_RE.sub(_grab, text)
    masked = _INLINE_CODE_RE.sub(_grab, masked)
    return masked, segments


def _unmask_code(text: str, segments: list[str]) -> str:
    return _MASK_RE.sub(lambda m: segments[int(m.group(1))], text)


def render_wikilinks(text: str) -> str:
    """Append ``([↗](slug.md))`` to every bare ``[[slug]]`` reference.

    Idempotent: links that already carry the rendered suffix are left alone.
    Code fences and inline code spans are not transformed. Pure function — no
    I/O, no globals, no existence checks.
    """
    masked, segments = _mask_code(text)

    def _replace(match: re.Match) -> str:
        slug = match.group(1)
        # Check whether this match is already followed by the rendered suffix.
        end = match.end()
        suffix = f" ([↗]({slug}.md))"
        if masked[end:end + len(suffix)] == suffix:
            return match.group(0)
        return f"{match.group(0)}{suffix}"

    rendered = _WIKILINK_SPAN_RE.sub(_replace, masked)
    return _unmask_code(rendered, segments)


# ---------------------------------------------------------------------------
# CLI layer (side effects only)
# ---------------------------------------------------------------------------

def _iter_targets(paths: list[Path]) -> list[Path]:
    """Expand directories to ``*.md`` files; pass files through unchanged."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(p.rglob("*.md")))
        else:
            out.append(p)
    return out


def _ensure_under_wiki(path: Path) -> None:
    """Path-traversal guard: every target must live under a ``.wiki`` dir."""
    parts = path.resolve().parts
    if ".wiki" not in parts:
        raise SystemExit(
            f"refusing to operate on {path}: must be under a .wiki/ directory"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render wikilinks to GitHub-clickable form ([[slug]] ([↗](slug.md)))",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Exit 1 if any file would change.")
    mode.add_argument("--write", action="store_true", help="Rewrite files in place.")
    parser.add_argument("paths", nargs="+", type=Path, help="Files or directories under .wiki/")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    targets = _iter_targets(args.paths)
    for t in targets:
        _ensure_under_wiki(t)

    dirty: list[Path] = []
    for path in targets:
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        rendered = render_wikilinks(original)
        if rendered != original:
            dirty.append(path)
            if args.write:
                path.write_text(rendered, encoding="utf-8")

    if args.check:
        if dirty:
            for p in dirty:
                print(f"would-render: {p}")
            return 1
        return 0

    # --write
    for p in dirty:
        print(f"rendered: {p}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
