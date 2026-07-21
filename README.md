# dsviper-database-tools

Definitions-directed **document rewriting** and **database migration** for
[Viper](https://pypi.org/project/dsviper/) — pure Python over the `dsviper`
binding, no C++.

A Viper type's `runtimeId` is a content fingerprint of its *definition* that
doubles as its storage key, so a schema change re-ids and re-keys its data: there
is no in-place `ALTER`, only a **rebuild** — read `Base(A)` read-only, transform,
write a fresh `Base(B)`. The old artefact stays as rollback.

The engine underneath is more general than migration. `DefinitionsRewriter.value` is
`Value → Value` — it rewrites a document from a *source* schema to a *target* schema,
indifferent to where the value came from or where it goes. A `Database` / `CommitDatabase`
migration is its flagship application (what this tool ships); the same engine could transcode
— a value decoded from JSON, re-encoded as XML — because the whole transform lives in Viper's
`Definition` / `Type` / `Value` space. New here? Start with the
**[migration guide](MIGRATION_GUIDE.md)** — the mental map.

This package gives you:

- **`TransformationDirectives`** — a *declarative* edit script (renames, shape
  changes, and the policies that govern lossy operations) — the model familiar from
  Django migrations, not a bespoke DSL.
- **`DefinitionsRewriter`** — one **target-directed** engine that builds the
  target `Definitions` from your directives and rewrites any value from the source
  domain to the target domain, spanning both families (renames *and* add / drop /
  reorder / retype).
- **`migrate_database.run` / `migrate_database.migrate`** — the read-old / write-new loop; pass
  `verify=True` to have the tool prove its own result.
- **`migrate_database.dry_run`** — exercise the rewriter over every document with **no
  target, no blob copy, no transaction**: a one-pass preview of what a migration would do
  (documents kept, dropped, blobs referenced / stranded). The dividend of an I/O-free engine.
- **`plan` / `format_plan`** — a **static plan report** from directives + schema alone (**no
  data**): every change classified lossless / policied / refused (with data-loss flags), and
  warnings for missing policies, `float→int`, and *possible forgotten renames*. Review a
  migration before running anything.
- **`migrate_commit_database.run` / `.migrate` / `.verify` / `.dry_run`** — the same surface for a
  **CommitDatabase**, rebuilt by faithful structural replay: every commit re-issued in topological
  order, history preserved (merges included). `verify` proves the rebuild *opcode by opcode* — a
  `CommitDatabase` stores mutations, so it is each opcode's rewrite that is checked (plus the DAG
  topology), not a re-materialised snapshot; `dry_run` is the same no-write preview at the opcode
  level.
- **`database_migrate.py`** — a root-level command-line tool for the whole decision loop: it
  loads a migration file and dispatches on the source (`Database` or `CommitDatabase`) — `--plan`
  (identify) and `--dry-run` (inform) are read-only pre-flight; `--verify` migrates and proves the
  result.

See the **[migration guide](MIGRATION_GUIDE.md)** for how to *think* about a migration,
and **[REWRITE.md](REWRITE.md)** for how the rewrite *works* and how to extend it — the
target-directed engine, the chain of proof, the invariants, and the code map.

## Install

The runtime `dsviper` is on PyPI; this tool is not published. Install the runtime, clone
this repo, and run the script from the repo — the same shape as `dsviper-tools`:

```bash
pip install "dsviper>=1.2.20"
git clone <repo> dsviper-database-tools
cd dsviper-database-tools
python3 database_migrate.py <migration> <source> <target>
```

(To use the engine as a library from elsewhere, `pip install -e .` puts
`dsviper_database_tools` on your path.)

## Write a migration

A migration is a Python file that defines `build_directives(source_defs)` — it
receives the source's live schema, so you build directives against real type/field
names. The tool loads it and rewrites the database.

```python
# migration_shop_v2.py
from dsviper import Type, ValueString
from dsviper_database_tools import TransformationDirectives

def build_directives(source_defs):
    d = TransformationDirectives()
    d.rename_field("Shop::Customer", "fullname", "full_name")
    d.add_field("Shop::Customer", "email", ValueString(""))       # seeded default
    d.drop_field("Shop::Customer", "legacyId")
    d.retype_field("Shop::Order", "amountCents", Type.INT64)      # widening — automatic
    d.retype_field("Shop::Order", "quantity", Type.INT16, policy="saturate")   # lossy — policy
    return d
```

Run it — the decision loop is on the command line (*identify → inform → decide*):

