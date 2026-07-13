"""The edit script — the source of truth for a document rewrite.

`TransformationDirectives` is a *declarative* description of a schema change (the
Django-migrations model, not imperative code): renames, shape changes, and the
policies that govern the lossy operations. Pure data — it holds strings, target
`Type`s and default `Value`s, and is consumed by `DefinitionsTransformer`.

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
        self.collision_policy = "fail"  # Map key collision: "fail" | "first" | "last"

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
    def rename_namespace(self, old_ns, new_name):    # name → new representations, same ids
        self.namespace_names[old_ns.uuid().representation()] = new_name

    def remap_namespace(self, old_ns, new_uuid):     # UUID → new runtimeIds, same representations
        self.namespace_uuids[old_ns.uuid().representation()] = new_uuid

    # -- struct field shape changes (family 2) --------------------------------
    def add_field(self, struct_repr, name, default_value):
        self.added_fields.setdefault(struct_repr, []).append((name, default_value))

    def drop_field(self, struct_repr, name):
        self.dropped_fields.setdefault(struct_repr, set()).add(name)

    def reorder_fields(self, struct_repr, order):   # target field names, in order
        self.field_order[struct_repr] = list(order)

    def retype_field(self, struct_repr, name, new_type, policy=None):
        # policy (for lossy retypes): "fail" (default) | "saturate" | ("default", V)
        self.retyped_fields.setdefault(struct_repr, {})[name] = (new_type, policy)

    # -- enum case shape changes (family 2) -----------------------------------
    def add_case(self, enum_repr, name):            # Class A — appended at end
        self.added_cases.setdefault(enum_repr, []).append(name)

    def reorder_cases(self, enum_repr, order):      # target case names, in order
        self.case_order[enum_repr] = list(order)

    def remove_case(self, enum_repr, case, policy):
        # policy: "fail" (default) | ("map-case", name) | "drop-record"
        self.removed_cases.setdefault(enum_repr, {})[case] = policy

    # -- maps -----------------------------------------------------------------
    def resolve_collisions(self, winner):           # "fail" | "first" | "last"
        self.collision_policy = winner
