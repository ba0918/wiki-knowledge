"""Unit tests for lib/domain/repo.py (repo ingest MVP — pure domain layer).

Covers the Security Considerations table of the plan
(docs/plans/20260703224551_repo-ingest-mvp.md):

* C-1  positive-match allowlist (https:// / ssh:// / git@host:path / local)
* H-3  userinfo removal + ``removed_userinfo`` flag
* H-5  host regex ``^[A-Za-z0-9][A-Za-z0-9.\\-]*$`` / digit-only port /
       ssh:// vs scp-like parse difference
* M-3  control character / newline rejection
* H-2  sensitive files (.env* / key material / .git) excluded from discovery
"""

from __future__ import annotations

import dataclasses

import pytest

from lib.domain.repo import (
    DocCandidate,
    RepoManifest,
    RepoParseError,
    RepoSource,
    build_manifest,
    discover_docs,
    normalize_repo_slug,
    parse_repo_source,
)
from lib.domain.types import Err, Ok


# ---------------------------------------------------------------------------
# parse_repo_source — accepted forms (C-1 allowlist)
# ---------------------------------------------------------------------------


def test_parse_https_basic() -> None:
    result = parse_repo_source("https://github.com/BurntSushi/ripgrep")
    assert isinstance(result, Ok)
    src = result.value
    assert src.kind == "https"
    assert src.host == "github.com"
    assert src.owner == "BurntSushi"
    assert src.name == "ripgrep"
    assert src.port is None
    assert src.url == "https://github.com/BurntSushi/ripgrep"
    assert src.removed_userinfo is False


def test_parse_https_strips_dot_git_suffix_from_name() -> None:
    result = parse_repo_source("https://github.com/BurntSushi/ripgrep.git")
    assert isinstance(result, Ok)
    assert result.value.name == "ripgrep"


def test_parse_https_with_port() -> None:
    result = parse_repo_source("https://gitlab.example.com:8443/group/proj")
    assert isinstance(result, Ok)
    src = result.value
    assert src.host == "gitlab.example.com"
    assert src.port == "8443"
    assert src.url == "https://gitlab.example.com:8443/group/proj"


def test_parse_https_gitlab_subgroup_owner() -> None:
    result = parse_repo_source("https://gitlab.example.com/group/sub/proj")
    assert isinstance(result, Ok)
    src = result.value
    assert src.owner == "group/sub"
    assert src.name == "proj"


def test_parse_https_removes_userinfo_and_sets_flag() -> None:
    # H-3: token@host must never survive into the stored URL.
    result = parse_repo_source("https://sometoken@gitlab.example.com/group/proj")
    assert isinstance(result, Ok)
    src = result.value
    assert src.removed_userinfo is True
    assert "sometoken" not in src.url
    assert src.url == "https://gitlab.example.com/group/proj"


def test_parse_https_removes_user_pass_userinfo() -> None:
    result = parse_repo_source("https://user:secret@example.com/o/r")
    assert isinstance(result, Ok)
    src = result.value
    assert src.removed_userinfo is True
    assert "secret" not in src.url
    assert "user" not in src.url.split("example.com")[0].replace("https://", "")


def test_parse_ssh_url_keeps_username_without_password() -> None:
    # ssh user (typically "git") is addressing, not a credential.
    result = parse_repo_source("ssh://git@gitlab.example.com/group/proj.git")
    assert isinstance(result, Ok)
    src = result.value
    assert src.kind == "ssh"
    assert src.host == "gitlab.example.com"
    assert src.owner == "group"
    assert src.name == "proj"
    assert src.url == "ssh://git@gitlab.example.com/group/proj.git"
    assert src.removed_userinfo is False


def test_parse_ssh_url_with_port() -> None:
    # H-5: ssh:// form — the ":2222" is a port.
    result = parse_repo_source("ssh://git@example.com:2222/owner/repo")
    assert isinstance(result, Ok)
    src = result.value
    assert src.port == "2222"
    assert src.owner == "owner"
    assert src.name == "repo"


def test_parse_ssh_url_strips_password_but_keeps_user() -> None:
    result = parse_repo_source("ssh://git:hunter2@example.com/o/r")
    assert isinstance(result, Ok)
    src = result.value
    assert src.removed_userinfo is True
    assert "hunter2" not in src.url
    assert src.url == "ssh://git@example.com/o/r"


def test_parse_scp_like_basic() -> None:
    result = parse_repo_source("git@github.com:BurntSushi/ripgrep.git")
    assert isinstance(result, Ok)
    src = result.value
    assert src.kind == "scp"
    assert src.host == "github.com"
    assert src.owner == "BurntSushi"
    assert src.name == "ripgrep"
    assert src.port is None
    assert src.url == "git@github.com:BurntSushi/ripgrep.git"


def test_parse_scp_like_colon_starts_path_not_port() -> None:
    # H-5: scp-like form — everything after ":" is a path, never a port.
    result = parse_repo_source("git@example.com:2222/owner/repo")
    assert isinstance(result, Ok)
    src = result.value
    assert src.port is None
    assert src.owner == "2222/owner"
    assert src.name == "repo"


