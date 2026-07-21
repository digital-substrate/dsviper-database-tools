# SUPPORT.md — what these tools migrate, what they refuse, and why

The scope map. What a migration can express, what it stops on, and — the part that saves
arguments — *which kind* of refusal you are looking at, because they are not repaired the same
way. To *use* the tools, read the [migration guide](MIGRATION_GUIDE.md); for the directive
reference, the [README](README.md).

> 1 · One migration, two artefacts · 2 · What is supported · 3 · What is refused, and why ·
> 4 · What does not exist · 5 · Known limits.

---

## 1 · One migration, two artefacts

A schema change touches two things, and they are not the same *kind* of thing:

| | the **base** | the **`.dsm` source** |
|---|---|---|
| what it is | a machine artefact | a hand-authored artefact |
| holds | your data | the schema *plus* comments, file split, ordering, alignment |
| migrated by | `database_migrate.py` — **rebuild** | `definitions_migrate.py` — **patch** |
| the original | kept as rollback | never modified; a fresh tree is written |

Both run the **same** `transformation.py`, through the same engine. One edit script, two
executors — the two artefacts cannot drift apart, because nothing is written twice.

### Why the source is patched rather than re-rendered

The obvious idea is to skip the second tool: the engine already builds the target `Definitions`,
and DSM governance guarantees `Definitions ⇒ DSMDefinitions ⇒ DSM` is total — every target *is*
expressible as DSM. So why not render it back out?

Because that arrow is total, not faithful. Going the other way, `text ⇒ Definitions` is a
**projection**: it keeps the schema and drops everything else. Rendering is its right inverse
(re-parse the render and you get the same `Definitions` — which is exactly why we use it as the
verification oracle), never its left inverse: it cannot restore what the projection dropped,
because that information is no longer there when the renderer runs.

Measured, on eighteen hand-written lines through `DSMDefinitions.from_definitions(defs).to_dsm()`:

| written by hand | after the round trip |
|---|---|
| `// the product catalogue — hand-maintained since 2019` | gone |
| `// DO NOT reorder: the order follows the business flow` | gone |
| `key<Customer> buyer;      // who ordered` | gone |
| `tools.dsm` — a whole function pool | **gone** (a pool is not in `Definitions`) |
| two files | one |
| `float rate = 0.5` | `float rate = 0.500000` |
| four-space indent, aligned columns | two-space indent, alignment lost |

Only the docstring survived — it *is* part of the model.

Redistributing a render back into the original files would recover the split and the ordering,
and still lose the comments, the alignment, the literal spelling and the pools. It also fails a
plainer test: a codemod produces a diff proportional to **the change**, a re-render one
proportional to **the file** — a renamed field would touch every line of the tree, and the
schema change would stop being reviewable.

**The short answer, when someone asks:** the `.dsm` holds more than the schema it declares.

---

## 2 · What is supported

One directive vocabulary, applied to both artefacts. Everything below migrates the data **and**
patches the source.

| | directives |
|---|---|
| **rename** | `rename_type` · `rename_field` · `rename_case` · `rename_attachment` |
| **field shape** | `retype_field` · `add_field` (a default, or `derive=` a hook) · `drop_field` · `reorder_fields` |
| **dimensions** | `resize_vec_field` · `resize_mat_field` · `transpose_mat_field` |
| **cases** | `add_case` · `remove_case` · `reorder_cases` |
| **namespaces** | `rename_namespace` (display name) · `remap_namespace` (uuid) · `move_type` · `move_attachment` |
| **definition drops** | `drop_type` · `drop_attachment` (+ their acknowledgements) |
| **documentation** | `document_type` · `document_field` · `document_case` · `document_attachment` |
| **hooks (Class C)** | `transform_field` (sees the struct — cross-field derivation) · `transform_type` (every occurrence of a type, nested included) |

Both persistences are supported — `Database` and `CommitDatabase` (a faithful DAG replay, history
and merges preserved) — each with the same `migrate` / `verify` / `dry_run` / `run` surface.

A note on **function pools**: they declare no storage, so they are outside the persistence
`Definitions` and carry no directive of their own. Their signatures reference types, and those
references follow every rename, namespace change and move — in both pool kinds, `function_pool`
and `attachment_function_pool`.

