# REWRITE.md — the rewrite engine, for maintainers

The developer/maintainer companion to the code. It explains *why* the engine is shaped the way
it is, names the invariants, and links each idea to where it lives — by **file : symbol** (line
numbers drift, names don't). To *use* the tool, read the [migration guide](MIGRATION_GUIDE.md);
for the terse directive reference, the [README](README.md).

> 1 · The principle · 2 · Module map · 3 · The chain of proof · 4 · Invariants · 5 · The engine ·
> 6 · The store loops · 7 · Extension points · 8 · The Viper boundary · Appendix — a worked trace.

---

## 1 · The principle — rewriting directed by the definitions

The whole engine is one idea, and it is not a hard one — only an unfamiliar one. Two habits make
it feel strange at first; naming them is half the work.

Most people migrate data **imperatively** ("open the row, update these columns") or
**source-directed** ("walk the old document, decide what to do with each field"). This engine does
neither. It is **directed by the definitions**: the transformation is a function of the source and
target *schemas*, not of the data — and it is realised by walking the **target**.

That is the flip. You do not walk what you *have* (the source value); you walk what you must
**produce** (the target type), and for each target position you ask *"where does this come from?"*.

It runs in **two phases**, and the split is the whole design:

**Phase 1 — definitions ⇒ definitions.** `build_target_definitions(source_defs, directives)`
(`rewrite/engine.py`) builds the *target* `Definitions` from the source plus the directives, in
dependency order. Every target type's `runtimeId` is **born** here, by construction, from the
target definition itself — never forged, never patched onto a value. It hands back a map
`source runtimeId → target Type` (kept as `type_map` on the rewriter). Ids are minted once, in one
place, before any value is touched.

**Phase 2 — value ⇒ value, target-directed.** `DefinitionsRewriter.value(v, target_type)`
(`rewrite/engine.py`) produces the target value by following the shape of the *target* type. At a
target struct it visits each target field and asks where it comes from: **added** → seed its
default; **retyped** → convert under its policy (`_retype`); **otherwise** → recurse through the
rename map. One loop, and it expresses *both* families at once — a rename is just the degenerate
case where the target shape equals the source shape.

`DefinitionsRewriter.from_directives(source_defs, directives)` wires the two: it runs phase 1 and
returns `(rewriter, target_defs)` — the rewriter ready for phase 2, the target definitions ready
to extend a fresh store.

### Why walk the target

This is the crux, and it is what makes the rest possible. The thing you must produce *completely
and validly* is the target. Walking the target guarantees **every target position is visited and
must be satisfied** — filled, or refused *at the position that cannot be filled*. So the result is
**total by construction**: nothing is silently left out or set wrong. A source-directed walk gives
no such guarantee — you can finish it with target fields still uninitialised. Target-directed is
precisely what makes `total-or-explicit-refusal` (§4) *enforceable* rather than aspirational.

Be precise about what this does **not** buy, though: totality is not intent. An *undeclared*
rename still reads as a drop + add (the source field's data lost to the new field's default) — the
walk fills the target validly, it just fills it from the wrong place. That specific trap is caught
elsewhere — by the static plan's "possible forgotten rename" warning (`rewrite/plan.py`) — not by
target-directedness.

> The core stops here, and it really is this simple. Where it stops being simple — cross-field
> derivation (`C = f(A, B)`), migration composition, aggregates over a history — is a genuine
> frontier, not merely an unfamiliar one. A later section marks it honestly (the extension points,
> and the traps imported from prior art), so the edges are respected, not underestimated.

---

## 2 · Module map

The code has two layers, and the boundary is load-bearing: a **pure kernel** that knows only
`Value`s and `Definitions`, and the **consumers** that feed and drain it (a store, and the CLI).
Correctness lives in the kernel; the consumers add I/O, transactions, and scale. Nothing in
`rewrite/` opens a file.

### The kernel — `rewrite/` (no I/O, no store, no format)

| File | Role | Entry symbols |
|---|---|---|
| `rewrite/engine.py` | the two phases (§1): build the target definitions, rewrite a value target-directed | `build_target_definitions` · `DefinitionsRewriter.value` / `.from_directives` · `Unrepresentable` |
| `rewrite/directives.py` | the declarative edit script — pure data, the input contract | `TransformationDirectives` |
| `rewrite/plan.py` | the **static** plan report (*identify*): classify a migration from directives + schema alone, no data | `plan` / `format_plan` |
| `rewrite/report.py` | the **dynamic** diagnostic report (*inform*): what each Class-B policy actually did on real data | `DiagnosticSink` / `format_report` |

`rewrite/__init__.py` re-exports these; a consumer pulls them in with `from .rewrite import …`.

### The consumers — the store loops and the CLI

| File | Role | Entry symbols |
|---|---|---|
| `migrate_database.py` | the `Database` loop, over materialised documents | `migrate` · `verify` / `VerificationError` · `dry_run` · `run` · `MigrationProgress` |
| `migrate_commit_database.py` | the `CommitDatabase` loop — a faithful DAG replay of opcodes | `migrate` · `verify` · `dry_run` · `run` · `CommitMigrationProgress` |
| `blobs.py` | shared blob byte-copy — streamed in chunks, content-addressed id preserved | `copy_blob` |
| `database_migrate.py` *(repo root)* | the CLI — load a migration file, dispatch on the source kind, write a fresh target | `main` |

Both loops expose the **same surface** — `migrate` / `verify` / `dry_run` / `run` — because they
are one idea over two persistences (§6). The only code a *user* writes is a migration file's
`build_directives(source_defs) -> TransformationDirectives`; everything else is the tool.

> **Read order for a newcomer:** `directives.py` (what you can say) → `engine.py`
> (`from_directives`, then `value`) → one loop (`migrate_database.py` is the simpler) → its
> `verify`. Everything else is variation on those four.

---

## 3 · The chain of proof

The three modules are not three separate things to trust. They are **one theorem, proved once and
carried up by two reductions.** Read that way, what each `verify` must check — and, just as
important, what it must *not* — falls out.

### The reduction

**The engine is the atom.** `value()` (`rewrite/engine.py`) is `Value → Value`: it turns a source
value into a valid target value, or refuses. It has no I/O and no scale, so its whole job is
*model correctness* — total-or-explicit-refusal (§4). Everything above rests on it; a crack here is
the most expensive kind, because both loops inherit it.

**The Database loop is the atom in a loop over documents.** `migrate_database.migrate` is, in
essence,

```
for each (attachment, key):   target.set(att, key, value(source.get(att, key)))
```

It adds *the loop* — one transaction, copy-on-reference blobs, a source snapshot — and nothing
about the model. So its correctness **reduces** to "the engine is correct" + "the loop applies it
faithfully to every document." `verify` therefore proves exactly what the loop *adds*: every kept
document equals `value(source_doc)`, no spurious document survives, and the target holds exactly
the referenced blobs.

**The CommitDatabase loop is the same atom in a loop over opcodes.** A `CommitDatabase` stores not
documents but the *mutations* that produce them — opcodes in a DAG. And **an opcode is a little
document**: `(attachment, key, semantics, path, value)`. Rewriting one is the same engine call on
its operand, plus a path translation and a key/attachment remap — indeed `AttachmentMutating.set`
*is* `Database.set`, so a `Document_Set` opcode is literally the Database loop's unit.
`migrate_commit_database.migrate` re-issues every opcode in topological order (`CommitData.sort`),
threading an old→new commit-id map. So its correctness **reduces** to "the engine is correct" +
"each opcode's path is translated" + "the DAG topology is preserved." `verify` proves precisely
that — opcode by opcode, plus the parent / target commit links (`_link_preserved`).

### What each verifier must *not* do

The reduction cuts both ways: a verifier proves what its layer **adds**, and *trusts* the layers
below. The CommitDatabase verifier is where getting this wrong is tempting — and where we did, at
first.

A `CommitState` is **not** a reliable oracle. It is a *blind, best-effort* reconstruction — the
runtime replays the opcode trace under last-write-wins, swallowing errors, to materialise the
documents. So comparing materialised `CommitState`s (the first, wrong, design) checks against
something the runtime itself does not treat as authoritative — and it *over-*claims: it fails on a
faithful migration whenever a non-local hook's input varies across the history (the derived value is
frozen into its opcode at write time, not re-derived at every later snapshot). The fix was to trust
the runtime's materialisation and check **our** artefact instead — **the opcode trace**. That is the
doctrine — *verify the opcodes, not the state* — and it is why the CommitDatabase `verify` is
opcode-level.

