# dsviper-database-tools

Definitions-directed **document rewriting** and **database migration** for
[Viper](https://pypi.org/project/dsviper/) — pure Python over the `dsviper`
binding, no C++.

A Viper type's `runtimeId` is a content fingerprint of its *definition* that
doubles as its storage key, so a schema change re-ids and re-keys its data: there
is no in-place `ALTER`, only a **rebuild** — read `Base(A)` read-only, transform,
write a fresh `Base(B)`. The old artefact stays as rollback.

This package gives you:

- **`TransformationDirectives`** — a *declarative* edit script (renames, shape
  changes, and the policies that govern lossy operations) — the model familiar from
  Django migrations, not a bespoke DSL.
- **`DefinitionsTransformer`** — one **target-directed** engine that builds the
  target `Definitions` from your directives and rewrites any value from the source
  domain to the target domain, spanning both families (renames *and* add / drop /
  reorder / retype).
- **`run_migration` / `migrate_database`** — the read-old / write-new loop; pass
  `verify=True` to have the tool prove its own result.
- **`run_commit_migration` / `migrate_commit_database`** — a **CommitDatabase** rebuilt
  by faithful structural replay: every commit re-issued in topological order, history
  preserved (merges included, since a merge only seeds the DAG linearization).
- **`database_migrate.py`** — a root-level command-line tool: it loads a migration
  file, opens the source read-only, and writes a fresh target — dispatching on the
  source (`Database` or `CommitDatabase`).

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for how the rewrite works — the
target-directed engine, the loss model, and the guarantees.

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

Run it:

```bash
python3 database_migrate.py migration_shop_v2.py old.db new.db --verify
```

`old.db` is opened read-only and left intact; `new.db` is the rebuilt database.
`--verify` proves the target is a faithful image, `--force` overwrites an existing
target, `-v` prints the migration summary.

## The directive surface

`TransformationDirectives` is the complete, declarative edit script. Every directive
names its target by **qualified name** (`representation()`, e.g. `"Shop::Order"`);
fields and cases are plain names. Renames and retypes name the field/case by its
**source** name — the schema you are migrating *from* — even when you also rename it.

| Directive | Effect | Family · class |
|---|---|---|
| `rename_namespace(old_ns, new_name)` | change a namespace's display **name** — new `Namespace::Type` representations, unchanged `runtimeId`s | rename |
| `remap_namespace(old_ns, new_uuid)` | change a namespace's identity **UUID** — new `runtimeId`s, unchanged representations | re-home |
| `rename_type(old, new)` | rename a concept / club / enum / struct (FQN → FQN) | rename |
| `rename_field(struct, old, new)` | rename a struct field | rename |
| `add_field(struct, name, default)` | add a field, seeded with `default` (a primitive-leaf `Value`) | shape · A |
| `drop_field(struct, name)` | remove a field | shape · A |
| `reorder_fields(struct, order)` | set the target field order (a permutation of the field names) | shape · A |
| `retype_field(struct, name, new_type, policy=None)` | change a field's leaf type; `policy` required when lossy | shape · A/B |
| `rename_case(enum, old, new)` | rename an enum case | rename |
| `add_case(enum, name)` | add a case (appended at the end) | shape · A |
| `reorder_cases(enum, order)` | set the target case order (a permutation of the case names) | shape · A |
| `remove_case(enum, case, policy)` | remove a case; `policy` governs values still holding it | shape · B |
| `rename_attachment(old_id, new_id)` | rename an attachment (its local name) | rename |
| `resolve_collisions(winner)` | how a `Map`-key / `Set`-element collision resolves (global) | policy |

**Class A** operations (widen, add-with-default, drop, reorder, add-case) are total and
apply automatically. **Class B** operations can lose information and carry a policy
(below). Not expressible as a directive — deliberately: splitting or merging a type or
field, re-parenting a concept, and cross-field derivations (these need custom code, not
a declaration).

**Policies (no silent loss).** Every lossy operation is refused by default and must
carry an explicit policy — checked *before* any data is touched:

- numeric narrowing → `"fail"` (default) / `"saturate"` / `("default", value)`
- parse `string→X` → `"fail"` / `("default", value)` / `"drop-record"`
- `Optional<A>→A` on nil → `"fail"` / `("default", value)` / `"drop-record"`
- remove a populated enum case → `"fail"` / `("map-case", name)` / `"drop-record"`
- `Vector→Set` collapse, `Map` key collision winner, …

An in-range / parseable / non-nil value always converts exactly; a policy governs
only the offenders. A `drop-record` policy makes the migration loop skip that
document.

## Programmatic use

```python
import dsviper as V
from dsviper_database_tools import DefinitionsTransformer, migrate_database

source = V.Database.open("old.db", readonly=True)
directives = build_directives(source.definitions())
transformer, target_defs = DefinitionsTransformer.from_directives(
    source.definitions(), directives)

target = V.Database.create("new.db")
target.extend_definitions(target_defs.const())
migrate_database(source, transformer, target)   # owns its own exclusive transaction
```

`migrate_database` copies the referenced blob bytes, transforms every document, and
reclaims any blob the schema change stranded. Pass `verify=True` to `run_migration`
(or call `verify_migration`) to have the tool prove the target is a faithful image.

## Status

Alpha. The rewrite engine covers the full type / directive surface (all containers,
`Vec`/`Mat`, `XArray`, the three key flavours); the `Database` loop copies blob bytes,
reclaims stranded blobs, and verifies its own result; the `CommitDatabase` loop replays
the whole commit DAG faithfully (history preserved, merges included) over the 10 opcode
verbs, in one atomic transaction. Not yet handled: `Vec`/`Mat` element widening, custom
cross-field (Class-C) hooks, and a round-trip verifier for a `CommitDatabase`.
