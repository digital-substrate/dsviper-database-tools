# Changelog

## [0.2.3] - 2026-07-18

- **`DiagnosticSink`'s `dropped` count no longer depends on `max_samples`.** It was derived from
  the *bounded* sample list, so it read `0` with `max_samples=0` even when records were dropped. It
  is now tracked per finding as they arrive (a counter set when a value is elided), so the count is
  exact for any sample cap. Found by the engine ↔ `REWRITE.md` review.

## [0.2.2] - 2026-07-18

Two fixes surfaced by the engine ↔ `REWRITE.md` review.

- **`plan` was out of sync with the engine.** A same-kind element retype
  (`Set`/`Vector`/`Map`/`XArray`/`Optional`/`Tuple` `<A>→<B>`) was classified `B (review)` and
  warned "missing policy" even for a lossless **widening** — but the engine treats it as Class A.
  `plan` now calls the engine's own `_container_element_retype_class`, so the *identify* surface
  matches what the engine actually does (no more false "missing policy" for an element widening).
- **The CLI now exposes the whole decision loop.** `database_migrate.py` gained `--plan` (identify)
  and `--dry-run` (inform) — both read-only, print-and-exit; the target is optional when either is
  used. Previously only `--verify` (decide) was reachable from the command line.

## [0.2.1] - 2026-07-18

Bugfix. A field `retype_field` between two **composite** types the engine holds crashed in the
scalar-narrowing tail (and a lossless widening of one was wrongly refused). Found by an
engine↔`REWRITE.md` review.

- `Optional<A> → Optional<B>` and `Tuple<...> → Tuple<...>` now join the same-kind **element
  retype** family — widen (Class A, automatic) / narrow (Class B, policied), nil- and
  position-preserving, nested-aware — the twin of the `Set`/`Vector`/`Map`/`XArray` element retype.
- `_retype` gained a fail-closed **composite guard**: any composite retype with no conversion branch
  (`struct↔struct`, `enum↔enum`, `key↔key`, …) is now a clean `[unsupported]` refusal
  (total-or-explicit-refusal), never a crash — use a Class-C hook for such a change.
- `REWRITE.md` updated: the element-retype family (now incl. the `Optional`/`Tuple` holders), the
  composite guard, and three doc↔code precisions surfaced by the review.

## [0.2.0] - 2026-07-17

Beta. The engine and both migration loops are feature-complete and self-verifying, proven on
real data at industrial scale. This closes 0.1.0's known gaps (`CommitDatabase` migration,
`Vec`/`Mat` element conversion, Class-C hooks).

- **Engine** — the full type / directive surface: renames, shape changes, retypes (leaf, all
  container elements, `Vec`/`Mat` element + dimension, the `Vector` bridge, variant arm-sets),
  definition-level drops, namespace split / merge, and Class-C hooks (cross-field, cross-document
  single-reference, aggregate). Total-or-explicit-refusal throughout.
- **`Database` migration** — one exclusive transaction, **copy-on-reference** blob streaming (no
  orphan sweep), a source snapshot, `on_progress`, and a self-`verify`; rolls back on any failure
  and discards a partial target. Adds `dry_run` (the *inform* preview) and `plan` (the static
  *identify* report).
- **`CommitDatabase` migration** — faithful DAG replay (history + merges preserved) over all ten
  opcode verbs, now with an **opcode-level `verify`** (each opcode's rewrite + the DAG topology,
  not a re-materialised snapshot), a `dry_run`, and progress. `drop_attachment` is admissible;
  record-scoped loss (`drop-record`) is refused. The `--verify` CLI flag now covers it.
- **Docs** — a [migration guide](MIGRATION_GUIDE.md) (how to think) and [REWRITE.md](REWRITE.md)
  (maintainer, code-linked); `ARCHITECTURE.md` retired, its durable content folded into REWRITE.

Requires `dsviper >= 1.2.20`.

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