> **The rule, stated once.** Each layer's `verify` proves what that layer *adds* and no more: the
> engine's model, the Database loop's document application, the CommitDatabase loop's opcode rewrite
> + topology. It trusts the engine (its own tests) and it trusts the runtime (its materialisation).
> A verifier that re-checks a layer it should trust either duplicates work or — as here — over-claims
> and false-fails.

---

## 4 · Invariants

The properties the code holds true. The tests guard them, but the invariant is the thing to keep in
your head when you extend the code — a change that reads fine yet quietly breaks one is the
dangerous kind. Each is named and pinned to where it is enforced.

**1 · Total, or an explicit refusal.** Every value becomes a valid target value, raises
`Unrepresentable` (a record the consumer elides), or raises — never a silently wrong value.
Enforced in two places in `rewrite/engine.py`: `DefinitionsRewriter._retype` / `.value` fail closed
on any un-handled or un-policied case (an un-policied narrowing raises; an unhandled composite hits
a guard instead of passing through unrewritten), and `._policy_completeness` refuses a lossy
operation carrying no policy *up front*, before any data is touched. When you add an operation, its
default must be refusal.

**2 · Ids are born in phase 1, never patched.** Every target `runtimeId` is minted by
`build_target_definitions` (`rewrite/engine.py`), by constructing the target definition. Phase 2
(`value`) only *looks up* the target type through `type_map`; it never forges or edits an id. That
is why a value can never carry a stale source id into the target — there is nowhere to patch one.

