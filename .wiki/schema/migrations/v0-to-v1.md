# v0 → v1 Field Mapping Specification

**Status**: authoritative specification for `lib/service/migrations/v0_to_v1.py`
**Schema**: v0 (`page-template.json`) → v1 (`page-template-v1.json`)
**First use**: Phase 0.13 (existing 12 articles in `.wiki/concepts/`)
**Updated**: 2026-04-09

## Purpose

Define the **exact** transformation rules from the legacy `type: wiki` frontmatter (v0) to the v1 schema introduced by the Source-Agnostic Knowledge Pipeline. This document is the single source of truth — any divergence between this file and `lib/service/migrations/v0_to_v1.py` is a bug, and both must move together.

The migration MUST be **lossless**: every v0 field is either mapped to a v1 field or stashed under `extensions.legacy` so that a future `v1_to_v0` migration (down path) can reconstruct the original state exactly.

## Scope

- **Input**: v0 frontmatter mapping (already parsed by `python-frontmatter` or equivalent) + raw body string + file reader callable for `content_hash`.
- **Output**: `Ok[Article]` (v1 domain type from `lib/domain/types.py`) or `Err[MigrationError]` on failure.
- **Not in scope**:
  - File I/O, backup, rollback — handled by `backup.py` and `migrate.py` handler.
  - `article_id` **allocation** (no collisions expected for legacy migration because existing slugs are guaranteed unique within `.wiki/concepts/`). The migration reuses the existing slug verbatim.
  - Body transformation (wikilinks, GitHub render artifacts) — body is preserved byte-for-byte except for the trailing newline convention enforced by `lib/service/schema.py`.

## Design principles

1. **Lossless** — no v0 field is dropped. Fields that have no direct v1 equivalent land under `extensions["legacy"]`.
2. **Pure** — the migration is a pure function of its inputs. File reads are injected via a `file_reader` callable; timestamps come from an injected `Clock`. No module-level globals.
3. **Deterministic** — given the same inputs (mapping, body, file contents, clock), the output Article is byte-identical.
4. **Fail closed on ambiguity** — unknown or invalid inputs return `Err(MigrationError)` rather than guessing.
5. **Preserve identity** — the v0 slug becomes the v1 `article_id` verbatim. Existing cross-references keep working. **Do not** prefix with a timestamp — that pattern is reserved for newly-allocated articles after Phase 0.

## Field mapping table

Legend: ✅ direct / 🔁 transformed / 🗂️ stashed in `extensions.legacy` / 🆕 synthesized.