```bash
python3 database_migrate.py migration_shop_v2.py old.db          --plan      # identify: the static plan, no write
python3 database_migrate.py migration_shop_v2.py old.db          --dry-run   # inform:   real loss on real data, no write
python3 database_migrate.py migration_shop_v2.py old.db new.db   --verify    # decide:   migrate, then prove it faithful
```

`old.db` is opened read-only and left intact; `new.db` is the rebuilt database. `--plan`
(schema-only, what *could* change) and `--dry-run` (which policies bite, what would drop, on the
real data) are **read-only pre-flight** — they print and exit, so the target is omitted. `--verify`
proves the target is a faithful image, `--force` overwrites an existing target, `-v` prints the
migration summary. For a lossless migration, skip straight to the run; walk the earlier steps when
it can lose data.

## The directive surface

`TransformationDirectives` is the complete, declarative edit script. Every directive
names its target by **qualified name** (`representation()`, e.g. `"Shop::Order"`);
fields and cases are plain names. Renames and retypes name the field/case by its
**source** name — the schema you are migrating *from* — even when you also rename it.

Each operation is **Class A** (lossless — automatic), **Class B** (lossy — refused until you name
a policy), or **Class C** (a hook you write). The **[migration guide](MIGRATION_GUIDE.md)**
explains the loss model and when to reach for each; this section is the exact reference.

**Renames** — the name *is* the identity, so a rename is a re-id (references follow):

| Directive | Effect |
|---|---|
| `rename_type(old, new)` | rename a struct / enum / concept / club (FQN → FQN) |
| `rename_field(struct, old, new)` | rename a struct field |
| `rename_case(enum, old, new)` | rename an enum case |
| `rename_attachment(old_id, new_id)` | rename an attachment — address it by `identifier()` (`NS::KeyConcept.name`); a bare local name is accepted but is not unique, so it renames every attachment of that name |

**Namespaces** — two orthogonal axes, plus per-definition moves for split / merge:

| Directive | Effect |
|---|---|
| `rename_namespace(old_ns, new_name)` | change a namespace's display **name** — new representations, unchanged `runtimeId`s |
| `remap_namespace(old_ns, new_uuid)` | change a namespace's identity **UUID** — new `runtimeId`s, unchanged representations |
| `move_type(type_repr, target_ns)` | move one struct / enum / concept / club to another namespace (split / merge) — Class A |
| `move_attachment(identifier, target_ns)` | move one attachment to another namespace |

**Struct field shape:**

| Directive | Effect | Class |
|---|---|---|
| `add_field(struct, name, default)` | add a field seeded with `default` (a primitive-leaf `Value`) | A |
| `add_field(struct, name, type, derive=fn)` | add a field **derived** from the struct — `fn(source_struct, field_name, target_type)` | C |
| `drop_field(struct, name)` | remove a field | A |
| `reorder_fields(struct, order)` | set the target field order (a permutation of the field names) | A |
| `retype_field(struct, name, new_type, policy=None)` | change a field's type (leaf, `Set`/`Vector`/`Map`/`XArray`/`Optional`/`Tuple` element, `Vec`/`Mat` element, variant arm-set, the `Vec`↔`Vector` bridge); `policy` required when lossy | A / B |

**`Vec` / `Mat` dimension** (named explicitly — never inferred from a target shape):

| Directive | Effect | Class |
|---|---|---|
| `resize_vec_field(struct, field, size, *, fill="zero", on_shrink="fail")` | grow (fill new cells) or shrink a `Vec` field | A / B |
| `resize_mat_field(struct, field, columns, rows, *, fill="identity", on_shrink="fail")` | grow or shrink a `Mat` field | A / B |
| `transpose_mat_field(struct, field)` | `Mat<c,r> → Mat<r,c>`, `[i,j] → [j,i]` | A |

**Enum shape:**

| Directive | Effect | Class |
|---|---|---|
| `add_case(enum, name)` | add a case (appended at the end) | A |
| `reorder_cases(enum, order)` | set the target case order (a permutation of the case names) | A |
| `remove_case(enum, case, policy)` | remove a case; `policy` governs values still holding it | B |

**Definition-level drops** — remove a whole definition (the co-direction of the build):

| Directive | Effect |
|---|---|
| `drop_type(type_repr)` | remove a struct / enum / concept / club (refused if a surviving definition still references it, with an accumulated report) |
| `drop_attachment(identifier)` | remove an attachment **and delete its documents** — requires `accept_attachment_drops()` |

**Custom hooks (Class C)** — for a change no declarative directive expresses (a field retyped
to an *unrelated* type, a value derived from siblings or from another document). The hook owns
its loss model; the engine validates its output against the target type:

