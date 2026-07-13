# Architecture — the rewriting algorithm

This document is the design reference for how `DefinitionsTransformer` turns a value
stored under one schema into the *same value* under an evolved schema — the engine
behind `migrate_database`. `README.md` describes the tool and how to run it; **this**
document describes the algorithm and the guarantees it upholds. It is self-contained:
everything needed to understand, extend, or trust the transformation is here.

> **The engine's law.** The rewrite walks the **target** type, not the source value.
> One target-directed pass expresses both families of schema change — a rename is
> just the case where the target shape equals the source shape — and target ids are
> baked in by *choosing target types*, never by editing ids inside a value.

## 1. Why a rebuild, not an `ALTER`

A Viper type's `runtimeId` is a content fingerprint of its **definition**, and that
same id is the type's storage key. Change the definition — rename a field, widen an
int, drop a case — and the id changes, so the stored bytes no longer key to it. There
is therefore no in-place `ALTER`: migration is a **rebuild**. We read the old
database read-only and write a fresh one, transforming every value on the way.

Precisely, that fingerprint is `hash(namespace UUID, type name, field names + types,
case names, …)`. Two consequences matter for the directives: a **type / field / case
name is part of the identity**, so renaming one genuinely re-ids its type; but a
**namespace is identified by its UUID** — its display name never enters a `runtimeId`.
A namespace therefore has *two orthogonal* directives: `rename_namespace` changes its
display **name** (new `Namespace::Type` representations, unchanged ids), while
`remap_namespace` changes its identity **UUID** (new `runtimeId`s, unchanged
representations). The other renames have no such split — for a type, field, or case
the name *is* the identity, so a rename is always a re-id.

"Transform a value" means: given a value `v` valid under the *source* definitions,
produce the corresponding value valid under the *target* definitions. That is the
whole job of `rewrite.py`.

## 2. Two phases

The transformer runs in two phases, and the split is the key to the whole design.

```
directives ──▶ build_target_definitions ──▶ target Definitions + id map (source→target)
                                                    │
value (source domain) ──────────────────────────────┴──▶ value() ──▶ value (target domain)
```

1. **`build_target_definitions` — definitions ⇒ definitions.** Construct the target
   `Definitions` from the source plus the declarative edit script
   (`TransformationDirectives`). New ids are *born* here, by construction — never
   forged, never patched onto an old value.
2. **`value` — value ⇒ value.** Rewrite each stored value into the target domain,
   using the id map phase 1 produced.

Keeping id-creation (phase 1) separate from value-rewriting (phase 2) is what lets
the engine bake target ids in simply by *choosing target types*, rather than editing
ids inside values.

## 3. Phase 1 — building the target definitions

`build_target_definitions` walks the source's concepts, clubs, enumerations,
structures and attachments, applies the directives, and emits the target registry
**in dependency order**:

- **Concepts** are created parent-before-child (a topological pass over the parent
  relation).
- **Structures** are created field-dependency-first: a struct is emitted only once
  every named type its fields reference already exists in the target. DSM forbids
  value-recursion, so this type graph is a DAG and the pass always terminates.
- As each target type is created, the map `source runtimeId → target Type` gets one
  more entry. That map **is** the bridge phase 2 uses.

Because the target is *built from* the directives, it is consistent by construction:
there is no separate "does the target match the directives?" check on this path.

## 4. Phase 2 — the target-directed value engine

`value(v, target_type)` is the heart. Its defining choice: **the walk is driven by
the target type, not the source value.** For a structure it iterates the *target*
fields and, for each, asks "where does this come from?":

```
for each field F in the TARGET struct:
    sn = source field that maps to F        (via the rename map)
    if sn is None            -> F was ADDED     -> seed F's default
    elif sn was retyped      -> convert v[sn] to F's type, under the decreed policy
    else                     -> recurse: value(v[sn], F's type)
```

This single loop expresses **both** kinds of schema change:

- **Family 1 — renames** (size-preserving). Nothing is added, dropped, or retyped, so
  every target field takes the `recurse` branch: the value is re-stamped onto the
  renamed types and ids follow. A rename is the degenerate case where the target shape
  equals the source shape.
- **Family 2 — shape changes** (add / drop / reorder / retype). A dropped source field
  never appears in the target walk; an added target field has no source and takes its
  seeded default; a retyped field goes through the converter. Reordering is free — we
  build from the target field order.

Containers recurse element-wise into their *mapped* element type (`Optional`,
`Vector`, `Set`, `Map`, `Tuple`, `Variant`, `XArray`). An unhandled composite type is
**refused**, never passed through verbatim — passing it through would keep stale
source ids, i.e. silent corruption.

## 5. Leaves that need care

- **Enumerations** are re-encoded **by name** against the target. The wire format
  stores a case by its *index*, so a naive byte copy would silently corrupt on a case
  reorder; rebuilding by name is immune.
- **Keys.** A `ValueKey` carries a flavour — `Key<concept>`, `Key<club>`, or
  `Key<any-concept>`. The rewrite rebuilds the key on the mapped concept with the
  original `instanceId`, then retypes it to the mapped `Key<X>` so the flavour
  survives (a plain rebuild would silently downgrade club / any-concept keys).
- **`Any`** recurses into the value it wraps — the wrapped value is self-describing.
- **`XArray`** is rebuilt atomically: its layout (element positions and tombstones) is
  copied opaquely while the elements — re-mapped to the target domain — are installed
  in the same step, so the result never passes through a partial state.
- **`blob_id` / `commit_id`** are content-addressed leaves. A `blob_id` is a hash of
  bytes, carried verbatim (the migration loop copies the bytes). A `commit_id` is a
  hash of content; inside a DAG migration it must be remapped, but for a plain
  `Database` it is a stable leaf.