def test_parse_ssh_url_rejects_non_numeric_port() -> None:
    # The same text shape under ssh:// must fail: "owner" is not a port.
    result = parse_repo_source("ssh://git@example.com:owner/repo")
    assert isinstance(result, Err)
    assert result.error == RepoParseError.INVALID_PORT


def test_parse_local_absolute_path() -> None:
    result = parse_repo_source("/home/user/repos/myproj")
    assert isinstance(result, Ok)
    assert result.value.kind == "local"


def test_parse_local_tilde_path() -> None:
    result = parse_repo_source("~/ghq/github.com/BurntSushi/ripgrep")
    assert isinstance(result, Ok)
    assert result.value.kind == "local"


def test_parse_local_relative_path() -> None:
    result = parse_repo_source("./some/repo")
    assert isinstance(result, Ok)
    assert result.value.kind == "local"


# ---------------------------------------------------------------------------
# parse_repo_source — rejections (C-1 / H-5 / M-3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   "])
def test_parse_rejects_empty(value: str) -> None:
    result = parse_repo_source(value)
    assert isinstance(result, Err)
    assert result.error == RepoParseError.EMPTY


def test_parse_rejects_non_string() -> None:
    result = parse_repo_source(123)  # type: ignore[arg-type]
    assert isinstance(result, Err)
    assert result.error == RepoParseError.INVALID_TYPE


def test_parse_rejects_leading_dash_option_injection() -> None:
    result = parse_repo_source("-oProxyCommand=calc")
    assert isinstance(result, Err)
    assert result.error == RepoParseError.OPTION_INJECTION


@pytest.mark.parametrize(
    "value",
    [
        "ext::sh -c whoami",
        "fd::3",
        "git::https://example.com/o/r",
        "file:///etc/passwd",
        "http://example.com/o/r",  # plain http is NOT allowlisted
        "ftp://example.com/o/r",
        "root@example.com:o/r",  # scp-like requires the git@ user exactly
        "example.com:o/r",  # bare scp-like without user
    ],
)
def test_parse_rejects_non_allowlisted_schemes(value: str) -> None:
    # C-1: positive-match allowlist — anything else is structurally rejected.
    result = parse_repo_source(value)
    assert isinstance(result, Err)
    assert result.error == RepoParseError.UNSUPPORTED_SCHEME


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/o/r\nevil",
        "https://example.com/o/r\x00",
        "/local/path\nwith-newline",
    ],
)
def test_parse_rejects_control_characters(value: str) -> None:
    # M-3: control characters / newlines are rejected for every kind.
    result = parse_repo_source(value)
    assert isinstance(result, Err)
    assert result.error == RepoParseError.CONTROL_CHARACTER


@pytest.mark.parametrize(
    "value",
    [
        "https://bad_host/o/r",  # underscore not allowed in host
        "https://-evil.com/o/r",  # leading dash
        "https://exa mple.com/o/r",  # whitespace
        "ssh://git@bad_host/o/r",
    ],
)
def test_parse_rejects_invalid_host(value: str) -> None:
    # H-5: host must fully match ^[A-Za-z0-9][A-Za-z0-9.\-]*$
    result = parse_repo_source(value)
    assert isinstance(result, Err)
    assert result.error == RepoParseError.INVALID_HOST


def test_parse_rejects_non_numeric_port_https() -> None:
    result = parse_repo_source("https://example.com:22a/o/r")
    assert isinstance(result, Err)
    assert result.error == RepoParseError.INVALID_PORT


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com",  # no repo path at all
        "https://example.com/",
        "https://example.com/../etc",  # traversal segments
        "https://example.com/o/..",
        "git@example.com:../escape",
    ],
)
def test_parse_rejects_invalid_path(value: str) -> None:
    result = parse_repo_source(value)
    assert isinstance(result, Err)
    assert result.error == RepoParseError.INVALID_PATH


# ---------------------------------------------------------------------------
# RepoSource — immutability
# ---------------------------------------------------------------------------


def test_repo_source_is_frozen() -> None:
    result = parse_repo_source("https://github.com/o/r")
    assert isinstance(result, Ok)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.value.host = "evil.com"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# normalize_repo_slug
# ---------------------------------------------------------------------------


def test_normalize_slug_lowercases_and_folds_dots() -> None:
    slug = normalize_repo_slug("github.com", "BurntSushi", "ripgrep")
    assert slug == "github-com-burntsushi-ripgrep"


def test_normalize_slug_dots_in_name() -> None:
    slug = normalize_repo_slug("github.com", "Microsoft", "TypeScript.DOM")
    assert slug == "github-com-microsoft-typescript-dom"


def test_normalize_slug_folds_japanese_to_separator() -> None:
    # Non-ASCII (e.g. Japanese) has no slug representation; it folds away.
    slug = normalize_repo_slug("github.com", "日本語", "リポジトリ")
    assert slug == "github-com"


def test_normalize_slug_collapses_consecutive_symbols() -> None:
    slug = normalize_repo_slug("", "", "My__Repo..js")
    assert slug == "my-repo-js"


