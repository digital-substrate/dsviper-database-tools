# Migration guide

*How to think* about migrating a Viper database — from the model to the decision. The
[README](README.md) is the reference (every directive, in tables); this guide is the mental
map.

> 1 · The model · 2 · The decision loop · 3 · The loss model · 4 · The directive vocabulary ·
> 5 · `Database` vs `CommitDatabase` · 6 · End-to-end.

---

## 1 · The model — rebuild, not ALTER

One fact drives everything: **a Viper type's `runtimeId` is a content fingerprint of its
*definition*, and that same id is its storage key.** The definition and the key are not two
things kept in sync — they are the *same* thing.

So the moment you change a definition — add a field, drop one, retype one, rename the type —
its `runtimeId` changes, and therefore *its storage key changes*. The existing data is still
keyed under the old id. You cannot edit the schema of a live store in place:

> **There is no `ALTER`. A schema change is a rebuild.**

The rebuild is always the same shape:

1. Open the source **read-only** — `Base(A)`. It is never modified.
2. Transform every value from the source domain to the target domain.
3. Write a **fresh** target — `Base(B)`.
4. Keep `Base(A)` as your rollback.

You never lose the ability to go back, because the old artefact is untouched.

### You describe *what* changed, not *how* to rewrite

You do not write the value-by-value rewrite. You write a **declarative** description of the
schema change — a `TransformationDirectives`, the way a Django migration names the edits — and
the engine works out how to rewrite each value:

```python
from dsviper import Type, ValueString
from dsviper_database_tools import TransformationDirectives

def build_directives(source_defs):        # receives the source's live schema — no guessing
    d = TransformationDirectives()
    d.rename_field("Shop::Customer", "fullname", "full_name")
    d.add_field("Shop::Customer", "email", ValueString(""))
    d.retype_field("Shop::Order", "amountCents", Type.INT64)      # widening — automatic
    return d
```

### Two stores, one model

Viper has two persistent shapes, and the tool rebuilds both:

- a **`Database`** stores the *documents themselves*;
- a **`CommitDatabase`** stores the *mutations* of documents as opcodes in a commit DAG; a
  document is what the runtime *materialises* from those opcodes.

They differ **only in how documents are materialised**. What a migration is *allowed* to do is
the same for both, because that is a property of the definitions, not of the storage. Where the
two genuinely diverge is §5.

---

## 2 · The decision loop — identify → inform → acknowledge → decide

The tool has one rule: **no silent loss.** A change that cannot lose information is applied
automatically; a change that can is **refused until you decide** — always *before* any data is
touched.

For a lossless migration the decision is trivial — go straight to the last step:

```python
migrate_database.run("old.db", build_directives, "new.db", verify=True)
```

When a migration *can* lose data, four steps let you look before you leap. Each answers a
different question:

- **Identify** — *what could change?* `plan(source_defs, directives)` classifies every change
  from the schema alone, no data: lossless, policied, or refused — and flags likely mistakes.
- **Inform** — *what actually happens to my data?* `dry_run(source, rewriter)` runs the real
  rewrite over real data but **writes nothing** — which policies bite, on which values, how
  many records would drop.
- **Acknowledge** — *do I accept deleting whole records?* Losing a field is one thing; deleting
  a whole document or attachment is graver, so it takes an explicit sign-off —
  `accept_document_drops()` / `accept_attachment_drops()`. Without it, `run` refuses.
- **Decide** — *do it, and prove it.* `run(..., verify=True)` writes the target in one
  transaction that rolls back on any failure, then proves the result is a faithful image.

The pre-flight tools work on the directives, and on the engine built from them:

```python
directives = build_directives(source.definitions())
rewriter, _ = DefinitionsRewriter.from_directives(source.definitions(), directives)
```

`plan` takes the directives, `dry_run` takes the rewriter, and `run` builds both for you from
`build_directives`.

> **The spine:** `plan` → `dry_run` → `accept_*` → `run` / `verify`. Reach for the middle steps
> when a migration is lossy or destructive; skip them when it isn't.

---

## 3 · The loss model — total, or an explicit refusal

One principle governs every value the engine touches:

> **Total, or an explicit refusal.** Every value becomes a valid target value, or the migration
> refuses — cleanly, and before any data is written. It never produces a silently wrong one.

The three classes you will hear about — A, B, C — are the *surface* of that rule: they name
*how* it applies to a given change.

**Class A — lossless.** Every value has a faithful target, so the principle is met with nothing
asked of you: widening a number, adding a field with a default, dropping a field, reordering,
`X → string`.

**Class B — lossy, so policied.** Some values *may* have no faithful target — narrowing a
number, parsing a string, removing an enum case data still holds, collapsing a set. The
principle refuses until you say what should happen to those offenders, via a **policy** checked
before any data is touched. In-range values still convert *exactly*; the policy governs only the
offenders:

```python
d.retype_field("Shop::Order", "quantity", Type.INT16, policy="saturate")
# 30000 -> 30000 (exact);  99999 -> 32767 (saturated — the offender)
```

Most policies bound the loss to a **field** (`"saturate"`, `("default", v)`,
`("map-case", name)`, `("fit", pad)`). One is different in kind — `"drop-record"` deletes the
**whole enclosing document**, which is why the *acknowledge* step in §2 signs it off.

