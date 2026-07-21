# DEFINITIONS_MIGRATE.md — the DSM-source codemod, for maintainers

The developer/maintainer companion to `definitions_migrate.py`, the **source twin** of the data
migration. `database_migrate.py` migrates the data in a base; this tool migrates the hand-authored
`.dsm` files that declare that schema — under the *same* `transformation.py`. For the engine that
both rest on, read [REWRITE.md](REWRITE.md); to *use* the tools, the
[migration guide](MIGRATION_GUIDE.md).

> 1 · The principle · 2 · Module map · 3 · The chain of proof · 4 · Invariants · 5 · The edit
> algebra · 6 · The directives, family by family · 7 · Extension points · 8 · The Viper boundary ·
> Appendix — a worked trace.

---

## 1 · The principle — patch, don't render

A schema change produces two artefacts to migrate, not one: the *data*, and the *source that
declares it*. The obvious way to migrate the second is to build the target `Definitions` with the
engine and render it back to DSM. That is exactly what this tool does **not** do, and the refusal
is the whole design.

Rendering is lossy in a dimension the engine does not model. A hand-authored tree carries a **file
split**, **comments**, **member ordering**, blank lines, alignment, a house style — none of which
lives in `Definitions`. Round-tripping through the engine would silently flatten all of it: the
migration would be correct and the diff unreadable, which for a hand-maintained artefact is a
failure.

So the tool is a **structured codemod**: it edits the source text *in place* (into a fresh copy),
at spans the parser reports, and touches nothing else. Everything not named by a directive is
preserved **by construction** — not by care, not by a rendering that happens to be faithful, but
because those bytes are never read, never rewritten, never even loaded into a model.

That leaves the tool with exactly two things to know, and it owns **neither**:

- **What the target looks like** — the *shape*. Answered by the engine
  (`DefinitionsRewriter.from_directives` → `target_defs` + `type_map`). No target type text is ever
  authored here; a retyped field's new type is `type_map`'s answer, stringified.
- **Where, exactly, in the text** — the *position*. Answered by the parser's `DSMSourceMap`
  by-product: the source span of every declaration, field, case, namespace, docstring and
  **resolved** type-reference.

The codemod is the join of those two oracles, and nothing more. Each directive becomes a set of
`(file, start, stop, replacement)` edits; the edits are spliced; the result is re-parsed and its
digest compared to the engine's target. **The engine is used as a verifier, not as a producer** —
that inversion is what distinguishes this module from every other consumer of `rewrite/`.

One consequence worth stating up front, because it will save you an afternoon: the tool is
**declaration-directed**, not text-directed. It never searches the text for a name. Every semantic
position comes from the source-map; the only text scanning it does is *syntactic* and local —
finding a closing brace, a statement terminator, a line's indentation.

---

## 2 · Module map

One file, `definitions_migrate.py` (repo root, a CLI like `database_migrate.py`), in four layers.
The layers are worth keeping distinct in your head: a bug in the bottom two corrupts *any* edit; a
bug in the top two corrupts *one directive*.

| Layer | Symbols | Role |
|---|---|---|
| **Span resolution** | `_Resolver` · `_line_starts` | a global offset into the assembled content → `(source file, local start, local stop)`, half-open |
| **The edit algebra** | `_Edit` · `_apply` · `_resolve_overlaps` · `_tidy_cut` · `_tidy_cut_case` | splice a set of edits into one file's text without them treading on each other |
| **Derivation** | `_derive` (+ `_index`, `_insert_before_close`, `_render_field_line`, `_render_doc`) | directives + source-map + engine → edits. One directive family per paragraph, in dependency order |
| **Region rewrites** | `_reorder_fields` / `_reorder_cases` / `_bake` / `_drop_region_edits` · `_relocate_moved_types` / `_match_brace` | the two operations that move *text*, not just replace it — they consume the edits already derived |

The entry points around them:

| Symbol | Role |
|---|---|
| `definitions_migrate(dsm_dir, transformation_module, out_dir, *, verify=True)` | the whole chain (§3): read → parse → directives → engine → derive → apply → verify → write |
| `_read_tree` / `_parse` | I/O and the `DSMBuilder` assembly — the only places that touch a file or the parser |
| `_refuse_unsupported` / `_UNSUPPORTED` | the fail-closed seam (§7): a directive with no source-patch is refused up front |
| `_refuse_dangling_pools` / `_pool_findings` / `_signature_type_names` | the pools, which the engine cannot see: refuse a dropped type still named by a signature, notify a rewritten one |
| `main` / `_load_transformation` | the CLI (`--no-verify`, `--force`) |