**3 · A policy governs only the offenders.** An in-range number, a parseable string, a non-nil
optional always converts *exactly*; a policy (`saturate` / `default` / …) is consulted only for the
values with no faithful target. In `_retype` this is literally `if lo <= v <= hi: exact` *else*
policy — and the diagnostic sink (`rewrite/report.py`) is notified only on the *else*. A policy that
altered an in-range value would be a bug.

**4 · An opcode is a little document.** In `migrate_commit_database.py`, an opcode
`(attachment, key, semantics, path, value)` is rewritten by the same engine call on its operand
plus a path/key/attachment remap — `AttachmentMutating.set` *is* `Database.set`. Keep it true: a
new opcode verb is handled by rewriting its operand through `value` / `_retype`, never by a bespoke
conversion.

**5 · A verifier proves what its layer adds — no more.** (§3.) The Database `verify` checks
document application; the CommitDatabase `verify` checks the opcode rewrite + topology
(`_link_preserved`) and does **not** re-check the runtime's materialisation. Reach for a
materialised `CommitState` inside a verifier and you are re-checking a layer you should trust — it
will over-claim.

**6 · Admissibility is a property of the definitions, not the storage.** What a migration is
*allowed* to do is decided from the definitions, so it is the same for a `Database` and a
`CommitDatabase` — the two differ only in how documents are materialised. The one exception is a
loss whose meaning genuinely depends on materialisation: `drop-record` deletes a *record*, which a
`CommitDatabase` (a trace of mutations, no single record) cannot express, so it is refused there
(up front, and again at runtime if a hook raises `Unrepresentable`). `drop_attachment` is static
and uniform — a whole partition — so it is admissible on both, under the *same shared gate*
(`_refuse_unacknowledged_attachment_drops`, imported by the CommitDatabase loop; its opcodes skipped
by `_addresses_dropped_attachment`). Rule of thumb: if a new operation's meaning does not depend on
*how* documents are stored, it belongs to both loops identically.

