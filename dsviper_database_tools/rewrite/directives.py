"""The edit script — the source of truth for a document rewrite.

`TransformationDirectives` is a *declarative* description of a schema change (the
Django-migrations model, not imperative code): renames, shape changes, and the
policies that govern the lossy operations. Pure data — it holds strings, target
`Type`s and default `Value`s, and is consumed by `DefinitionsRewriter`.

FQN arguments are qualified-name strings (`representation()`, e.g. "Shop::Customer").
"""


class TransformationDirectives:
    def __init__(self):
        self.type_renames = {}          # src repr -> tgt repr           (family 1)
        self.field_renames = {}         # src struct repr -> {src field -> tgt field}
        self.case_renames = {}          # src enum repr  -> {src case  -> tgt case}
        self.dropped_fields = {}        # src struct repr -> set(field)   (family 2)
        self.retyped_fields = {}        # src struct repr -> {src field -> (new Type, policy)}
        self.added_fields = {}          # src struct repr -> [(name, default Value)]
        self.removed_cases = {}         # src enum repr -> {case -> policy}
        self.attachment_renames = {}    # src identifier -> new identifier
        self.added_cases = {}           # src enum repr -> [names]   (Class A, at end)
        self.case_order = {}            # src enum repr -> [target names in order]
        self.field_order = {}           # src struct repr -> [target names in order]
        self.namespace_names = {}       # src ns uuid repr -> new display name (representation only)
        self.namespace_uuids = {}       # src ns uuid repr -> new ValueUUId    (runtimeId only)
        self.type_namespaces = {}       # type repr -> target NameSpace   (per-definition move; split/merge)
        self.attachment_namespaces = {} # attachment identifier -> target NameSpace
        self.collision_policy = "fail"  # Map key collision: "fail" | "first" | "last"
        self.document_drops_accepted = False   # explicit sign-off that drop-record may DELETE documents
        self.resized_fields = {}        # src struct repr -> {field -> (kind, dims, fill, on_shrink)}
        self.transposed_fields = {}     # src struct repr -> set(field)   (Mat<c,r> -> Mat<r,c>)
        self.transformed_fields = {}    # src struct repr -> {field -> (new_type, fn)}  (Class-C hook)
        self.transformed_types = {}     # src type runtimeId repr -> (new_type, fn)  (global Class-C hook)
        self.transformed_type_names = {}  # ... -> the source type's representation, kept alongside:
                                        # a runtimeId is a fingerprint, so a name-based consumer (a
                                        # source codemod) could otherwise only recover the name by
                                        # walking the schema — and would miss a type the schema does
                                        # not reach (a composite used only in a pool signature).
        # documentation authoring (Class A — doc is outside the runtimeId; overrides the
        # source doc the build carries by default). Members named by SOURCE name.
        self.type_docs = {}             # type repr (struct/enum/concept/club) -> text
        self.field_docs = {}            # src struct repr -> {src field -> text}
        self.case_docs = {}             # src enum repr  -> {src case  -> text}
        self.attachment_docs = {}       # src attachment identifier -> text
        # definition-level drops (the co-direction of the additive build)
        self.dropped_types = set()      # type repr (struct/enum/concept/club) to NOT recreate
        self.dropped_attachments = set()   # attachment identifier to NOT recreate (+ delete its docs)
        self.attachment_drops_accepted = False   # sign-off that drop_attachment may DELETE documents

    # -- renames (family 1, size-preserving; no data policy) ------------------
    def rename_type(self, old, new):
        self.type_renames[old] = new

    def rename_field(self, struct_repr, old, new):
        self.field_renames.setdefault(struct_repr, {})[old] = new

    def rename_case(self, enum_repr, old, new):
        self.case_renames.setdefault(enum_repr, {})[old] = new

    def rename_attachment(self, old_id, new_id):    # an attachment is a named Map<Key,Doc>
        self.attachment_renames[old_id] = new_id

    # -- a namespace has two orthogonal axes: its NAME drives the human
    #    representation (`Namespace::Type`), its UUID drives every type's runtimeId.
    #    `rename_namespace`/`remap_namespace` act on a WHOLE namespace (all its definitions,
    #    uniformly); `move_type`/`move_attachment` reassign a SINGLE definition's namespace —
    #    together they express the n:m namespace algebra (split = move some out; merge = map/move
    #    into a shared namespace). A definition's namespace is part of its runtimeId, so a move
    #    is a lossless re-id (Class A, like a rename); references follow via the mapping.
    def rename_namespace(self, old_ns, new_name):    # name → new representations, same ids
        self.namespace_names[old_ns.uuid().representation()] = new_name

    def remap_namespace(self, old_ns, new_uuid):     # UUID → new runtimeIds, same representations
        self.namespace_uuids[old_ns.uuid().representation()] = new_uuid

    def move_type(self, type_repr, target_ns):       # struct/enum/concept/club -> target NameSpace
        self.type_namespaces[type_repr] = target_ns

    def move_attachment(self, identifier, target_ns):
        self.attachment_namespaces[identifier] = target_ns

    # -- struct field shape changes (family 2) --------------------------------
    def add_field(self, struct_repr, name, default_or_type, derive=None):
        # `derive` None: `default_or_type` is a Value — a static default (domain-free).
        # `derive` given: `default_or_type` is the target Type, and the field is a Class-C
        # DERIVED field — `derive(source_struct, field_name, target_type) -> value` computes it
        # from the source struct (its siblings). Same contract/validation as `transform_field`.
        self.added_fields.setdefault(struct_repr, []).append((name, default_or_type, derive))

    def drop_field(self, struct_repr, name):
        self.dropped_fields.setdefault(struct_repr, set()).add(name)

    # -- definition-level drops (the co-direction of the additive build) ------
    #    A key is a concept-instance identity, not a foreign key, and nothing references an
    #    attachment — so dropping an attachment dangles nothing (only mass-deletes its docs,
    #    hence the acknowledgement gate). Dropping a TYPE can dangle a surviving reference; the
    #    build refuses that up front with an accumulated report (never a silently broken target).
    def drop_type(self, type_repr):
        # struct / enum / concept / club — addressed uniformly by qualified name.
        self.dropped_types.add(type_repr)

    def drop_attachment(self, identifier):
        self.dropped_attachments.add(identifier)

    def accept_attachment_drops(self):
        """Acknowledge that this migration may **delete whole attachments** — every document
        of a dropped attachment is gone. A deliberate, separate act (like `accept_document_drops`
        for `drop-record`), not an implicit consequence. Enforced by the `Database` migrate loop;
        `dry_run` informs without it (identify → inform → acknowledge → decide)."""
        self.attachment_drops_accepted = True

    def reorder_fields(self, struct_repr, order):   # target field names, in order
        self.field_order[struct_repr] = list(order)

    def retype_field(self, struct_repr, name, new_type, policy=None):
        # policy (for lossy retypes): "fail" (default) | "saturate" | ("default", V)
        self.retyped_fields.setdefault(struct_repr, {})[name] = (new_type, policy)

    # -- Vec/Mat DIMENSION changes (family 2). Named explicitly, never inferred from the
    #    target type — a target Mat<3,2> cannot say resize vs transpose vs (ambiguous)
    #    reshape. The target type is DERIVED (the element type T is read from the source);
    #    the field must be a *direct* Vec/Mat (a nested Vec/Mat, e.g. Vector<Vec<T,n>>, is
    #    not yet addressable — that needs a type-path vocabulary). Position-preserving
    #    (`[i]->[i]`, `[i,j]->[i,j]`): grow fills the new cells, shrink drops the trailing ones.
    def resize_vec_field(self, struct_repr, field, size, *, fill="zero", on_shrink="fail"):
        # fill: "zero" (born-default) | a numeric scalar. on_shrink: "fail" | "accept".
        self.resized_fields.setdefault(struct_repr, {})[field] = ("vec", (size,), fill, on_shrink)

    def resize_mat_field(self, struct_repr, field, columns, rows, *, fill="identity", on_shrink="fail"):
        # fill: "identity" (born-default: extend the diagonal with 1) | "zero" | a numeric
        #       scalar. on_shrink: "fail" | "accept" (accept the dropped rows/columns).
        self.resized_fields.setdefault(struct_repr, {})[field] = ("mat", (columns, rows), fill, on_shrink)

    def transpose_mat_field(self, struct_repr, field):
        # Mat<c,r> -> Mat<r,c>, [i,j] -> [j,i]. Lossless; the target shape is derived.
        self.transposed_fields.setdefault(struct_repr, set()).add(field)

    # -- Class-C custom transform (a user hook) -------------------------------
    def transform_field(self, struct_repr, field, new_type, fn):
        # The escape hatch for a change no declarative directive expresses (e.g. a field
        # retyped to an UNRELATED type). `new_type` names the target type (in the source
        # domain — the engine maps it); `fn(source_value, target_type) -> target_value` is the
        # author's transform. It owns its loss model: it returns a valid target value (the
        # engine validates it), raises `Unrepresentable` to drop the record, or raises to
        # refuse. The engine refuses anything the hook does not produce as a valid target value.
        self.transformed_fields.setdefault(struct_repr, {})[field] = (new_type, fn)

    def transform_type(self, source_type, new_type, fn):
        # The GLOBAL hook: transform EVERY occurrence of `source_type` (wherever it appears —
        # a field, a container element, a variant arm, nested) to `new_type`, in one directive.
        # Rides the target-directed recursion (the walk visits every node). A field-level
        # `transform_field` on the same position OVERRIDES this (resolution: field > type).
        # Same contract as `transform_field`: `fn(source_value, target_type) -> target_value`.
        rid = source_type.runtime_id().representation()
        self.transformed_types[rid] = (new_type, fn)
        self.transformed_type_names[rid] = source_type.representation()

    # -- enum case shape changes (family 2) -----------------------------------
    def add_case(self, enum_repr, name):            # Class A — appended at end
        self.added_cases.setdefault(enum_repr, []).append(name)

    def reorder_cases(self, enum_repr, order):      # target case names, in order
        self.case_order[enum_repr] = list(order)

    def remove_case(self, enum_repr, case, policy):
        # policy: "fail" (default) | ("map-case", name) | "drop-record"
        self.removed_cases.setdefault(enum_repr, {})[case] = policy

    # -- documentation authoring (Class A; overrides the carried source doc) ---
    #    Documentation is metadata OUTSIDE the runtimeId (a doc change never re-ids/re-keys),
    #    so this is lossless authoring, no policy. The build carries the source doc by default;
    #    these set/override it. Members named by SOURCE name (as renames do); `text=""` clears.
    def document_type(self, type_repr, text):
        # struct / enum / concept / club — all addressed uniformly by qualified name.
        self.type_docs[type_repr] = text

    def document_field(self, struct_repr, field, text):
        self.field_docs.setdefault(struct_repr, {})[field] = text

    def document_case(self, enum_repr, case, text):
        self.case_docs.setdefault(enum_repr, {})[case] = text

    def document_attachment(self, attachment_id, text):
        self.attachment_docs[attachment_id] = text

    # -- maps -----------------------------------------------------------------
    def resolve_collisions(self, winner):           # "fail" | "first" | "last"
        self.collision_policy = winner

    # -- explicit sign-off for record-scoped loss -----------------------------
    def accept_document_drops(self):
        """Acknowledge that this migration may **delete whole documents**. Every
        `drop-record` policy is *record-scoped*: when a value has no target image it elides
        the enclosing document, rather than losing a bounded field (the value-closed
        policies `saturate` / `("default", v)` / `("map-case", n)`). Because that
        consequence is categorically graver, a `Database` migration **refuses** any
        `drop-record` until this explicit act — dropping a document must be a deliberate,
        acknowledged decision, not a field policy that reads like `saturate`.

        Run `migrate_database.dry_run` first: it deliberately does *not* require this
        acknowledgement, so it can show exactly how many / which documents would be dropped.
        Sign off here once the count is understood. (A `CommitDatabase` migration refuses
        `drop-record` outright — it rewrites opcode-carried values with no document record —
        regardless of this flag.)"""
        self.document_drops_accepted = True

    # -- introspection --------------------------------------------------------
    def drop_record_sites(self):
        """Every target (`Struct.field` retype, `Enum::case` removal) that decrees a
        `drop-record` policy. `drop-record` is **record-scoped** — it elides the enclosing
        document — so a consumer with no document to drop (a `CommitDatabase` migration,
        which rewrites opcode-carried values) refuses it up front using this list."""
        sites = [f"{s}.{f}" for s, fields in self.retyped_fields.items()
                 for f, (_t, p) in fields.items() if p == "drop-record"]
        sites += [f"{e}::{c}" for e, cases in self.removed_cases.items()
                  for c, p in cases.items() if p == "drop-record"]
        return sites