> **Read order for a newcomer:** `definitions_migrate()` (the six numbered steps — the whole story
> in forty lines) → `_derive` top to bottom (each directive family is a self-contained paragraph)
> → `_apply` + `_resolve_overlaps` (why edits do not collide) → `_reorder_fields` and
> `_relocate_moved_types` **last**. The two region rewrites are the only genuinely subtle code in
> the file, and they only make sense once you have seen what they are consuming.

---

## 3 · The chain of proof

Six steps, and each is a link. Read `definitions_migrate()` alongside.

1. **Read** the source tree (`_read_tree`) — `.dsm` files only, sorted, never mutated.
2. **Parse** with a `DSMSourceMap` attached (`_parse`). A parse error here is the *user's* source
   being broken, and it is refused with file:line:pos before anything else runs.
3. **Directives** — `transformation_module.build_directives(source_defs)`. The **same file** the
   data migration runs, over the same source definitions, so the two artefacts cannot drift by
   construction: one edit script, two consumers.
4. **The engine oracle** — `DefinitionsRewriter.from_directives(source_defs, directives)` yields
   the target `Definitions` and `type_map`. Every refusal the engine makes (an un-policied lossy
   op, a dangling `drop_type`, a namespace collision) fires *here*, before a byte of text is
   touched, with the engine's own message.
5. **Derive and apply** — `_derive` produces the edits, `_apply` splices them per file.
6. **Verify, then write** — re-parse the patched tree **in memory** and compare digests; only then
   write `out_dir`. A failed verify leaves no target tree behind.

### What the digest proves

`runtimeId` is a content fingerprint of a definition and `Definitions.hexdigest()` aggregates them,
so an equal digest is a strong structural claim: the patched source declares *the same schema* the
engine built. It is sensitive to the **namespace uuid**, **type names**, **field names**, **field
order**, **field types and defaults**, **cases and their order**.

### What the digest does *not* prove — read this before adding a test

The digest is computed over the *persistence* `Definitions`, which deliberately excludes several
things this tool nonetheless edits. Where the oracle is blind, **the test must assert on the
patched text itself**; `verify=True` will happily pass a wrong patch.

| Edited by the tool | In the digest? | What actually covers it |
|---|---|---|
| documentation (`document_type` / `_field` / `_case` / `_attachment`) | **no** — a doc is outside the `runtimeId` by design (a doc change must never re-id or re-key data) | the re-parse (it is syntactically valid) + a text assertion |
| a namespace's **display name** (`rename_namespace`) | **no** — the `runtimeId` binds the namespace *uuid*, not its name | the re-parse (references must still resolve) + a text assertion |
| function-pool signatures (both kinds — see below) | **no** — pools are binding/service, outside persistence | the re-parse: a signature naming a renamed or moved type must still **resolve**, and unambiguously |
| the file split, comments, formatting, blank lines | **no** | nothing — they are preserved *by construction* (§1), which is why no code may rewrite a region it was not asked to |

So there are really **two** oracles, and they are complementary: the **re-parse** proves the patch
is *syntactically valid and referentially closed*; the **digest** proves it is *structurally the
target*. Neither alone is enough, and both together still say nothing about style.

> **The rule, stated once.** Anything the digest cannot see must be pinned by a text assertion in
> `tests/test_definitions_migrate.py`. A test for a documentation, namespace-rename, or pool-facing
> change that only calls `definitions_migrate(..., verify=True)` and asserts nothing is a test that
> passes on a no-op.

---

## 4 · Invariants

The properties the code holds true. A change that reads fine and quietly breaks one is the
dangerous kind.

**1 · The engine is the only authority on *shape*.** No DSM type expression is composed by hand.
A changed field's text comes from `type_map` — `tgt_field[...].type().representation(namespace=tns)`
in `_derive` — for *every* type-changing directive at once (`retype_field`, `transform_field`,
`resize_*`, `transpose_*` share the single `type_changed` pass). This is why the dimension and
Class-C directives need no DSM-specific code: they change the target type, and the target type is
read off the engine. Never special-case a type's spelling here; if the text is wrong, the engine's
type is wrong.

