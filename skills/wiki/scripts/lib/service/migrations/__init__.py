"""Migration subsystem — schema-version transitions for wiki articles.

The public surface of this package is:

* :class:`lib.service.migrations.base.Migration` — the Protocol that every
  concrete migration implements.
* :class:`lib.service.migrations.base.MigrationError` — discriminated error
  enum for expected input-driven failures.
* :func:`lib.service.migrations.registry.register_migration` — decorator that
  attaches a concrete migration to the in-process registry.
* :class:`lib.service.migrations.backup.BackupManager` — creates and verifies
  on-disk backups before destructive migration runs.
* :class:`lib.service.migrations.v0_to_v1.V0ToV1Migration` — the first
  concrete migration, implementing ``.wiki/schema/migrations/v0-to-v1.md``.

The Phase 0.11 CLI handler ``skills/wiki/scripts/migrate.py`` composes these
pieces — it contains no business logic of its own. Part of the
Source-Agnostic Knowledge Pipeline (currently standby; v0 is schema-of-record).
"""