def test_normalize_slug_empty_components_are_skipped() -> None:
    assert normalize_repo_slug("", "", "Ripgrep") == "ripgrep"


def test_normalize_slug_all_non_ascii_returns_empty() -> None:
    assert normalize_repo_slug("", "", "日本語") == ""


def test_normalize_slug_applies_nfc() -> None:
    # NFD "é" (e + combining acute) → NFC → non-ascii → folded, deterministic
    nfd_name = "café-repo"
    assert normalize_repo_slug("", "", nfd_name) == normalize_repo_slug(
        "", "", "café-repo"
    )


# ---------------------------------------------------------------------------
# discover_docs — tiers, deny-list, determinism (H-2)
# ---------------------------------------------------------------------------


def test_discover_docs_tier1_readme_architecture_adr_index() -> None:
    files = [
        "README.md",
        "crates/core/README.md",
        "docs/index.md",
        "docs/architecture.md",
        "docs/adr/0001-record.md",
    ]
    docs = discover_docs(files)
    assert all(d.tier == 1 for d in docs)
    assert [d.path for d in docs] == sorted(files)


def test_discover_docs_tier2_openapi_swagger() -> None:
    docs = discover_docs(["openapi.yaml", "api/swagger.json"])
    assert {d.path: d.tier for d in docs} == {
        "openapi.yaml": 2,
        "api/swagger.json": 2,
    }


def test_discover_docs_tier3_other_markdown() -> None:
    docs = discover_docs(["CHANGELOG.md", "docs/guide.md"])
    assert all(d.tier == 3 for d in docs)


def test_discover_docs_ignores_non_doc_files() -> None:
    docs = discover_docs(["src/main.rs", "Cargo.toml", "image.png"])
    assert docs == ()


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/pkg/README.md",
        "dist/README.md",
        "build/docs/index.md",
        "vendor/lib/GUIDE.md",
        ".git/description.md",
    ],
)
def test_discover_docs_deny_list_directories(path: str) -> None:
    assert discover_docs([path]) == ()


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "config/id_rsa",
        "certs/server.pem",
        "certs/server.key",
    ],
)
def test_discover_docs_excludes_sensitive_files(path: str) -> None:
    # H-2: .env* / key material never become doc candidates.
    assert discover_docs([path]) == ()


def test_discover_docs_is_deterministic_regardless_of_input_order() -> None:
    files = ["docs/guide.md", "README.md", "openapi.yaml", "ARCHITECTURE.md"]
    forward = discover_docs(files)
    backward = discover_docs(list(reversed(files)))
    assert forward == backward
    # sorted by (tier, path)
    assert [(d.tier, d.path) for d in forward] == sorted(
        (d.tier, d.path) for d in forward
    )


def test_doc_candidate_is_frozen() -> None:
    (doc,) = discover_docs(["README.md"])
    assert isinstance(doc, DocCandidate)
    with pytest.raises(dataclasses.FrozenInstanceError):
        doc.tier = 3  # type: ignore[misc]


# ---------------------------------------------------------------------------
# build_manifest — structure metadata + truncation
# ---------------------------------------------------------------------------


def _manifest(**overrides: object) -> RepoManifest:
    files = [
        "README.md",
        "Cargo.toml",
        "src/main.rs",
        "src/lib.rs",
        "docs/guide.md",
        ".gitignore",
    ]
    kwargs: dict = dict(
        slug="github-com-o-r",
        source_url="https://github.com/o/r",
        clone_path="/tmp/clones/o/r",
        revision="abc1234def",
        all_files=files,
        docs=discover_docs(files),
        max_docs=50,
    )
    kwargs.update(overrides)
    return build_manifest(**kwargs)


def test_build_manifest_structure_metadata() -> None:
    m = _manifest()
    assert m.slug == "github-com-o-r"
    assert m.revision == "abc1234def"
    assert m.top_level_dirs == ("docs", "src")
    assert m.total_files == 6
    assert m.file_count_by_extension["rs"] == 2
    assert m.file_count_by_extension["md"] == 2
    assert m.file_count_by_extension["toml"] == 1


def test_build_manifest_entrypoint_candidates() -> None:
    m = _manifest(
        all_files=["Cargo.toml", "package.json", "nested/package.json", "src/main.rs"]
    )
    assert "Cargo.toml" in m.entrypoints
    assert "package.json" in m.entrypoints
    assert "src/main.rs" in m.entrypoints
    assert "nested/package.json" not in m.entrypoints


def test_build_manifest_truncates_docs_at_max_docs() -> None:
    files = [f"docs/page-{i:03d}.md" for i in range(60)]
    m = _manifest(all_files=files, docs=discover_docs(files), max_docs=50)
    assert len(m.docs) == 50
    assert m.docs_total == 60
    assert m.docs_truncated is True


def test_build_manifest_no_truncation_flag_when_under_limit() -> None:
    m = _manifest()
    assert m.docs_truncated is False
    assert m.docs_total == len(m.docs)


def test_build_manifest_is_frozen_and_deterministic() -> None:
    a = _manifest()
    b = _manifest()
    assert a == b
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.revision = "zzz"  # type: ignore[misc]