**2 · The source-map is the only authority on *position*.** Every semantic edit is anchored to a
span the parser reported. The tool must never locate a declaration, field or reference by searching
the text — a comment mentioning `Order`, a string literal containing `::`, or a field name that is
a substring of another would all break it. Local *syntactic* scanning is fine and expected
(`_tidy_cut`, `_line_indent`, `_match_brace`, `_insert_before_close`): those look for braces,
terminators and whitespace, never for meaning.

**3 · One span, one edit.** `_apply` splices right-to-left over offsets that assume the text has
not moved beneath them; `_resolve_overlaps` only understands **strict containment** (a wholesale
replacement subsumes the finer edits inside it). Two edits with the *same* span would both apply
and corrupt each other. When you add a directive, ask which existing family could name the same
span, and make one subsume the other.

**4 · A reference mirrors the source's qualification — except a move, which always qualifies.**
The unified reference pass rewrites a resolved reference to its target name while **keeping the
form the author chose**: a bare `Customer` stays bare, a qualified `Shop::Order` keeps its prefix.
A pool signature may write either — outside a namespace a bare name still resolves, provided the
parser finds exactly one candidate. The one exception is `move_type`: a bare `T` left behind in the
old namespace would dangle, so a moved referent is always rewritten fully qualified. This is also
the pass that makes pool signatures survive a rename (§3) — the parser resolves them, so the
source-map holds their references like any other.

**5 · A relocated member carries its own edits, and they leave the list.** `_bake` applies the
edits falling inside a member's extent to the member's *text* before that text is moved; the
consumed edits are then dropped (`_drop_region_edits` for a reorder, an identity filter in
`_relocate_moved_types`). Otherwise the same edit would apply twice — once inside the moved text,
once at the now-vacated offsets — or, worse, be silently lost. Whenever text moves, ask what was
supposed to happen *inside* it.

**6 · Derivation order is dependency order.** The paragraph order inside `_derive` is not
cosmetic. Renames, references, types and docs are derived first; **`reorder` next**, so a reordered
member's own edits are already available to bake; **`move` last**, so a declaration that is
renamed *and* retyped *and* reordered *and* moved carries all of it into its new namespace. Moving
a paragraph up in that function is a silent-corruption change, not a refactor.

**7 · Nothing references an attachment.** A key is a concept-instance identity, not a foreign key,
so an attachment rename / move / drop patches its **declaration only** — no reference sweep. Same
fact, same consequence as on the data side. (Attachments are declarations in the source map like
any type, keyed — like every declaration — on the map's own `identifier()`, which for an attachment
is `NS::KeyConcept.name`.)

**8 · Fail closed at the seam.** `_UNSUPPORTED` is intentionally **empty** — every directive is
patched today. It stays as the seam: a directive added to `TransformationDirectives` upstream and
not taught to `_derive` should be refused *up front* with an actionable message, not left to the
digest oracle to reject after the fact with a hex mismatch. Extending the directive vocabulary
without visiting this file is the failure mode it exists to catch.