**7 · `Value` equality is canonical.** `verify` compares values with `==`; this is sound only
because Viper's `Value` equality is structural and its encoding canonical (equal values encode
byte-identically; containers are order-independent). That is the runtime's guarantee, not ours —
but every self-check rests on it.

---

## 5 · The engine

The two phases of §1, in detail. Both live in `rewrite/engine.py`.

### Phase 1 — `build_target_definitions`

It constructs the target `Definitions` in **dependency order** and returns `(target, tmap,
att_map)`. Two inner functions carry the work: `map_t(source_type)` maps any source type to its
target — a named type via `tmap`, a container by mapping its element, a `map` / `tuple` / `variant`
component-wise, a `transform_type`'d type to its `new_type` — and `ready(t)` is true when every
named type `t` depends on is already in `tmap`. The loop builds each definition once it is `ready`,
so a struct is created only after the types its fields reference exist. `tmap`
(`source runtimeId → target Type` — the map §1 calls `type_map` once it is kept on the rewriter) is
the id map, **minted by construction** — this is invariant #2: ids are born here.

Refusals happen up front, before any value:
- an `add_field` static default must be **domain-free** (`_default_domain_free`) — a primitive leaf
  or a container of such, never a named type, because a composite default would carry stale source
  ids into the target;
- dimension ops are validated (`_validate_dimension_ops`);
- a `drop_type` that leaves a surviving definition pointing at the removed type is refused, with
  **every** dangling site accumulated into one report (a type-walk over the built refs), not a
  misleading topological "cycle" or a first-error abort.

Namespaces have three axes here (`tgt_ns`): the **name** (display), the **uuid** (identity), and a
**per-definition move** (`type_namespaces`) that overrides both, for split / merge.

### Phase 2 — `value`, the target-directed walk

`value(v, tt)` dispatches on the **target** `type_code`. A `transform_type`'d source type is
intercepted at the top and handed to its hook (it rides the recursion, reaching nested
occurrences). The **struct branch is the heart**: `_field_source` pairs each *target* field with its
source, and per field the walk decides —