**Class C — your hook.** When no directive expresses the change, you supply the transform
yourself (`transform_field` / `transform_type` / `add_field(..., derive=)`). The principle still
holds: the engine validates your output against the target type and refuses anything that is not
a valid target value.

> Class A never asks; Class B asks once, up front; Class C hands you the pen but checks your
> answer.

---

## 4 · The directive vocabulary, by intent

You reach for a directive by *what you want to do*. Seven intents cover the whole surface; the
README has every signature and policy.

| I want to… | Directives |
|---|---|
| **rename** a type, field, case, or attachment | `rename_type` · `rename_field` · `rename_case` · `rename_attachment` |
| **reshape** a struct or enum | `add_field` · `drop_field` · `reorder_fields` · `add_case` · `reorder_cases` · `remove_case` |
| **retype** a field | `retype_field` · `resize_vec_field` · `resize_mat_field` · `transpose_mat_field` |
| **drop** a whole definition | `drop_type` · `drop_attachment` |
| **move** definitions across namespaces | `rename_namespace` · `remap_namespace` · `move_type` · `move_attachment` |
| **derive** what the vocabulary can't express | `transform_field` · `transform_type` · `add_field(..., derive=)` |
| **document** a definition | `document_type` · `document_field` · `document_case` · `document_attachment` |

Most of these are Class A. The ones that can lose data are the few you would expect —
`remove_case` and a narrowing `retype_field` (Class B, policied), and the `derive` /
`transform_*` hooks (Class C). Two things worth holding in mind:

- **You always name by the *source* schema.** A field or case is addressed by the name it has in
  `Base(A)` — even when the same directive renames it. (A rename is a re-id; references follow.)
- **§1 simplified: a couple of edits sit *outside* the `runtimeId`.** The id fingerprints a
  definition's *structure*, not its labels or its prose — so two edits re-label without re-keying:
  a **docstring** change (documentation rides across every migration for free; the `document_*`
  directives only *override* what is carried), and a **`rename_namespace`** (new representations,
  same ids). Only a namespace's *UUID* — `remap_namespace` — is in the fingerprint.

---

## 5 · `Database` vs `CommitDatabase`

Everything so far — the model, the loop, the loss model, the vocabulary — applies to **both**
stores, unchanged. You write one migration; the tool rebuilds whichever it is handed
(`database_migrate.py` dispatches on the source kind). This is because *what a migration may do*
is a property of the **definitions**, and the definitions are the same however the documents are
stored.

The two differ **only in how documents are materialised** — a `Database` holds them directly, a
`CommitDatabase` replays its opcodes to produce them — and that surfaces in exactly two places
you can see:

- **History is preserved.** A `CommitDatabase` is not flattened to its latest state and rebuilt
  as a snapshot: every commit is re-issued in order, branches and merges included. You migrate the
  *whole history*, and it stays a history.
- **Record-scoped loss has no place.** On a `Database`, `drop-record` deletes a materialised
  document. A `CommitDatabase` has no single document to delete — an opcode is one *mutation* in a
  document's trajectory, and dropping it would leave the document half-migrated, not gone. So a
  `CommitDatabase` migration **refuses** `drop-record` (and any hook that drops a value): bound the
  loss to a field instead (`"saturate"`, `("default", v)`, `("map-case", name)`), or migrate
  through a `Database`.

Everything else — renames, retypes, drops, `drop_attachment`, namespace moves, Class-C hooks, blob
handling, and `verify` proving the result — is the same on both.

---

## 6 · End-to-end

### A `Database`

A migration is a Python file defining `build_directives` — mix lossless and policied edits freely:

```python
# shop_v2.py
from dsviper import Type, ValueString
from dsviper_database_tools import TransformationDirectives

def build_directives(source_defs):
    d = TransformationDirectives()
    d.rename_field("Shop::Customer", "fullname", "full_name")                 # A
    d.add_field("Shop::Customer", "email", ValueString(""))                   # A
    d.retype_field("Shop::Order", "quantity", Type.INT16, policy="saturate")  # B
    return d
```

Run it — the source is opened read-only, a fresh target is written, `--verify` proves it:

```bash
python3 database_migrate.py shop_v2.py old.db new.db --verify
```

When a migration is lossy or destructive, walk the loop (§2) from Python first — look before you
leap:

```python
import dsviper as V
from dsviper_database_tools import plan, format_plan, migrate_database, DefinitionsRewriter

source = V.Database.open("old.db", readonly=True)
directives = build_directives(source.definitions())
rewriter, _ = DefinitionsRewriter.from_directives(source.definitions(), directives)

print(format_plan(plan(source.definitions(), directives)))   # identify — what could change
print(migrate_database.dry_run(source, rewriter))            # inform  — what happens to the data
# directives.accept_document_drops()                         # acknowledge — only if it drops records
# happy? run the command above, or migrate_database.run("old.db", build_directives, "new.db", verify=True)
```

### A `CommitDatabase`

The **same file, the same command** — the tool sees the source is a `CommitDatabase` and replays
its whole history rather than copying documents:

```bash
python3 database_migrate.py shop_v2.py old.rapmc new.rapmc --verify
```

The only thing to reconsider is record-scoped loss: `"drop-record"` is refused here (§5). Bound the
loss to a field, or migrate through a `Database`.