**9 · The source tree is read-only, and the target is written only after verification.** The
inputs are never mutated (the tool's non-destructiveness is what makes it safe to rerun), and the
re-parse + digest check runs on the in-memory patched text, so a failure leaves nothing on disk —
the codemod's twin of the data migration discarding a partial target.

---

## 5 · The edit algebra

The bottom two layers, in detail. Everything above them produces `_Edit`s and trusts this.

### Spans

The parser reports offsets into `builder.content()` — the *assembled* content of every file, as a
Python `str`, so offsets are **code points, not bytes** (an accented docstring does not shift the
arithmetic). Spans are **inclusive**; `_Resolver.resolve` converts to half-open (`stop - base + 1`)
because that is what slicing wants. A file occupies a contiguous line range in the assembled
content, so its base is the content offset of its first line, and a local offset is `global - base`
— valid across a multi-line span.

### An edit

`_Edit(source, start, stop, replacement, tidy)`, with two degenerate forms that carry meaning:

- `start == stop` — an **insertion** (a zero-width splice). Insertions never subsume and are never
  subsumed by `_resolve_overlaps`.
- `replacement == ""` with `tidy=True` — a **deletion that cleans up after itself**. `_tidy_cut`
  widens the span to swallow the trailing `;`, the rest of the line through its newline, and the
  leading indentation, then collapses a blank line left dangling below an already-blank line above.
  `_tidy_cut_case` is its comma-list twin: a case eats its *following* comma, or — when it is the
  list's last case — the *preceding* one.

`_apply` sorts by `(start, stop)` descending and splices, so earlier edits' offsets stay valid.

### Rendering, where it is unavoidable

Three things have no source text to patch, and only these three are rendered:

- **an added field** — `_render_field_line` builds a throwaway one-field struct, runs the
  **binding's own** DSM renderer (`DSMDefinitions.from_definitions(...).to_dsm()`) and lifts the
  member line out. Literal formatting (floats, uuids, containers) is therefore the runtime's, not
  ours — the same reason invariant #1 exists, applied to values;
- **a docstring** — `_render_doc` (`"""…"""`, multi-line aware), re-indented to the anchor by
  `_reindent`;
- **a fresh `namespace Y {uuid} { … };` block** — when a `move_type`'s target namespace does not
  yet exist in the tree (§6).

Indentation for an inserted member is taken from an existing sibling (`_insert_before_close`), not
from a constant, so the tool adopts the file's style rather than imposing one.

---

## 6 · The directives, family by family

`_derive`, paragraph by paragraph, in its (load-bearing — invariant #6) order. `_index` first
builds the three lookups the whole function uses: declarations by `NS::Name` (types **and**
attachments), fields by `(struct repr, name)`, cases by `(enum repr, name)`.

**Type rename** — patch the declaration's `name_span`. Its references are *not* handled here.

**The unified reference pass** — one loop over `source_map.references()` handling three directives
at once: a type rename (the simple name), a namespace rename (the prefix), and a `move_type` (both,
always qualified). Qualification mirrors the source (invariant #4). Untouched referents — including
every primitive — are skipped, so the pass costs nothing on a small edit. This single loop is what
carries a rename into a **function-pool signature**, which the digest cannot check but the re-parse
can (§3). DSM declares two kinds of pool and this pass covers both: `function_pool` (stateless) and
`attachment_function_pool` (stateful — the name is a code-generation contract, meaning the generated
function takes `AttachmentGetting`, or `AttachmentMutating` under `mutable`, as an implicit first
parameter; it binds no persistence attachment, so no attachment directive reaches a pool). Neither
the pool header nor `mutable` is ever edited, so a migration cannot alter that contract.

Two pool failure modes exist, and they are different in kind — which decides *where* each is
caught. A **dropped** type leaves a signature naming nothing: that is membership of a name in a
set, so `_refuse_dangling_pools` answers it **up front**, before any edit, walking the parsed DSM
model (`_pool_findings` over both pool kinds, `_signature_type_names` down through containers), and
refuses with every site accumulated. A **renamed** type can instead make a bare signature reference
*ambiguous* (two namespaces now offer the same simple name): that is a property of the whole
patched tree, answerable only by resolving it — the parser's job at the verify re-parse, which
reports it sited and with its candidates. Do not try to pre-compute the second; it would mean
re-implementing the inspector.

A `transform_type` is the third case and is neither: the signature is rewritten to the new type,
which is what was asked, so it is **notified** (`on_notice`, printed by the CLI) rather than
refused — a pool's API changed silently, and the author should know.

**`transform_type`** — a *global* type substitution. The directive keys its source by `runtimeId`
(the engine's storage key) and records the source type's `representation()` beside it
(`transformed_type_names`) — that name is what this layer matches on, and every **occurrence** in
`source_map.types()` carrying it is replaced. Keeping the name at the directive is what lets the
substitution reach a type the *schema* does not hold: a composite used only in a function-pool
signature is in no `Definitions`, so no walk over the definitions could have found it. A composite
occurrence spans the whole expression, so a nested match lands inside an outer replacement —
overlap resolution keeps the outer one (invariant #3). A *named* source type is hooked away by the
engine, so its declaration is cut.

**Field / case rename** — the member's `name_span`. **Attachment rename** — the declaration's, and
nothing else (invariant #7).

**Type change** — the union of `retyped_fields`, `transformed_fields`, `resized_fields`,
`transposed_fields`: one pass, one source of truth (invariant #1). The target field is looked up
under its *renamed* name, since a rename may be in flight in the same migration. A second edit
follows the first: when the engine's target field carries **no** default, the `= <literal>` tail is
cut (the span from the name's end to the declaration's end) — a default was authored against the
old type, so the engine does not carry it onto a type-changed field, and the text must say the same
thing the definition does.

**Namespace rename / remap** — patch `name_span` / `uuid_span` at **every** occurrence in
`source_map.name_spaces()`: one namespace may be re-opened in several files, and all of them must
agree or the tree stops assembling.

**Documentation** — replace an existing docstring span, or (when there is none) insert one at the
anchor declaration's line start, re-indented. `text=""` clears, via a `tidy` deletion. Remember the
digest is blind here (§3).

**Add a field / add a case** — `_insert_before_close` splices before the block's closing brace, at
the sibling indentation; a case joins the comma list after the last existing case.

**Remove a case** — the comma-aware cut. **Drop a type / an attachment / a field** — a `tidy`
deletion of the whole block or declaration.

**Reorder fields / cases** — the first of the two region rewrites. The member region
`[min start, max end]` is replaced wholesale by the target permutation: each surviving member's
text is **baked** (invariant #5), dropped members are omitted, added members are rendered in place,
and the superseded edits — everything inside the region, plus the add-member insertions past it —
are removed from the list. A `order` that is not a permutation of the resulting member set raises
`ValueError` with the expected set: the failure is the *user's* directive being incoherent, and it
is worth failing loudly rather than producing a plausible tree the digest would then reject.

**`move_type` / `move_attachment`** — the second region rewrite, and the most intricate. The
declaration's text (docstring included, own edits baked) is cut from its namespace block and
spliced into the target:

- into a **live** block for that namespace if one exists — preferring one in the same file — just
  before its closing brace, found by `_match_brace`, which counts braces while **skipping string
  and docstring bodies** (a `"has { brace"` default must not throw off the depth; a `{uuid}` is
  self-balancing and needs no care);
- otherwise a **fresh** `namespace Y {uuid} { … };` block appended to the declaration's own file.
  Two adjacent blocks for one namespace simply re-open it — valid DSM.

One extra pass earns its keep here: a reference *inside* the moved declaration to a type that
stays behind (an attachment's key concept, say) would dangle once the text lands in `Y`, because a
bare name no longer resolves there. Those are qualified on the way out — the reference pass above
only touched *renamed or moved* referents, and an unchanged staying sibling is neither.

---

## 7 · Extension points

### Adding a directive

The engine work comes first (see REWRITE.md §7); then, here:

1. **Decide the position**: which span in the source-map names the thing you edit? If none does,
   you need a parser change (§8), not a codemod change.
2. **Add a paragraph to `_derive`**, respecting the order (invariant #6): before `reorder` unless
   it *is* a region rewrite.
3. **Never author type text** (invariant #1). If your directive changes a field's type, add it to
   the `type_changed` union and you are done.
4. **Check for span collisions** with the families that could name the same position (invariant #3).
5. **Test it** — and if the digest cannot see your change (§3), assert on the patched text.
6. If you cannot patch it yet, put it in `_UNSUPPORTED` with a reason. An honest refusal is a
   feature; a directive that silently no-ops and is caught later by a hex mismatch is not.

### Known edges

Scope limits, each one verified by running the tool, recorded so they are not rediscovered as
bugs. They sort by kind, and the kind is what tells you how much to worry.

#### Silently incomplete

- **A legacy local-name directive on an ambiguous attachment reaches nothing here.** An
  attachment's name is qualified by its **key concept**, not just its namespace: its identity is
  `identifier()`, `NS::KeyConcept.name`, so one namespace may legitimately hold
  `attachment<Customer, …> orders` *and* `attachment<Vendor, …> orders`. Addressed by identifier,
  both are patched exactly — `_index` keys declarations on the source map's own `identifier()`.
  Addressed by the bare local name (the legacy key the engine still accepts), the directive is
  ambiguous: the engine renames **every** homonym while this layer cannot choose one, so
  `att_repr` drops the ambiguous key and the digest refuses. Name the identifier.
- **A directive naming a type or member that does not exist** (a typo) is a **silent no-op**: the
  engine ignores it, so the target digest is unchanged, the codemod patches nothing, and `verify`
  passes with a success message. Shared with the data migration — the directive language has no
  "did you mean" — but it bites harder here, where the user's evidence of success is a diff.

#### Cosmetic — deliberate

- **Two moves into the same absent namespace produce two adjacent blocks** (in reverse derivation
  order — both are zero-width insertions at the file end). Re-opening a namespace is valid DSM and
  the digest agrees; grouping the moves by target namespace before emitting would produce one
  block.
- **A declaration cut from its namespace can leave an empty `namespace N { … };` block behind.**
  Valid DSM, and the intended behaviour: the codemod removes what it was asked to remove and does
  not tidy the neighbourhood.
- **`--force` does not clean `out_dir`**: files from a previous run survive alongside the new ones.

---

## 8 · The Viper boundary

Like the engine, this tool is **pure Python over the `dsviper` binding**, and the same rule holds:
if a change seems to need new runtime behaviour, it belongs in the runtime.

What it needs from the binding, beyond the engine's requirements:

- **`DSMSourceMap`** — the parser by-product: spans for declarations, fields, cases, namespaces,
  docstrings, resolved references and type occurrences, plus each declaration's `identifier()` —
  its identity in the parser's own terms, which is what keeps this layer from re-deriving one by
  string surgery (invariant #2). This is *newer than the package's shipped
  floor*, which is why `tests/test_definitions_migrate.py` **live-probes** the installed binding
  (`hasattr(V, "DSMSourceMap")`) and skips cleanly where it is absent — the suite documents the
  contract without breaking on an older peer. Keep that probe on any new test here.
- **`DSMBuilder`** — assembly (`append` / `content` / `part`) and `parse(source_map=…)`.
- **`DSMDefinitions.from_definitions(...).to_dsm()`** — the renderer, used *only* for the three
  unavoidable renderings of §5.
- **`Definitions.hexdigest()`** — the structural oracle, with the blindnesses catalogued in §3.

A missing span is the one thing that cannot be worked around here: locating the position by text
search would break invariant #2. The right fix is to extend the source-map in the parser.

---

## Appendix — a worked trace

The awkward case, because it exercises invariants #4, #5 and #6 at once: a type **moved** to
another namespace, **renamed**, and with a field **retyped**, while a sibling stays behind.

```python
d.move_type("Shop::Order", archive_ns)             # Shop::Order -> Archive::Order
d.rename_type("Shop::Order", "Shop::Ticket")       # ... and renamed (only the simple name is
                                                   #     read — the move carries the namespace)
d.retype_field("Shop::Order", "quantity", Type.UINT64)
```

Source (one file), with `Customer` staying in `Shop`:

```dsm
namespace Shop {1111…} {
concept Customer;
struct Order {
    key<Customer> buyer;
    uint32 quantity;
};
};
```

`_derive` runs in order:

1. **Type rename** — an edit on `Order`'s `name_span` → `Ticket`.
2. **Reference pass** — no *external* reference to `Shop::Order` here; `key<Customer>` is
   untouched (`Customer` is neither renamed, moved, nor in a renamed namespace).
3. **Type change** — an edit on `quantity`'s `type_span` → `uint64 `, the text coming from
   `type_map` (invariant #1), not from the directive.
4. **Move** (last — invariant #6). The declaration's extent is cut, and the two edits above,
   falling inside it, are **baked** into the carried text and removed from the list (invariant #5).
   The extra qualification pass then notices `Customer` resolves to `Shop`, which is *not* the move
   target, and qualifies it — a bare `Customer` would dangle in `Archive`. `Archive` has no block
   in the tree, so a fresh one is appended.

Result:

```dsm
namespace Shop {1111…} {
concept Customer;
};

namespace Archive {2222…} {

struct Ticket {
    key<Shop::Customer> buyer;
    uint64 quantity;
};

};
```

**Verify** re-parses this in memory: it resolves (so the qualification was necessary and
sufficient), and its digest equals the engine's target (so the rename, the move and the retype all
landed). Only then is it written. Note what the digest did *not* check, and what the test must
therefore assert itself: that `concept Customer;` is still in its original file, unmoved and
uncommented — the whole point of a codemod.