| target field is… | source | how |
|---|---|---|
| added, static | — | seed the default (`payload`, else the field's born default) |
| added, `derive=` | the struct | `_apply_field_hook` (Class C — sees siblings) |
| `transform_field`'d | the struct | `_apply_field_hook` (Class C) |
| `resize` / `transpose` | the field | `_resize` / `_transpose` |
| `retype`'d | the field | `_retype` (Class A / B) |
| kept or renamed | the field | recurse `value` |

A field-level hook always wins over the global `transform_type` (resolution: field > type). Every
other target `type_code` — optional, vector, set, map, tuple, variant, xarray, any, enum, key,
commit_id, primitive — has its own branch; the container branches recurse element-wise, with
`_set_add` / `_map_set` guarding a set-collapse / map-collision.

`_retype` is the sibling of `value` for a *changed* type: the **structural** conversions (unwrap an
`Optional`, the `Set`↔`Vector` / `Vector`↔`XArray` / `Vec`↔`Vector` bridges, a **container element
retype**, a variant arm-set change) and the **leaf** conversions (widen, `→string`, parse, narrow,
`float→int`). A Class-B branch consults its policy only on the offender (invariant #3).

### The leaves that need care

Most branches are a plain recurse. These are the ones where a naïve "just copy it" is wrong:

- **enum — re-encode by *name*.** The wire format stores a case by its declared **index**, so a
  byte-copy corrupts the moment cases are reordered. The enum branch rebuilds
  `ValueEnumeration(tt, name)` **by name**, applying a case rename or a removed-case policy
  (`map-case` / `drop-record` / fail). Never turn this into a copy.
- **key — preserve the flavour.** A `Key<X>` is concept / club / any-concept. `ValueKey.create`
  alone always yields a *concept* key, silently downgrading the rest — so the key branch rebuilds on
  the mapped concept + the stable `instanceId`, then `to_key(target)` restores the flavour.
- **any — follow the content.** `Any` wraps a dynamic value; the branch rewrites the wrapped value
  (`value(unwrap, None)`), so a schema change *inside* an `Any` is followed, not frozen.
- **xarray — rebuild atomically.** Positions and tombstones are structural. The branch uses
  `rebuild_from`, so the layout copy and the re-mapped elements land in one step — never a half-built
  `XArray`, positions and tombstones preserved.
- **blob_id — verbatim; commit_id — remapped only in a DAG replay.** A `blob_id` is
  content-addressed and stable across stores, so it passes through. A `commit_id` is remapped to its
  re-issued target **only** when `_commit_id_remap` is installed (the CommitDatabase replay wires
  it); a Database migration, or an external cross-base id, keeps it verbatim.
- **container element retype — per element, policied, nested.** `Set` / `Vector` / `Map` / `XArray`
  `<A> → <B>` runs each element through `_retype_element` (the policy-governed leaf path), guarding a
  post-narrow set-collapse / map-collision; `_container_element_retype_class` classifies it (widen A
  / narrow B), and it recurses for nested containers.

---

## 6 · The store loops

Both loops are the engine (§3) driven over a real store, in one exclusive transaction that rolls
back on any failure. They differ only in the unit they iterate — a document, or an opcode.

### The `Database` loop — `migrate_database.py`

The core is `_transform_pass(source, rewriter, sink, diag=, progress=)` — the read + rewrite loop,
**shared** by `migrate` and `dry_run`: for each `(attachment, key)` it reads the document, rewrites
it with `rewriter.value`, and hands the result to `sink`; a `drop-record` arrives as an
`Unrepresentable` it catches and skips. It wires the two things a non-local hook needs — the source
view (`_source_view = source.attachment_getting()`) and the record's own key (`_self_key`) — and
clears them in `finally`.

- **`migrate`**'s sink copies blobs **on reference**: before `target.set`, it streams every blob the
  document references that the target lacks (`blobs.copy_blob`), deduped — so exactly the referenced
  blobs are copied, never an orphan, nothing to sweep. The whole pass runs under `_source_snapshot`
  (one read transaction, so a concurrent writer cannot tear the read) and one exclusive target
  transaction.
- **`dry_run`** reuses `_transform_pass` with a no-op sink and a `DiagnosticSink` — no target, no
  blobs, no transaction — a one-pass preview.
- **`verify`** re-derives the expected target through the **same** engine wiring `migrate` used
  (source view + self key — invariant #5's reason it must *mirror*), asserts every kept document
  equals `value(source_doc)`, no spurious document survives, and the target holds exactly the
  referenced blobs.
- **`run`** opens / builds / writes and, on failure, discards the partial target (`_remove_db_file`).

Three refusals fire up front, before any data: `_refuse_ambiguous_drop_record` (a `drop-record`
whose bite sits under a container has no unambiguous record to elide) and
`_refuse_unacknowledged_drops` / `_refuse_unacknowledged_attachment_drops` (record-scoped deletion
needs `accept_*`). `dry_run` enforces only the coherence one — it *informs* about the
acknowledgements rather than gating them.

### The `CommitDatabase` loop — `migrate_commit_database.py`

`migrate` re-issues every commit in topological order (`CommitData.sort`). A **Mutations** commit
replays its opcodes onto a fresh `CommitMutableState` built from the target parent (`remap[parent]`),
under the source view `CommitState@C` (the commit's own materialised source state — the same view
`verify` uses, invariant #5); a **Merge / Enable / Disable** carries no opcodes and is re-issued
structurally (`reissue(label, remap[parent], remap[target])`). All in one exclusive transaction.

- **`translate_opcode` / `_replay_opcodes`** transform each opcode: remap attachment + key, rewrite
  the operand (through `value`, or `_update_value` for the retype-at-path case), and translate the
  path — `translate_path` rebuilds the `PathConst` field-by-field, applying renames and the terminal
  retype. An `XArray_Insert` + `XArray_Update` pair is re-fused into one insert; each content-adding
  verb streams its blobs on reference first (`ensure_blobs`).
- **Record-scoped loss is refused** both ways (invariant #6): a `drop-record` *policy* up front, and
  a hook that raises `Unrepresentable` at runtime → a clear `ValueError` + rollback (an opcode is a
  mutation, not a record).
- **`drop_attachment` is admissible**: its opcodes are skipped (`_addresses_dropped_attachment`, the
  insert pair together) under the shared acknowledgement gate.
- **`verify` is opcode-level**: for each source commit it re-derives each opcode's rewrite under
  `CommitState@C` and asserts the stored target opcode carries exactly that (attachment / key / path
  / operand), plus the DAG topology (`_link_preserved`: every parent link, and a merge/enable/disable
  target link, is the remapped source id). It never compares materialised states.
- **`dry_run`** previews the opcode rewrites (no target), collecting the would-abort record-scoped
  sites and the diagnostic report.

---

## 7 · Extension points

Where you plug in, and the frontier where it stops being mechanical.

### Adding to the vocabulary

- **A new directive.** Add its field + setter to `TransformationDirectives` (`rewrite/directives.py`
  — pure data). If it changes the target *schema*, teach `build_target_definitions` (phase 1) to
  build the new shape; if it changes a *value*, add a branch in `value` or `_retype` (phase 2). If it
  can lose data, its default **must be refusal** (invariant #1) — wire it into `_policy_completeness`.
  Add a `tests/` test for the operation *and* its refuse-by-default; a Class-B op with no such test
  is a hole.
- **A new Class-B policy.** Handle it where the operation is decided (`_retype`, the enum branch, …),
  consult it **only on the offender** (invariant #3), and `_emit` a finding so `dry_run` observes it.
- **A new opcode verb** (CommitDatabase). Rewrite its operand through `value` / `_retype` and its path
  through `translate_path` in `translate_opcode` — never a bespoke conversion (invariant #4). Teach
  `verify` and `dry_run` the same verb; if it can carry blobs, call `ensure_blobs` first.

### The frontier — where it stops being mechanical

Class A and value-closed Class B extend the walk mechanically. Three things do **not** — the genuine
hard edges (the "unfamiliar vs difficult" line of §1), and prior art marks the walls:

- **Cross-field derivation** `C = f(A, B)`. A single top-down, target-directed walk cannot express it
  — you cannot read a sibling you have not visited. This is the tree-transducer limit (macro tree
  transducers, look-ahead). The engine crosses it *only* by making a field hook **struct-scoped**
  (`_apply_field_hook` sees the whole source struct, which is in hand). Do not fold cross-field into
  the plain per-field recursion.
- **Migration composition.** Chaining A→B then B→C, or a split / merge, re-enters the data-exchange
  ambiguity (second-order tgds, Fagin–Kolaitis–Popa–Tan): not guaranteed expressible or stable in the
  directive language. Examine composition explicitly before promising it; do not assume chaining
  "just works."
- **Aggregate over a history.** A fold over a *growing* collection has no single value across a
  `CommitDatabase`'s commits (the trajectory-not-a-value problem of §3). Well-defined per snapshot; a
  semantics question across the DAG, not a missing API.

### Decreed closed — do not "fix"

Some refusals are deliberate; "repairing" one reintroduces an ambiguity we removed. A `Vec`↔`Mat`
reshape, a flat→`Mat` un-flatten, and `Vector`→`Mat` are refused because the target type alone cannot
say resize vs transpose vs (layout-ambiguous) reshape; a `variant`↔non-variant retype is refused for
the same "the target is ambiguous" reason. The fix, when you meet one, is a new **named** directive
or a Class-C hook — never inferring intent from the target shape.

### Prior art

The engine is a **schema-directed, format-agnostic value transformation**, leaning on well-trodden
ground: reader/writer schema resolution (Avro, Protobuf — aliases, type promotion, defaults ≈ the
Class-A surface); an abstract type model with many encodings (ASN.1 BER/DER/… — the
value-model-plus-codecs shape that makes it format-agnostic); functorial data migration (Spivak — a
rename is the shape-preserving functor); data exchange & schema mappings (Fagin–Kolaitis–Miller–Popa);
lenses (Foster et al. — migration is the forward direction, the source kept as rollback); and
datatype-generic / type-directed traversal (Scrap Your Boilerplate — the *mechanism*). What is
distinctive is not the shape but the **discipline**: total-or-explicit-refusal with decreed Class-B
policies, and the dividend that the same engine previews (`dry_run`) and self-verifies (`verify`)
with no target.

---

## 8 · The Viper boundary

The engine is **pure Python over the `dsviper` binding** — a composition of already-bound runtime
atoms, no C++, no I/O. Hold the line: if a change seems to need new runtime behaviour (a new `Value`
operation, a codec, a store primitive), it belongs in the **runtime**, not here. This tool only
*composes* what the binding exposes.

What it trusts the runtime for: `Value` equality is canonical (invariant #7); the construction API is
governed — a `create_*` refuses a dangling or duplicate definition (the same check the DSM parser
runs), so a broken target can never be *built*, and the engine does not re-implement that checker; a
`CommitState` materialises the opcode trace under last-write-wins (§3). The engine adds the
*transform* between definitions; everything below the value model is Viper's.

A corollary: the engine is not tied to a store. `value()` is `Value → Value`, so a `Database` /
`CommitDatabase`, or a JSON / XML document decoded into a value, are peer sources and sinks around the
one engine (`Value.json_decode → value() → Value.to_xml_string` transforms a document's schema *and*
format at once). Migration is the flagship application, not the definition of the tool.

---

## Appendix — a worked trace

One concrete pass, to see phases 1 and 2 together. Source
`Shop::Order { amountCents: int32, label: string, qty: int64, note: string }`, and:

```python
d.retype_field("Shop::Order", "amountCents", Type.INT64)             # widen — Class A
d.rename_field("Shop::Order", "label", "title")                      # rename
d.retype_field("Shop::Order", "qty", Type.INT16, policy="saturate")  # narrow — Class B
d.add_field("Shop::Order", "currency", ValueString("EUR"))           # add
d.drop_field("Shop::Order", "note")                                  # drop
```

**Phase 1** (`build_target_definitions`) builds
`Shop::Order { amountCents: int64, title: string, qty: int16, currency: string }` and records the old
struct id → the new one in `tmap`.

**Phase 2** (`value`), rewriting `{amountCents: 500, label: "shoes", qty: 40000, note: "x"}`, walks
the **target** fields:

- `amountCents` ← retyped `int32 → int64` (widen, Class A) → `500`
- `title` ← source `label` (renamed), same type → recurse → `"shoes"`
- `qty` ← retyped `int64 → int16` (Class B); `40000` overflows, `saturate` → `32767`
- `currency` ← added → its default `"EUR"`
- `note` — never visited (it is not a *target* field, so the target-directed walk never asks about it)

Result: `{amountCents: 500, title: "shoes", qty: 32767, currency: "EUR"}` — valid under the target
schema. Every target field visited and satisfied; nothing silent.