| v0 field | v1 destination | kind | Transformation |
|---|---|---|---|
| `title` | `title` | ✅ | verbatim string, must be non-empty |
| `type` (const `"wiki"`) | `extensions.legacy.type` | 🗂️ | verbatim. Used by `v1_to_v0` to reconstruct the const. Rejected if not `"wiki"`. |
| `source_refs[]` | `sources[]` | 🔁 | see [sources[] construction](#sources-construction) |
| `created` (`YYYY-MM-DD`) | `captured_at` **and** `knowledge_time.valid_from` | 🔁 | same value written to both fields. `captured_at` = "when we captured this article into the wiki", `knowledge_time.valid_from` = "when the knowledge became valid"; for legacy articles these coincide by definition. |
| `updated` (`YYYY-MM-DD`) | `extensions.legacy.updated` | 🗂️ | v1 does not have a top-level `updated` field (content drift is tracked via `sources[].source_version` + `generated_by`). Lossless stash. |
| `category` (slug from `categories.json`) | `article_type` (via mapping table) **and** `extensions.legacy.category` | 🔁 🗂️ | see [article_type derivation](#article_type-derivation). Original `category` is always stashed under `extensions.legacy.category` regardless of the chosen `article_type`. |
| `tags[]` | `tags[]` | 🔁 | v0 tags are prepended, then `"legacy-v0"` is appended as a tombstone marker. Duplicates removed while preserving first-seen order. |
| `related[]` (paths like `concepts/foo.md`) | `relations.related_to[]` | 🔁 | each path is converted via `Path(p).stem` → `article_id`. See [relations conversion](#relations-conversion). |
| body (markdown) | `Article.body` / `Segment.text` | ✅ | verbatim, except for the trailing-newline rules already enforced by `lib/service/schema.py`. |

All v1 required fields not listed above are **synthesized** (🆕) with canonical defaults — see [synthesized v1 fields](#synthesized-v1-fields).

## article_id derivation

```python
article_id = Path(source_markdown_path).stem
# e.g. ".wiki/concepts/querylog.md" → "querylog"
```

- Validated via `lib/service/path_validator.sanitize_id`. An id that fails the slug pattern `^[a-z0-9][a-z0-9_-]{0,126}[a-z0-9]$|^[a-z0-9]$` returns `Err(MigrationError.INVALID_ID)`.
- **No timestamp prefix**. Legacy article_ids intentionally differ from the post-Phase-0 allocation format (`YYYYMMDDHHMMSS-{slug}`). The mix is permanent; `article_id` is immutable.
- **Uniqueness** is guaranteed by the filesystem (only one `.md` file per stem in `.wiki/concepts/`). The migration does not invoke `wiki_repo.allocate_id`.

## article_type derivation

v0 `category` → v1 `article_type` via this table:

| v0 `category` | v1 `article_type` | Rationale |
|---|---|---|
| `concepts` | `concept` | direct match |
| `tools` | `reference` | tooling comparison / capability tables are reference material |
| `practices` | `runbook` | workflow / procedure-oriented |
| `references` | `reference` | direct match |
| _anything else_ | `concept` (fallback) | `concept` has `x-fallback-for-migration: true` in `article-types/concept.json` |

**Fallback marker**: whenever the fallback path is taken (`_anything else_` row), the tag `"legacy-unmapped-category"` MUST be added to `tags[]` in addition to `"legacy-v0"`, so that `review` can later reclassify these articles.

**Original category is always preserved** under `extensions.legacy.category`, regardless of which `article_type` was chosen. This is non-negotiable for lossless round-trip.

## status initial value

Legacy articles are set to **`"current"`**.

Rationale: these articles were authored by a human and are in active use at migration time. Starting them at `unverified` would blanket-demote real content and defeat the purpose of the wiki during the dogfooding phase. `lint-wiki` / `review` can downgrade individual articles later.

**Exception**: if **all** `sources[].content_hash` entries had to fall back to the placeholder (`sha256:` + "0"*64) because every source file was missing, status is written as `"unverified"` — we cannot claim an article is current if we cannot verify any of its evidence.

## sources[] construction

Each v0 `source_refs[i]` (a relative path string) is expanded into a full v1 `Source`:

```python
Source(
    id=f"legacy-{i+1}",                              # 1-based, stable across re-runs
    type="file",                                     # v0 only ever referenced files
    ref=source_refs[i],                              # verbatim — do NOT normalize paths
    source_version=1,                                # legacy sources have never been re-fetched
    content_hash=_compute_content_hash(source_refs[i], file_reader),
    fetched_at=clock.now_iso8601_utc(),              # the migration timestamp, see below
    permalink=None,
)
```

- **`ref` is verbatim**: the v0 convention is `{wiki_root}`-relative, and v1 keeps the same convention. Path normalization is forbidden here — it would invalidate existing file references.
- **`content_hash` computation**:
  - `file_reader(ref)` is called and expected to return `bytes | None`.
  - If bytes are returned: `content_hash = "sha256:" + hashlib.sha256(bytes).hexdigest()`.
  - If `None` (file missing at migration time): `content_hash = "sha256:" + ("0" * 64)`, and the boolean flag `any_source_missing` is set, which later forces `status = "unverified"`.
- **`fetched_at` is the migration timestamp**, not the v0 `created` date. This is the correct semantic: "this is when we captured this source **with this exact hash** into v1 provenance". `clock.now_iso8601_utc()` is evaluated **once per migration batch** so that all sources within a single run share the same timestamp.
- **`id`**: `"legacy-1"`, `"legacy-2"`, …. Stable and deterministic; reused if the migration is re-run over the same v0 input.

### File reader contract

```python
FileReader = Callable[[str], Optional[bytes]]
```

- Input: the v0 `source_refs[i]` string (e.g. `"raw/articles/20260406-querylog-feature.md"`).
- Output: raw bytes of the file, or `None` if not found.
- Implementation must resolve the path via `path_validator.resolve_safe_path(wiki_root, ref)` to block traversal. Concrete `WikiRootFileReader` lives in `wiki_repo.py` or `migrations/backup.py` — **not** in `v0_to_v1.py`, to keep the migration pure.

## relations conversion

```python
related_to = []
for p in v0_mapping.get("related", []):
    stem = Path(p).stem                               # "concepts/foo.md" → "foo"
    result = sanitize_id(stem)
    if is_err(result):
        return Err(MigrationError.INVALID_RELATED)
    related_to.append(result.value)
# preserve order, remove exact duplicates (first-seen wins)
```

- Input format is always `concepts/{slug}.md` in the existing 12 articles; the migration accepts any path that ends in a valid slug stem.
- Output values are bare article_ids (e.g. `"querylog"`). They do **not** include paths or file extensions.
- All other `relations` fields are initialized to their canonical empty values:
  - `supersedes = []`
  - `superseded_by = None`
  - `caused_by = []`
  - `derived_from = []`
  - `implements = []`
  - `depends_on = []`

## extensions.legacy stash

```python
extensions = {
    "legacy": {
        "type": v0_mapping["type"],          # always "wiki"
        "category": v0_mapping["category"],  # original slug
        "updated": v0_mapping["updated"],    # original YYYY-MM-DD
    }
}
```

- Keys inside `extensions.legacy` are **reserved** for the migration. Other tools must not write there.
- If the v0 mapping contains any fields not recognized by this spec (future-proofing), they are stashed under `extensions.legacy._unknown = { field: value, … }` rather than dropped. The domain load path already has a parallel `extensions["_unknown"]` convention at the top level; `extensions.legacy._unknown` is scoped to legacy-only unknowns.

## Synthesized v1 fields

| v1 field | Value | Rationale |
|---|---|---|
| `schema_version` | `1` | canonical |
| `knowledge_time.valid_to` | `None` | "still current, no known end" per v1 schema description |
| `claims` | `[]` | claim extraction is Phase 3, not migration |
| `claim_refs` | `[]` | same |
| `generated_by.tool` | `"wiki-migrate"` | distinguishes migrated articles from `wiki-compile` and `wiki-repo-stub` |
| `generated_by.version` | `1` | first version of the migration tool |
| `generated_by.generated_at` | `clock.now_iso8601_utc()` | migration timestamp, same value as `sources[].fetched_at` batch timestamp |
| `extensions.review.audit` | **omitted** | audit trail is empty until `review.py` appends |

## Body handling

- The markdown body after the frontmatter separator (`---`) is preserved **verbatim**, including embedded `[[wikilinks]]`, GitHub render artifacts (`([↗](slug.md))`), and inline HTML.
- The trailing-newline convention enforced by `lib/service/schema.py` (strip on dump, append on load for non-empty bodies) is applied uniformly — the migration delegates to `schema.py` rather than re-implementing the logic.
- If wikilinks point to slugs that do not exist after migration, the detection is deferred to `lint-wiki` (`dead_link` check). The migration does not validate body references.

## MigrationError enum

```python
class MigrationError(str, Enum):
    INVALID_ID = "invalid_id"                          # slug fails sanitize_id
    MISSING_REQUIRED_FIELD = "missing_required_field"  # title / source_refs / created / category / tags not present
    INVALID_DATE = "invalid_date"                      # created / updated not YYYY-MM-DD
    INVALID_TYPE_CONST = "invalid_type_const"          # type != "wiki"
    INVALID_RELATED = "invalid_related"                # related[i] stem fails sanitize_id
    SOURCE_READ_FAILURE = "source_read_failure"        # reserved — not used in lossy mode, but migrate.py may elevate missing sources to this in --strict
    EMPTY_SOURCE_REFS = "empty_source_refs"            # v0 requires minItems: 1, reject if empty
```

- `str, Enum` discriminator pattern matches the rest of the codebase (`PathValidationError`, `SchemaError`, `FileLockTimeout`).
- **Programming bugs** (e.g. `file_reader` raises an unexpected exception) are **not** mapped to `MigrationError` — they propagate as exceptions so they surface in tests. Only expected, input-driven failures become `Err`.

## Determinism and re-runnability

Running the migration twice on the same v0 article with the same clock and file contents MUST produce byte-identical v1 output. This is verified by the `test_v0_to_v1.py` round-trip test:

```python
def test_migration_is_deterministic():
    result_a = migrate(mapping, body, fake_reader, fixed_clock)
    result_b = migrate(mapping, body, fake_reader, fixed_clock)
    assert result_a == result_b
```

**Non-determinism sources that are intentionally eliminated**:
- `clock` is injected — no `datetime.now()` calls.
- `file_reader` is injected — no direct filesystem access.
- `tags` deduplication is order-preserving.
- Dict key ordering in `extensions` is fixed via explicit construction order.
- `sources[]` indices are 1-based stable.

## Down migration (v1 → v0)

Scope: `down(article: Article) -> Ok[Mapping] | Err[MigrationError]` is implemented but **only used for rollback validation** (Phase 0.11 `migrate.py --rollback` checks round-trip before touching disk). It is not a user-facing feature.

Reconstruction rules:
- `title` ← `article.title`
- `type` ← `article.extensions["legacy"]["type"]` (must equal `"wiki"`)
- `source_refs[]` ← `[s.ref for s in article.sources]`
- `created` ← `article.captured_at`
- `updated` ← `article.extensions["legacy"]["updated"]`
- `category` ← `article.extensions["legacy"]["category"]`
- `tags[]` ← `article.tags`, with `"legacy-v0"` and `"legacy-unmapped-category"` removed
- `related[]` ← `[f"concepts/{aid}.md" for aid in article.relations.related_to]`

If any `extensions.legacy` field is missing, `down` returns `Err(MigrationError.MISSING_REQUIRED_FIELD)` — a signal that the article was not originally migrated from v0 and cannot be reverted.

## Phase 0.13 application plan

Order of operations when running against the real 12 articles:

1. **backup**: `lib/service/migrations/backup.py` creates `.wiki/backups/{timestamp}/concepts/` and writes `.meta.json`.
2. **dry-run**: `migrate.py` (Phase 0.11) runs the full migration against an in-memory copy, prints a summary (12 v0 → 12 v1), reports any `Err`, exits 0.
3. **apply**: user re-runs with `--apply`; each article is atomically rewritten (tempfile + `os.replace`) one at a time. SIGINT is honored at the per-article boundary (exit 130).
4. **verify**: `graph_gen.py` + `lint-wiki.py` + `trust_score.py` + `gap_detect.py` are re-run to confirm the mixed-state behavior degrades gracefully. Lint must not crash on v1 articles.
5. **rollback path**: if anything is off, `migrate.py --rollback {timestamp}` restores from backup. The `.meta.json.tree_sha256` warning (non-blocking in dogfooding) flags any tampering.

## Non-goals for this migration

- **Enriching articles**: the migration does not add new claims, sources, or knowledge_time refinements.
- **Fixing wikilinks**: dead links survive the migration and surface in `lint-wiki`.
- **Changing the wiki_root layout**: paths stay where they are.
- **Running LLM prompts**: no compile-time LLM calls. Migration is purely mechanical.

## Open questions (tracked in plan, not blocking this spec)

- Whether `tools` should map to `reference` or get its own `tool` article_type in a future schema bump. For v1, the four types in `article-types/` are fixed: `decision / runbook / reference / concept`.
- Whether `extensions.legacy` should itself be versioned (e.g. `extensions.legacy._schema: 1`) so the down-path can evolve. Current answer: **no** — legacy is frozen by definition.
