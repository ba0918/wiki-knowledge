"""Pure domain logic for the repo ingest MVP.

This module is **pure**: no I/O, no subprocess, no filesystem lookups. It
owns the repo-MVP-specific types (``RepoSource`` / ``DocCandidate`` /
``RepoManifest``) so that ``lib/domain/types.py`` (pipeline v1 schema) stays
untouched.

Security mapping (docs/plans/20260703224551_repo-ingest-mvp.md):

* **C-1** ``parse_repo_source`` is a positive-match allowlist. Only four
  input shapes are accepted: ``https://`` URLs, ``ssh://`` URLs, scp-like
  ``git@host:path`` and local-looking paths (no ``:`` at all). Everything
  else — ``ext::``, ``fd::``, ``git::``, ``file://``, plain ``http://`` —
  is structurally rejected.
* **H-3** userinfo is removed from the stored URL. For ``https`` the whole
  userinfo is a credential and is dropped; for ``ssh``/scp the username is
  protocol addressing (``git@``) and is kept, but a ``:password`` part is
  dropped. Either removal sets ``removed_userinfo=True`` so callers can warn.
* **H-5** hosts must fully match ``^[A-Za-z0-9][A-Za-z0-9.\\-]*$`` and ports
  must be digit-only. The ssh:// form parses ``:2222`` as a port while the
  scp-like form parses everything after ``:`` as a path — tests pin the
  difference.
* **M-3** control characters (including newlines and NUL) are rejected for
  every input kind.
* **H-2** ``discover_docs`` never yields ``.git/``, ``.env*`` or key
  material, and applies a fixed deny-list for generated/vendored trees.

Path *existence* checks (local repos) and path *containment* checks
(``resolve_safe_path``) are deliberately not here — they are service-layer
concerns (``lib/service/repo_clone.py``).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Sequence

from lib.domain.types import Err, Ok


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RepoParseError(str, Enum):
    """Discriminator for repo source parse failures."""

    EMPTY = "empty"
    INVALID_TYPE = "invalid_type"
    CONTROL_CHARACTER = "control_character"
    OPTION_INJECTION = "option_injection"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    INVALID_HOST = "invalid_host"
    INVALID_PORT = "invalid_port"
    INVALID_PATH = "invalid_path"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

RepoSourceKind = Literal["https", "ssh", "scp", "local"]

# H-5: host validation — first char alphanumeric, then alphanumeric/dot/dash.
HOST_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.\-]*$"
_HOST_RE = re.compile(HOST_PATTERN)
_PORT_RE = re.compile(r"^[0-9]+$")

# Deny-list of directory segments excluded from doc discovery (any depth).
DENY_DIR_SEGMENTS = frozenset({"dist", "build", "vendor", "node_modules", ".git"})

# Top-level files that hint at the repository's entry points.
ENTRYPOINT_FILES = frozenset(
    {
        "package.json",
        "main.go",
        "go.mod",
        "Dockerfile",
        "docker-compose.yml",
        "Cargo.toml",
        "pyproject.toml",
        "setup.py",
        "Makefile",
        "main.py",
        "index.js",
    }
)

# Nested paths that also count as entry points.
ENTRYPOINT_PATHS = frozenset(
    {
        "src/main.rs",
        "src/main.py",
        "src/index.ts",
        "src/index.js",
        "cmd/main.go",
    }
)

DEFAULT_MAX_DOCS = 50


@dataclass(frozen=True)
class RepoSource:
    """A validated repo source specification (allowlist-passed).

    ``url`` is the userinfo-sanitized form safe to store, log, and pass to
    ``git``/``ghq``. For ``kind="local"`` it is the original path text —
    existence/containment validation happens in the service layer.
    """

    kind: RepoSourceKind
    url: str
    host: str = ""
    owner: str = ""
    name: str = ""
    port: str | None = None
    removed_userinfo: bool = False


@dataclass(frozen=True)
class DocCandidate:
    """One document candidate discovered inside a repository.

    ``tier`` — 1: README/architecture/adr/docs-index, 2: API definitions
    (openapi/swagger), 3: other markdown.
    """

    path: str
    tier: int


@dataclass(frozen=True)
class RepoManifest:
    """Snapshot manifest of one ingested repository.

    ``docs`` is truncated to ``max_docs`` (see :func:`build_manifest`);
    ``docs_total`` / ``docs_truncated`` always describe the full set so the
    truncation is visible to consumers.
    """

    slug: str
    source_url: str
    clone_path: str
    revision: str
    top_level_dirs: tuple[str, ...]
    file_count_by_extension: dict[str, int]
    total_files: int
    entrypoints: tuple[str, ...]
    docs: tuple[DocCandidate, ...]
    docs_total: int
    docs_truncated: bool


# ---------------------------------------------------------------------------
# parse_repo_source
# ---------------------------------------------------------------------------


def parse_repo_source(text: str) -> Ok[RepoSource] | Err[RepoParseError]:
    """Parse a repo source string against the positive-match allowlist (C-1).

    Accepted shapes (and nothing else):

    * ``https://host[:port]/owner/name[.git]``
    * ``ssh://[user[:pass]@]host[:port]/owner/name[.git]``
    * ``git@host:owner/name[.git]`` (scp-like; user must be exactly ``git``)
    * local-looking paths — any text containing no ``:`` (existence is
      verified by the service layer)
    """

    if not isinstance(text, str):
        return Err(
            error=RepoParseError.INVALID_TYPE,
            detail=f"expected str, got {type(text).__name__}",
        )

    if _has_control_characters(text):
        return Err(error=RepoParseError.CONTROL_CHARACTER)

    stripped = text.strip()
    if not stripped:
        return Err(error=RepoParseError.EMPTY)

    if stripped.startswith("-"):
        # Argument injection (e.g. -oProxyCommand=...) — structurally banned.
        return Err(error=RepoParseError.OPTION_INJECTION, detail=stripped[:32])

    if stripped.startswith("https://"):
        return _parse_url(stripped, kind="https", scheme="https://")
    if stripped.startswith("ssh://"):
        return _parse_url(stripped, kind="ssh", scheme="ssh://")
    if stripped.startswith("git@"):
        return _parse_scp_like(stripped)
    if ":" in stripped:
        # Everything with a colon that is not an allowlisted form:
        # ext:: / fd:: / git:: / file:// / http:// / user@host:path ...
        return Err(error=RepoParseError.UNSUPPORTED_SCHEME, detail=stripped[:64])

    return Ok(value=RepoSource(kind="local", url=stripped))


def _has_control_characters(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _split_userinfo(authority: str) -> tuple[str | None, str]:
    """Split ``[userinfo@]hostport`` → (userinfo | None, hostport)."""

    if "@" in authority:
        userinfo, hostport = authority.rsplit("@", 1)
        return userinfo, hostport
    return None, authority


def _split_hostport(
    hostport: str,
) -> tuple[str, str | None] | Err[RepoParseError]:
    if ":" in hostport:
        host, port = hostport.split(":", 1)
        if not _PORT_RE.fullmatch(port):
            return Err(error=RepoParseError.INVALID_PORT, detail=port[:16])
    else:
        host, port = hostport, None
    if not _HOST_RE.fullmatch(host):
        return Err(error=RepoParseError.INVALID_HOST, detail=host[:64])
    return host, port


def _split_repo_path(path: str) -> tuple[str, str] | Err[RepoParseError]:
    """Split ``owner/name[.git]`` → (owner, name). Owner may span multiple
    segments (GitLab subgroups) and may be empty for single-segment paths."""

    segments = [s for s in path.split("/") if s]
    if not segments:
        return Err(error=RepoParseError.INVALID_PATH, detail="empty repo path")
    if any(seg in (".", "..") for seg in segments):
        return Err(error=RepoParseError.INVALID_PATH, detail="dot segment")
    if any(re.search(r"\s", seg) for seg in segments):
        return Err(error=RepoParseError.INVALID_PATH, detail="whitespace")
    name = segments[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if not name:
        return Err(error=RepoParseError.INVALID_PATH, detail="empty name")
    return "/".join(segments[:-1]), name


def _parse_url(
    text: str, *, kind: RepoSourceKind, scheme: str
) -> Ok[RepoSource] | Err[RepoParseError]:
    rest = text[len(scheme) :]
    authority, sep, path = rest.partition("/")
    if not sep or not path:
        return Err(error=RepoParseError.INVALID_PATH, detail="missing repo path")

    userinfo, hostport = _split_userinfo(authority)
    hp = _split_hostport(hostport)
    if isinstance(hp, Err):
        return hp
    host, port = hp

    repo_path = _split_repo_path(path)
    if isinstance(repo_path, Err):
        return repo_path
    owner, name = repo_path

    # H-3: userinfo handling. https → drop entirely. ssh → the username is
    # addressing (keep it); a :password part is a credential (drop it).
    removed = False
    kept_user = ""
    if userinfo is not None:
        if kind == "https":
            removed = True
        elif ":" in userinfo:
            kept_user = userinfo.split(":", 1)[0]
            removed = True
        else:
            kept_user = userinfo

    user_part = f"{kept_user}@" if kept_user else ""
    port_part = f":{port}" if port is not None else ""
    url = f"{scheme}{user_part}{host}{port_part}/{path}"

    return Ok(
        value=RepoSource(
            kind=kind,
            url=url,
            host=host,
            owner=owner,
            name=name,
            port=port,
            removed_userinfo=removed,
        )
    )


def _parse_scp_like(text: str) -> Ok[RepoSource] | Err[RepoParseError]:
    """Parse ``git@host:path``. The user must be exactly ``git`` (C-1) and
    the text after ``:`` is always a path, never a port (H-5)."""

    rest = text[len("git@") :]
    hostpart, sep, path = rest.partition(":")
    if not sep or not path:
        return Err(error=RepoParseError.UNSUPPORTED_SCHEME, detail=text[:64])
    if not _HOST_RE.fullmatch(hostpart):
        return Err(error=RepoParseError.INVALID_HOST, detail=hostpart[:64])

    repo_path = _split_repo_path(path)
    if isinstance(repo_path, Err):
        return repo_path
    owner, name = repo_path

    return Ok(
        value=RepoSource(
            kind="scp",
            url=f"git@{hostpart}:{path}",
            host=hostpart,
            owner=owner,
            name=name,
            port=None,
            removed_userinfo=False,
        )
    )


# ---------------------------------------------------------------------------
# normalize_repo_slug
# ---------------------------------------------------------------------------


def normalize_repo_slug(host: str, owner: str, name: str) -> str:
    """Normalize (host, owner, name) into a lowercase slug.

    NFC normalize → lowercase → fold every non-``[a-z0-9]`` run into a
    single ``-`` → trim leading/trailing separators. The result may be empty
    (e.g. all-CJK input); callers must validate with ``sanitize_id`` before
    using it as a filesystem identifier (defensive double-check, Architect
    C2).
    """

    joined = "-".join(part for part in (host, owner, name) if part)
    normalized = unicodedata.normalize("NFC", joined).lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


# ---------------------------------------------------------------------------
# discover_docs
# ---------------------------------------------------------------------------

_TIER1_DIR_SEGMENTS = frozenset({"adr", "adrs", "architecture", "decisions"})


def _is_sensitive(basename_lower: str) -> bool:
    """H-2: files that must never become doc candidates."""

    return (
        basename_lower == ".env"
        or basename_lower.startswith(".env.")
        or basename_lower.endswith((".pem", ".key"))
        or basename_lower.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"))
    )


def _classify_tier(path: str) -> int | None:
    """Return the doc tier for ``path``, or None if it is not a doc."""

    segments = path.split("/")
    basename = segments[-1]
    lower = basename.lower()
    path_lower = path.lower()

    if lower.startswith("readme"):
        return 1
    if lower.endswith(".md"):
        if path_lower == "docs/index.md":
            return 1
        if "architecture" in lower:
            return 1
        if any(seg.lower() in _TIER1_DIR_SEGMENTS for seg in segments[:-1]):
            return 1
    if lower.startswith(("openapi", "swagger")):
        return 2
    if lower.endswith(".md"):
        return 3
    return None


def discover_docs(file_list: Sequence[str]) -> tuple[DocCandidate, ...]:
    """Classify repository file paths into prioritized doc candidates.

    Deterministic: output is sorted by ``(tier, path)`` and deduplicated,
    independent of input order. Deny-listed directories (H-2 /
    :data:`DENY_DIR_SEGMENTS`) and sensitive files are excluded.
    """

    candidates: set[DocCandidate] = set()
    for raw in file_list:
        path = raw.strip().strip("/")
        if not path:
            continue
        segments = path.split("/")
        if any(seg in DENY_DIR_SEGMENTS for seg in segments):
            continue
        if _is_sensitive(segments[-1].lower()):
            continue
        tier = _classify_tier(path)
        if tier is None:
            continue
        candidates.add(DocCandidate(path=path, tier=tier))

    return tuple(sorted(candidates, key=lambda d: (d.tier, d.path)))


# ---------------------------------------------------------------------------
# build_manifest
# ---------------------------------------------------------------------------


def _extension_of(basename: str) -> str:
    stem, dot, ext = basename.rpartition(".")
    if not dot or not stem or not ext:
        return "(none)"
    return ext.lower()


def build_manifest(
    *,
    slug: str,
    source_url: str,
    clone_path: str,
    revision: str,
    all_files: Sequence[str],
    docs: Sequence[DocCandidate],
    max_docs: int = DEFAULT_MAX_DOCS,
) -> RepoManifest:
    """Build an immutable :class:`RepoManifest` from snapshot inputs.

    Pure and deterministic: all collections are sorted, ``docs`` is
    truncated to ``max_docs`` with ``docs_total`` / ``docs_truncated``
    recording the untruncated reality.
    """

    top_level_dirs = tuple(
        sorted({f.split("/", 1)[0] for f in all_files if "/" in f})
    )

    ext_counts: dict[str, int] = {}
    for f in all_files:
        ext = _extension_of(f.rsplit("/", 1)[-1])
        ext_counts[ext] = ext_counts.get(ext, 0) + 1

    entrypoints = tuple(
        sorted(
            f
            for f in all_files
            if ("/" not in f and f in ENTRYPOINT_FILES) or f in ENTRYPOINT_PATHS
        )
    )

    sorted_docs = tuple(sorted(docs, key=lambda d: (d.tier, d.path)))
    docs_total = len(sorted_docs)
    truncated = docs_total > max_docs

    return RepoManifest(
        slug=slug,
        source_url=source_url,
        clone_path=clone_path,
        revision=revision,
        top_level_dirs=top_level_dirs,
        file_count_by_extension=dict(sorted(ext_counts.items())),
        total_files=len(all_files),
        entrypoints=entrypoints,
        docs=sorted_docs[:max_docs],
        docs_total=docs_total,
        docs_truncated=truncated,
    )