---

## 3 · What is refused, and why

Four different reasons. The kind matters: one is a knob you turn, one is a decision you work
*with*, one is a fact about your storage, one is a mistake to fix.

### a) It would lose data, and you have not said what to do — *a knob*

Narrowing a number, `string → X`, `Optional<A> → A`, removing a populated case, collapsing a
`Set`/`Map`, `Vector → Vec`. Refused **by default**; you open it with an explicit policy —
`"fail"`, `"saturate"`, `("default", v)`, `("map-case", n)`, `("fit", pad)`, `"drop-record"` —
which is consulted **only for the offending values**. Anything in range converts exactly.

Checked before any data is read, so you learn about it in a second, not halfway through a
multi-gigabyte base.

### b) The target shape is ambiguous — *decided, and deliberately closed*

A `Vec`↔`Mat` reshape, a flat→`Mat` un-flatten, `Vector → Mat`, a `variant`↔non-variant retype.
The target type alone cannot say whether you meant a resize, a transpose, or a (layout-ambiguous)
reshape — and guessing would be choosing on your behalf.

The way through is to **say which one**: that is exactly why `resize_vec_field`,
`resize_mat_field` and `transpose_mat_field` exist as named directives. For anything else, a
Class-C hook (`transform_field` / `transform_type`) does what you mean, explicitly.

### c) The meaning depends on how documents are stored — *a fact, not a gap*

`drop-record` deletes a whole record. A `Database` can express that; a `CommitDatabase` stores a
*trace of mutations* with no single record to elide, so it is refused there. Even on a `Database`
it carries two gates: it is refused where "the record" is ambiguous (a policy biting inside a
`Vector`/`Set`/`Map`/`XArray`), and it requires `accept_document_drops()` — deleting documents
should be a signed act, not a side effect of a field policy. `drop_attachment` is likewise gated
by `accept_attachment_drops()`.

This is the one place the two persistences legitimately differ.

### d) A directive names something that is not there — *a mistake, reported early*

All five are checked **before anything is built or written**, and each accumulates *every* site
into one report rather than stopping at the first:

| report | what it caught |
|---|---|
| `[unknown-target]` | a directive names a type, field, case, attachment or namespace the source does not hold — a typo would otherwise do nothing, silently |
| `[dropped-type-referenced]` | a `drop_type` leaves a surviving definition pointing at the removed type |
| `[dropped-type-in-pool]` | … or a function-pool signature naming it |
| `[namespace-collision]` | a move or merge lands two definitions in one `NS::Name` slot |
| *ambiguous type* | a rename makes a bare reference resolve to two candidates — caught by the parser at the verify re-parse, sited and with the candidates listed |

---

## 4 · What does not exist

Not refusals — capabilities the vocabulary does not have, and why.

- **Creating a type.** A migration is directed by the *source* schema. A type born from nothing
  has no data to carry, and no file to live in on the source side. (The one design question still
  open.)
- **Renaming or documenting a function pool.** No directive, and the parser reports no span for a
  pool declaration. The *types inside* its signatures follow every migration, which is what
  matters for the tree to keep resolving.
- **Reshaping a `Vec`/`Mat`, retyping a variant arm.** Ambiguous targets (§3b) — use a hook.
- **Composing migrations** (A→B then B→C) and **aggregating over a `CommitDatabase`'s history.**
  Genuine frontiers, not missing API: chaining re-opens a data-exchange ambiguity, and a fold over
  a growing collection has no single value across a DAG's commits.

---

## 5 · Known limits

Small, deliberate, and none of them silent:

- two `move_type`s into a namespace that does not yet exist emit two adjacent `namespace Y { … };`
  blocks — valid DSM (re-opening a namespace is legal), and cheaper than tracking freshly created
  blocks;
- `definitions_migrate --force` writes into a non-empty output directory without clearing it, so
  files from an earlier run survive alongside the new ones;
- a declaration cut out of its namespace can leave an empty `namespace N { };` block behind — the
  codemod removes what it was asked to remove and does not tidy the neighbourhood.

Anything that would be *wrong* rather than untidy is refused (§3) — the project's rule is
total-or-explicit-refusal: every value becomes a valid target value, or the migration stops and
tells you where.