| Directive | Effect |
|---|---|
| `transform_field(struct, field, new_type, fn)` | replace one field via `fn(source_struct, field_name, target_type)` — sees its siblings (cross-field), and via a `ctx` argument can dereference into `Base(A)` (cross-document) |
| `transform_type(source_type, new_type, fn)` | replace **every** occurrence of a type via `fn(value, target_type)` — rides the recursion into nested positions |

**Documentation** (Class A — carried by default, these override; `""` clears):

| Directive | Effect |
|---|---|
| `document_type(type_repr, text)` | set a struct / enum / concept / club docstring |
| `document_field(struct, field, text)` | set a field docstring |
| `document_case(enum, case, text)` | set an enum-case docstring |
| `document_attachment(identifier, text)` | set an attachment docstring |

**Policies & acknowledgements:**

| Directive | Effect |
|---|---|
| `resolve_collisions(winner)` | how a `Map`-key / `Set`-element collision resolves — `"fail"` / `"first"` / `"last"` |
| `accept_document_drops()` | sign off that a `drop-record` policy may **delete whole documents** |
| `accept_attachment_drops()` | sign off that `drop_attachment` may **delete whole attachments** |

**Class-B policy vocabulary, by operation** — the exact policy each lossy operation accepts (it
converts in-range / parseable / non-nil values exactly, and applies the policy only to the
offenders; the *why* is the guide's §3):

- numeric narrowing (incl. `Set`/`Vector`/`Map`/`XArray`/`Optional`/`Tuple` and `Vec`/`Mat` element) → `"fail"` (default) / `"saturate"` / `("default", value)`
- parse `string→X` → `"fail"` / `("default", value)` / `"drop-record"`
- `Optional<A>→A` on nil → `"fail"` / `("default", value)` / `"drop-record"`
- remove a populated enum case / variant arm → `"fail"` / `("map-case", name)` / `"drop-record"`
- `Vector→Vec` length-fit → `"fail"` / `("fit", pad)` / `"drop-record"`
- `Vector→Set` collapse, `Map` key collision → `resolve_collisions(winner)`

## Programmatic use

```python
import dsviper as V
from dsviper_database_tools import DefinitionsRewriter, migrate_database

source = V.Database.open("old.db", readonly=True)
directives = build_directives(source.definitions())
transformer, target_defs = DefinitionsRewriter.from_directives(
    source.definitions(), directives)

target = V.Database.create("new.db")
target.extend_definitions(target_defs.const())
migrate_database.migrate(source, transformer, target)   # owns its own exclusive transaction
```

`migrate_database.migrate` transforms every document and streams each referenced blob to the
target as it is first needed (copy-on-reference — exactly the referenced blobs, never an
orphan), in one exclusive transaction that rolls back on any failure. Pass `verify=True` to
`migrate_database.run` (or call `migrate_database.verify`) to have the tool prove the target is
a faithful image. Pass `on_progress=` (to `migrate` or `run`) a callback that receives a
`MigrationProgress` — bytes copied against the source's total blob bytes, documents, and
attachment position — for a progress bar over the dominant cost (blob I/O).

`migrate_commit_database` mirrors this surface for a `CommitDatabase` — same
`migrate` / `verify` / `dry_run` / `run`, an `on_progress=` callback receiving a
`CommitMigrationProgress` (bytes + commit position), and the same rollback-on-failure. It
replays the commit DAG instead of copying documents; see the
[migration guide §5](MIGRATION_GUIDE.md) for what that changes.

## Status

Beta — feature-complete and self-verifying, proven on real data at industrial scale, but not
yet published, API-frozen, or battle-tested by an outside user. The rewrite engine covers the
full type / directive surface — all containers,
`Vec`/`Mat` (element conversion, resize, transpose, the `Vector` bridge), `XArray`, the three
key flavours, variant arm-sets, definition-level drops, namespace split / merge, and Class-C
hooks (cross-field, cross-document single-reference, and aggregate). The `Database` loop copies
exactly the referenced blob bytes (copy-on-reference — never an orphan) and verifies its own
result; the `CommitDatabase` loop replays the whole commit DAG faithfully (history preserved,
merges included) over the 10 opcode verbs, in one atomic transaction that rolls back on failure,
and verifies itself end to end (every opcode correctly rewritten, the DAG topology preserved). No
dedicated directive yet — a Class-C hook expresses each today: `Vec`/`Mat` **reshape**, **variant
arm retype**, and **aggregate** derivation over a *collection* of other documents.