## 6. Class A vs Class B — the loss model

Every leaf/structural conversion is one of two classes:

- **Class A — automatic**, because it is total and lossless: integer *widening*,
  `X → string`, adding a field with a default, dropping a field, reordering, adding a
  case, `Vector → Set`. No policy required.
- **Class B — policied**, because it *can* lose information: numeric *narrowing*,
  `string → X` parsing, unwrapping `Optional<A> → A`, removing a populated case, `Set`
  element collapse, `Map` key collision, `drop-record`.

The rule for Class B is: **an in-range / parseable / non-nil value always converts
exactly; the policy governs only the offenders.** Narrowing `int64 → int32` copies
`100` verbatim and consults the policy only for a value that overflows. Policies are
`"fail"` (default), `"saturate"`, `("default", value)`, `("map-case", name)`, or
`"drop-record"`. `drop-record` raises `DropRecord`, which the migration loop catches
to skip that whole document.

## 7. Guarantees, and where they are enforced

The engine is **total-or-explicit-refusal**: every value either becomes a valid target
value, raises `DropRecord` (a *decreed* skip), or raises — never a silent wrong value.
Four checks, all *before any data is touched*, uphold this:

- **P1 — name completeness.** Every source named type must have a target (itself, or a
  `rename_type` destination). A missing target is refused at construction.
- **P2 — shape invariance** (rename-only transformers). When you supply a hand-built
  target instead of directives, a family-1 transformer requires each matched type to be
  identical up to renames — same field/case count and order. A shape difference means
  it is really a family-2 change, and is refused rather than silently mis-aligned.
- **Policy completeness.** Every lossy retype and every populated-case removal must
  carry a decreed policy. An un-policied lossy op is refused at construction — you
  cannot start a migration that would later lose data with no instruction on how.
- **Domain-free defaults.** An `add_field` default is authored in the *source* domain
  but lives in a *target* field, so it is only expressible if it references no named
  (migrated) type — a primitive leaf or a container of such. A composite default that
  embeds a struct / enum / key / `Any` would carry stale source ids, and is refused.

Unsupported-but-safe operations fail *closed and early* with a clear message rather
than corrupting: a `Vec`/`Mat` element retype, for instance, is refused at construction
(carry it verbatim, or model the change as a new field).

## 8. A worked trace

Source `Shop::Order { amountCents: int32, label: string, qty: int64, note: string }`,
with directives (`retype_field` and `rename_field` are keyed by the **source** name):

```python
d.retype_field("Shop::Order", "amountCents", Type.INT64)             # widen — Class A
d.rename_field("Shop::Order", "label", "title")                     # rename — family 1
d.retype_field("Shop::Order", "qty", Type.INT16, policy="saturate")  # narrow — Class B
d.add_field("Shop::Order", "currency", ValueString("EUR"))          # add
d.drop_field("Shop::Order", "note")                                 # drop
```

Phase 1 builds `Shop::Order { amountCents: int64, title: string, qty: int16, currency:
string }` and maps the old struct id to the new one. Phase 2, rewriting `{amountCents:
500, label: "shoes", qty: 40000, note: "x"}`, walks the *target* fields:

- `amountCents` ← retyped `int32 → int64` (Class A widen) → `500`
- `title` ← source `label` (renamed), same type → recurse → `"shoes"`
- `qty` ← retyped `int64 → int16` (Class B); `40000` overflows `int16`, `saturate` →
  `32767`
- `currency` ← added → its default `"EUR"`
- `note` — dropped, never visited

Result: `{amountCents: 500, title: "shoes", qty: 32767, currency: "EUR"}`, valid under
the target schema.

## 9. Design decisions (locked)

- **Target-directed, one engine for both families.** The walk follows the target type;
  a rename is the degenerate shape-preserving case. No separate rename vs. reshape code
  path.
- **Id-creation is a phase, not an edit.** Target ids are minted in phase 1 by
  construction; phase 2 bakes them in by choosing target types. Ids are never forged or
  patched inside a value.
- **No silent loss.** Every lossy op is refused by default and must carry a decreed
  policy, checked before any data is touched. In-domain values always convert exactly.
- **Total-or-explicit-refusal.** An unhandled composite, an un-policied lossy op, an
  unexpressible default, an unsupported retype — all fail closed and early, never pass
  through as a wrong value.
- **Pure Python over the binding.** The engine is a composition of already-bound
  runtime atoms; the `dsviper` runtime is untouched.

## 10. Status, and where the loop fits

`value()` rewrites one value. `migrate_database` (in `migrate.py`) drives it over a
whole database: it streams blobs first (their content-addressed ids are preserved),
transforms every document, then mark-sweeps any blob the schema change stranded.
`verify_migration` re-derives the expected target from the source through the same
transformer and asserts they match — the tool checks its own correctness.

`migrate_commit_database` (in `commit_migrate.py`) drives a **`CommitDatabase`** rebuild
by faithful structural replay: every commit is re-issued in topological order so history
is preserved (merges included, since a merge only seeds the DAG linearization), the
opcode verbs are translated through the same value engine, and the intra-DAG `commit_id`
references are remapped to the target commits — all in one atomic transaction.

The rewrite engine covers the full type and directive surface (all containers,
`Vec`/`Mat`, `XArray`, the three key flavours); the `Database` migration loop copies
blob bytes and reclaims orphans, and verifies its own result; the `CommitDatabase` loop
replays the whole commit DAG faithfully. Not yet built: `Vec`/`Mat` element widening,
custom cross-field (Class-C) derivations, and a round-trip verifier for a
`CommitDatabase` (`verify` is `Database`-only).
