# Changelog

## [0.1.0] - 2026-07-13

First cut. Definitions-directed document rewriting + database migration in pure
Python over the `dsviper` binding (no C++).

- `TransformationDirectives` — declarative edit script: renames (type / field / case /
  attachment), the two orthogonal namespace axes (`rename_namespace` = display name →
  new representations; `remap_namespace` = identity UUID → new `runtimeId`s), shape
  changes (field add / drop / reorder / retype, case add / reorder / remove), and
  Class-B policies (narrowing, parse, unwrap, `Vector→Set`, remove-case, `Map`/`Set`
  collision).
- `DefinitionsTransformer.from_directives` — builds the target `Definitions` in
  dependency order and rewrites values with one target-directed engine over both
  families. Full type coverage: all containers, `Vec`/`Mat` (verbatim), `XArray`
  (via the `ValueXArray.rebuild_from` binding), the three key flavours. Guards: (P2)
  shape-invariance for renames, policy-completeness for lossy ops, domain-free
  `add_field` defaults — all refuse before touching data.
- `migrate_database` / `run_migration` — the read-old / write-new loop: streams blob
  bytes (content-addressed ids preserved), transforms documents, then mark-sweeps any
  blob the schema change stranded. `run_migration(verify=True)` self-checks.
- `verify_migration` — re-derives the expected target from the source through the same
  transformer and asserts a faithful image (values, dropped records, no dangling blob).
- `run_migration(source, build_directives, target, verify=…)` — the read-old /
  write-new entry point.
- `migrate_commit_database` / `run_commit_migration` — a `CommitDatabase` rebuilt by
  faithful structural replay: every commit re-issued in topological order (history
  preserved, merges included), the 10 opcode verbs translated through the engine, one
  atomic exclusive transaction. Blob byte-copy is streamed and shared with the
  `Database` loop.
- `database_migrate.py` — root command-line tool over both: loads a migration file,
  dispatches on the source type, writes a fresh target (`python3 database_migrate.py
  <migration> <source> <target> --verify`). Same flat, run-from-the-repo shape as
  `dsviper-tools`.

Requires `dsviper >= 1.2.20` (for `ValueXArray.rebuild_from`).

Known gaps: `CommitDatabase` (DAG) migration; `Vec`/`Mat` element widening; Class-C
cross-field hooks. See `ARCHITECTURE.md` for the algorithm.
