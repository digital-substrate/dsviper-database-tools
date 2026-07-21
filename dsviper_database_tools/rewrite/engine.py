"""Definitions-directed document rewriting — the engine.

`DefinitionsRewriter.from_directives(source_defs, directives)` builds the target
`Definitions` from the source + the edit script, and rewrites any value from the
source domain to the target domain with a single TARGET-DIRECTED engine (`value()`)
that spans both transformation families:

  * family 1 — renames (size-preserving): the value is re-stamped, ids follow.
  * family 2 — shape changes: the walk is driven by the *target* type (drop absent,
    seed added defaults, convert leaves), with a decreed policy on every lossy op.

It is a composition of already-bound runtime atoms — no C++, the `dsviper` runtime
untouched. See `REWRITE.md` for the algorithm, the invariants, and where each is enforced.
"""

import functools
import inspect
import math

import dsviper as V


@functools.lru_cache(maxsize=None)
def _hook_param_count(fn):
    """How many parameters a hook declares — decides whether it gets the `ctx` (an extra
    trailing slot). A local hook stays clean; a non-local one opts in by declaring it."""
    try:
        return len(inspect.signature(fn).parameters)
    except (ValueError, TypeError):
        return 0


class _HookContext:
    """Handed to a hook that declares a slot for it. Bundles the read-only **source view**
    (`attachment_getting`, `None` unless a store loop wired one) and a **re-entrant** rewrite
    capability, for non-local (cross-document) Class-C derivation. The engine never fetches —
    it forwards this; the hook uses it. `rewrite(value, follow_refs=False)` applies the SAME
    migration to a fetched source value; `follow_refs=False` (default) blanks the source view
    for the nested call, so its hooks cannot fetch further — bounding depth and breaking any
    cross-document reference cycle."""
    __slots__ = ("_rw",)

    def __init__(self, rewriter):
        self._rw = rewriter

    @property
    def has_source_view(self):
        return self._rw._source_view is not None

    @property
    def attachment_getting(self):
        if self._rw._source_view is None:
            raise ValueError("[hook] no source view is wired — this migration reads another "
                             "document, but was run without one. Run it through "
                             "migrate_database.migrate / dry_run (which wires the source), or "
                             "guard with `ctx.has_source_view`.")
        return self._rw._source_view

    @property
    def has_self_key(self):
        return self._rw._self_key is not None

    @property
    def self_key(self):
        """The **source** key of the document currently being rewritten — the record's own
        identity, stable across the whole walk of that document. Lets an aggregate hook match
        *incoming* references (`sum(order.amount where order.custRef == self_key)`) — the one
        thing the source view alone cannot supply (it is keyed *by* the key, and the document
        value does not carry its own key). `None` (raises here) outside a store loop, and
        inside a re-entrant `ctx.rewrite` (a fetched value has no well-defined self)."""
        if self._rw._self_key is None:
            raise ValueError("[hook] no self key is wired — either this migration was not run "
                             "through a store loop (migrate / dry_run), or the hook fired "
                             "inside a re-entrant ctx.rewrite (a fetched value has no self). "
                             "Guard with `ctx.has_self_key`.")
        return self._rw._self_key

    def rewrite(self, value, target_type=None, *, follow_refs=False):
        rw = self._rw
        if follow_refs:
            return rw.value(value, target_type)
        saved_view, saved_key = rw._source_view, rw._self_key
        rw._source_view = None
        rw._self_key = None                # a fetched value has no well-defined self identity
        try:
            return rw.value(value, target_type)
        finally:
            rw._source_view, rw._self_key = saved_view, saved_key

# representationally-lossless leaf widenings — automatic (Class A)
WIDENING = {
    ("int8", "int16"), ("int8", "int32"), ("int8", "int64"),
    ("int16", "int32"), ("int16", "int64"), ("int32", "int64"),
    ("int32", "double"), ("int16", "double"), ("int8", "double"),
    ("uint8", "uint16"), ("uint8", "uint32"), ("uint8", "uint64"),
    ("uint16", "uint32"), ("uint16", "uint64"), ("uint32", "uint64"),
    ("float", "double"),
}
PRIMITIVES = {"void", "bool", "uint8", "uint16", "uint32", "uint64",
              "int8", "int16", "int32", "int64", "float", "double",
              "blob_id", "commit_id", "uuid", "string", "blob", "vec", "mat"}
INT_RANGE = {
    "int8": (-128, 127), "int16": (-2**15, 2**15 - 1),
    "int32": (-2**31, 2**31 - 1), "int64": (-2**63, 2**63 - 1),
    "uint8": (0, 255), "uint16": (0, 2**16 - 1),
    "uint32": (0, 2**32 - 1), "uint64": (0, 2**64 - 1),
}
FLOATS = {"float", "double"}
# composite (non-scalar) kinds. A retype among these is expressed by a structural branch in
# `_retype`; one that reaches the scalar-leaf tail unhandled fails CLOSED (invariant #1) rather
# than crashing in the numeric path (which assumes a scalar operand).
COMPOSITES = {"struct", "enum", "concept", "club", "optional", "vector", "set", "map",
              "xarray", "tuple", "variant", "key", "any"}


def _vecmat_retype_class(src_type, new_type):
    """Classify a Vec/Mat field retype. Returns `(class, detail)` — `"A"`, `"B"`, or
    `"refused"` — or `None` when neither side is Vec/Mat (an ordinary scalar/container
    retype for the caller). Three families are recognised:

    * **element conversion** (fixed dims): `Vec<T,n>→Vec<T',n>` / `Mat<T,c,r>→Mat<T',c,r>`
      widen (A) or narrow (B), the scalar leaf story vectorised over the block.
    * **the Vector bridge** (fixed ↔ variable, element type T preserved): flattening
      `Vec/Mat → Vector<T>` drops only the size/shape *constraint* (A, lossless — a `Mat`
      flattens in the runtime's native **column-major** order); un-flattening
      `Vector<T> → Vec<T,n>` must impose the fixed size (B — the runtime length is the
      offender surface, governed by a policy).
    * **refused**: a *dimension* change (needs an explicit resize/transpose directive),
      `Vec↔Mat`, `Vector→Mat` (both the length *and* the column-/row-major un-flatten are
      ambiguous — the runtime itself rejects a flat `Mat`), and any bridge that also changes
      the element type (T is preserved across the bridge; convert it at fixed shape instead)."""
    sc, tc = src_type.type_code(), new_type.type_code()
    if sc not in ("vec", "mat") and tc not in ("vec", "mat"):
        return None

    # -- the Vector bridge: fixed ↔ variable, element type T preserved --------
    if sc in ("vec", "mat") and tc == "vector":                 # flatten → Vector (A)
        se = (V.TypeVec.cast(src_type) if sc == "vec" else V.TypeMat.cast(src_type)).element_type()
        te = V.TypeVector.cast(new_type).element_type()
        if se.representation() != te.representation():
            return ("refused", f"{sc}→Vector with an element-type change "
                    f"({se.representation()}→{te.representation()}) — a flatten preserves T; "
                    f"convert the element type at fixed shape instead")
        order = " (column-major)" if sc == "mat" else ""
        return ("A", f"{sc}→Vector flatten{order} (lossless — the fixed-size constraint is dropped)")
    if sc == "vector" and tc == "mat":                          # un-flatten → Mat (refused)
        return ("refused", "Vector→Mat un-flatten — the length and the column-major/row-major "
                "layout are both ambiguous (the runtime itself rejects a flat Mat); build the "
                "columns explicitly, or route it through a reshape hook")
    if sc == "vector" and tc == "vec":                          # length-fit → Vec (B)
        se = V.TypeVector.cast(src_type).element_type()
        te = V.TypeVec.cast(new_type).element_type()
        if se.representation() != te.representation():
            return ("refused", f"Vector→Vec with an element-type change "
                    f"({se.representation()}→{te.representation()}) — a length-fit preserves T")
        return ("B", "Vector→Vec length-fit (the runtime length must equal the fixed size)")

    # -- same-kind element conversion, fixed dimensions -----------------------
    if sc == "vec" and tc == "vec":
        sv, tv = V.TypeVec.cast(src_type), V.TypeVec.cast(new_type)
        if sv.size() != tv.size():
            return ("refused", f"Vec size {sv.size()}→{tv.size()} — a dimension change needs a "
                    f"resize directive (element widening/narrowing at fixed size is supported)")
        se, te = sv.element_type().type_code(), tv.element_type().type_code()
    elif sc == "mat" and tc == "mat":
        sm, tm = V.TypeMat.cast(src_type), V.TypeMat.cast(new_type)
        if (sm.columns(), sm.rows()) != (tm.columns(), tm.rows()):
            return ("refused", f"Mat {sm.columns()}×{sm.rows()}→{tm.columns()}×{tm.rows()} — a "
                    f"dimension change needs a resize/transpose directive")
        se, te = sm.element_type().type_code(), tm.element_type().type_code()
    else:
        return ("refused", f"{sc}→{tc} — a Vec↔Mat / Vec↔scalar conversion is not supported")
    if se == te:
        return ("A", f"Vec/Mat identity ({se})")                # same element type: nothing to convert
    if (se, te) in WIDENING:
        return ("A", f"Vec/Mat element widening {se}→{te} (lossless)")
    return ("B", f"Vec/Mat element narrowing {se}→{te}")


def _container_element_retype_class(src_type, new_type):
    """Class of a **same-kind element retype** — `set/vector/xarray/map` (containers) or `optional`
    (the multiplicity-1 holder) whose element (and, for a map, key) type changes: widen / format /
    same-kind → **A** (lossless, automatic), a narrowing element → **B** (needs a policy). Recurses
    for nested containers/holders; a `map` weighs both key and value (either narrowing ⇒ B). Returns
    `"A"` / `"B"`, or `None` if `src_type → new_type` is not a same-kind element retype (a *shape*
    change like `set→vector` or `Optional<A>→A`, or a leaf, is classified elsewhere)."""
    sc, tc = src_type.type_code(), new_type.type_code()
    if sc != tc or sc not in ("set", "vector", "xarray", "map", "optional", "tuple"):
        return None

    def elem_class(se, te):
        nested = _container_element_retype_class(se, te)        # nested container → recurse
        if nested is not None:
            return nested
        s, t = se.type_code(), te.type_code()
        if s == t or (s, t) in WIDENING or t == "string":       # same / widen / format → lossless
            return "A"
        return "B"                                              # a narrowing (or parse) leaf

    if sc == "map":
        sm, tm = V.TypeMap.cast(src_type), V.TypeMap.cast(new_type)
        return "B" if "B" in (elem_class(sm.key_type(), tm.key_type()),
                              elem_class(sm.element_type(), tm.element_type())) else "A"
    if sc == "tuple":                                          # fixed arity — classify every position
        st, ts = V.TypeTuple.cast(src_type).types(), V.TypeTuple.cast(new_type).types()
        if len(st) != len(ts):
            return None                                        # an arity change is a shape change, not this
        return "B" if any(elem_class(a, b) == "B" for a, b in zip(st, ts)) else "A"
    getter = {"set": V.TypeSet, "vector": V.TypeVector, "xarray": V.TypeXArray, "optional": V.TypeOptional}[sc]
    return elem_class(getter.cast(src_type).element_type(), getter.cast(new_type).element_type())


class Unrepresentable(Exception):
    """The source value has no faithful image in the target domain — a nil that
    cannot be unwrapped, a string that will not parse, a populated case that was
    removed — and the decreed `drop-record` policy elects to elide rather than abort.
    Names the engine's *condition*, not a consumer's response: the `Database` loop
    catches it to skip the enclosing document; other consumers may dispose of it
    differently (refuse, log-and-continue, ...)."""


def _const(defs):
    """Accept either a mutable `Definitions` or an already-const `DefinitionsConst`
    (as returned by `Database.definitions()`)."""
    return defs.const() if hasattr(defs, "const") else defs


def _sample(x):
    """A short, serialisable rendering of a value for a diagnostic sample. Accepts a
    `Value`, a native scalar, or `None` (for an elided/absent image)."""
    if x is None:
        return None
    if isinstance(x, str):                 # an already-rendered marker ("nil") or parsed string
        return x
    if hasattr(x, "representation"):
        return x.representation()
    return repr(x)


def _native(x):
    """The native scalar of a leaf `Value`, or `x` itself if already native."""
    return V.Value.dumps(x) if hasattr(x, "representation") else x


class DefinitionsRewriter:
    _commit_id_remap = None            # {src commit repr -> new ValueCommitId}, set during a DAG replay
    _sink = None                       # diagnostic sink: called with a finding dict each time a
                                       # Class-B policy actually bites; None = no observation
    _source_view = None                # read-only source `AttachmentGetting` for non-local hooks
                                       # (wired by the store loop); None = no cross-document reads
    _self_key = None                   # source key of the document being rewritten (record identity);
                                       # wired by the store loop per-document; None = no self identity

    # -- notify the diagnostic sink that a Class-B policy governed an OFFENDER (a value
    #    that would otherwise lose information): a narrowing/parse that saturated/defaulted,
    #    a nil/removed-case elided, a set/map member collapsed. Exact conversions (in range,
    #    parseable, non-nil) never emit — they are lossless by the engine's contract. Cheap
    #    no-op when unobserved: `before`/`after` are rendered only if a sink is attached.
    def _emit(self, op, site, policy, before, after):
        if self._sink is None:
            return
        self._sink({"site": site, "op": op, "policy": policy,
                    "before": _sample(before), "after": _sample(after)})

    # -- extend a diagnostic site path only while observing (keeps the migrate hot path
    #    free of string building). `[]`/`{}`/`<key>`/`<val>`/`.i` mark a nesting level so a
    #    loss INSIDE a collection element is attributed distinctly from the field itself.
    def _sub(self, site, suffix):
        return f"{site}{suffix}" if (self._sink is not None and site is not None) else site

    def __init__(self, source_defs, target_defs, directives):
        self.source = _const(source_defs)
        self.target = _const(target_defs)
        self.d = directives
        self.att_map = {}
        self._build_maps()
        self._shape_guard()
        self._policy_completeness()

    # -- build the target Definitions from source + directives (definitions ⇒
    #    definitions), then wire the rewriter against it. Returns (rewriter, target).
    @classmethod
    def from_directives(cls, source_defs, directives):
        target, tmap, att_map = build_target_definitions(source_defs, directives)
        self = cls.__new__(cls)
        self.source = _const(source_defs)
        self.target = target.const()
        self.d = directives
        self.type_map = dict(tmap)          # src rid repr -> target named Type
        self.att_map = dict(att_map)        # src attachment rid repr -> target Attachment
        # No shape guard here: the target was BUILT from the directives, so it is
        # consistent by construction. The strict family-1 guard (P2) applies to the
        # external-target __init__ path, where it validates a hand-authored target.
        self._policy_completeness()         # but Class-B policies are still required
        return self, target

    # -- id map M (name-matching + rename directives), keyed by source rid repr
    def _build_maps(self):
        self.type_map = {}
        groups = [
            (self.source.concepts(),     self.target.concepts()),
            (self.source.clubs(),        self.target.clubs()),
            (self.source.enumerations(), self.target.enumerations()),
            (self.source.structures(),   self.target.structures()),
        ]
        for src_list, tgt_list in groups:
            tgt_by_name = {t.representation(): t for t in tgt_list}
            for s in src_list:
                tgt_name = self.d.type_renames.get(s.representation(), s.representation())
                if tgt_name not in tgt_by_name:            # (P1) name completeness
                    raise KeyError(f"[P1] no target for {s.representation()} -> {tgt_name}")
                self.type_map[s.runtime_id().representation()] = tgt_by_name[tgt_name]

    def _map_named(self, t):
        return self.type_map[t.runtime_id().representation()]

    # -- arms of the SOURCE variant (mapped to the target domain) absent from the TARGET
    #    variant — i.e. removed. Membership is by runtimeId (the type identity, as the runtime
    #    itself dedups arms). A value on a removed arm has no target image (Class B); pure
    #    additions / reorder leave this empty (Class A). Returns the removed arms' reprs.
    def _variant_removed_arms(self, src_type, new_type):
        tgt_ids = {a.runtime_id().representation() for a in V.TypeVariant.cast(new_type).types()}
        return [a.representation() for a in V.TypeVariant.cast(src_type).types()
                if self.map_type(a).runtime_id().representation() not in tgt_ids]

    # -- (P2) shape invariance: every matched pair identical up to renames
    def _shape_guard(self):
        for s in self.source.structures():
            tgt = self._map_named(s)
            fren = self.d.field_renames.get(s.representation(), {})
            src_fields = s.fields()
            tgt_names = [f.name() for f in tgt.fields()]
            if len(src_fields) != len(tgt_names):
                raise ValueError(f"[P2] field count differs for {s.representation()} "
                                 f"— that is a family-2 shape change, not a rename")
            for i, f in enumerate(src_fields):
                expected = fren.get(f.name(), f.name())
                if tgt_names[i] != expected:
                    raise ValueError(f"[P2] field #{i} of {s.representation()}: "
                                     f"expected '{expected}', target has '{tgt_names[i]}' "
                                     f"— reorder/retype is family 2")
        for s in self.source.enumerations():
            tgt = self._map_named(s)
            cren = self.d.case_renames.get(s.representation(), {})
            src_cases = [c.name() for c in s.cases()]
            tgt_cases = [c.name() for c in tgt.cases()]
            if len(src_cases) != len(tgt_cases):
                raise ValueError(f"[P2] case count differs for {s.representation()}")
            for i, c in enumerate(src_cases):
                expected = cren.get(c, c)
                if tgt_cases[i] != expected:
                    raise ValueError(f"[P2] case #{i} of {s.representation()}: "
                                     f"expected '{expected}', target '{tgt_cases[i]}'")

    # -- (policy-completeness) refuse a lossy retype / removed case with no policy,
    #    at construction — before any data is touched (family-2 analog of P2)
    def _policy_completeness(self):
        for s in self.source.structures():
            retypes = self.d.retyped_fields.get(s.representation(), {})
            for fname, (new_type, policy) in retypes.items():
                src_type = s.check(fname).type()
                sc = src_type.type_code()
                tc = new_type.type_code()
                vm = _vecmat_retype_class(src_type, new_type)      # Vec/Mat element retype?
                if vm is not None:
                    kind, detail = vm
                    if kind == "refused":
                        raise ValueError(f"[unsupported] {s.representation()}.{fname}: {detail}")
                    if kind == "B" and policy is None:
                        raise ValueError(f"[policy-completeness] {detail} on "
                                         f"{s.representation()}.{fname} needs a decreed policy")
                    continue                                       # element widen (A) / policied narrow (B)
                ce = _container_element_retype_class(src_type, new_type)   # set/vector/xarray/map element retype?
                if ce is not None:
                    if ce == "B" and policy is None:
                        raise ValueError(f"[policy-completeness] {sc}->{tc} element narrowing on "
                                         f"{s.representation()}.{fname} needs a decreed policy")
                    continue                                       # element widen (A) / policied narrow (B)
                if sc == "variant" or tc == "variant":             # variant arm-set change
                    if sc != "variant" or tc != "variant":
                        raise ValueError(f"[unsupported] {s.representation()}.{fname}: {sc}->{tc} "
                                         f"— a variant<->non-variant retype is not supported")
                    removed = self._variant_removed_arms(src_type, new_type)
                    if removed and policy is None:
                        raise ValueError(f"[policy-completeness] variant arm removal "
                                         f"({', '.join(removed)}) on {s.representation()}.{fname} "
                                         f"needs a decreed policy — a value on a removed arm has "
                                         f"no target image")
                    continue                                       # add/reorder (A) / removal (B)
                # widen / format / set↔vector↔xarray container shape (total, nothing lost)
                class_a = ((sc, tc) in WIDENING or tc == "string"
                           or (sc == "set" and tc == "vector")
                           or (sc == "vector" and tc == "xarray")
                           or (sc == "xarray" and tc == "vector"))
                if not class_a and policy is None:
                    raise ValueError(f"[policy-completeness] {sc}->{tc} on "
                                     f"{s.representation()}.{fname} needs a decreed policy")
        for e in self.source.enumerations():
            for case, policy in self.d.removed_cases.get(e.representation(), {}).items():
                if policy is None:
                    raise ValueError(f"[policy-completeness] removed case "
                                     f"{e.representation()}.{case} needs a decreed policy")
        _validate_dimension_ops(self.source, self.d)   # resize/transpose: direct-Vec/Mat + fill

    # -- type ⇒ type
    def map_type(self, t):
        th = self.d.transformed_types.get(t.runtime_id().representation())
        if th is not None:                             # global hook: this type maps to new_type
            return self.map_type(th[0])
        tc = t.type_code()
        if tc in ("struct", "enum", "concept", "club"):
            return self._map_named(t)
        if tc == "optional":
            return V.TypeOptional(self.map_type(V.TypeOptional.cast(t).element_type()))
        if tc == "vector":
            return V.TypeVector(self.map_type(V.TypeVector.cast(t).element_type()))
        if tc == "set":
            return V.TypeSet(self.map_type(V.TypeSet.cast(t).element_type()))
        if tc == "map":
            m = V.TypeMap.cast(t)
            return V.TypeMap(self.map_type(m.key_type()), self.map_type(m.element_type()))
        if tc == "xarray":
            return V.TypeXArray(self.map_type(V.TypeXArray.cast(t).element_type()))
        if tc == "tuple":
            return V.TypeTuple([self.map_type(x) for x in V.TypeTuple.cast(t).types()])
        if tc == "variant":
            return V.TypeVariant([self.map_type(x) for x in V.TypeVariant.cast(t).types()])
        if tc == "key":
            return V.TypeKey(self.map_type(V.TypeKey.cast(t).element_type()))
        return t                        # primitives, Any, Vec, Mat, AnyConcept

    def _retype_element(self, elem, te, policy, esite):
        """Convert one container element to the target element type `te`. A differing-kind leaf
        goes through the policy-governed `_retype` (widen A / narrow B); a **nested container**
        whose type changed also goes through `_retype` (its element retype branch, so an inner
        narrowing is policied too); a same-kind composite or an identical type recurses via
        `value` (a plain rewrite / composite descent) — the same split as the Optional unwrap."""
        if elem.type_code() != te.type_code():
            return self._retype(elem, te, policy, esite)
        if (te.type_code() in ("set", "vector", "xarray", "map", "vec", "mat", "optional", "tuple")
                and elem.type().runtime_id().representation() != te.runtime_id().representation()):
            return self._retype(elem, te, policy, esite)
        return self.value(elem, te, esite)

    def _set_add(self, out, ne, site):
        """Add a converted element to a target set, guarding a non-injective collapse (two source
        elements mapping to one member) under `collision_policy` — shared by `value` and `_retype`."""
        if ne in out:                                      # Class B: element collapse
            if self.d.collision_policy == "fail":
                raise ValueError(f"[Class-B] set element collapse: {ne.representation()} — a "
                                 f"non-injective element migration would silently drop a member; "
                                 f"decree resolve_collisions('first'|'last')")
            self._emit("set-collapse", site, self.d.collision_policy, ne, None)
            return                                         # first/last both = collapse to one
        out.add(ne)

    def _map_set(self, out, nk, nv, site):
        """Set a converted (key, value) into a target map, guarding a key collision under
        `collision_policy` — shared by `value` and `_retype`."""
        if out.contains(nk):                               # Class B: key collision
            pol = self.d.collision_policy
            if pol == "last":
                self._emit("map-collision", site, pol, out.at(nk, encoded=False), nv)
                out.set(nk, nv)                            # overwrite (old value dropped)
            elif pol == "first":
                self._emit("map-collision", site, pol, nv, None)   # this value dropped
            else:                                          # fail
                raise ValueError(f"[Class-B] map key collision: {nk.representation()}")
        else:
            out.set(nk, nv)

    # -- the SINGLE container/holder traversal — the one place that knows the container/holder
    #    kinds (optional / vector / set / map / xarray / tuple). Rebuilds `tt` by applying
    #    `elem_fn(element, element_type, site) -> converted` to each element. `elem_fn` is either
    #    `value`'s type-preserving recurse or `_retype`'s policied `_retype_element`; sharing one
    #    loop keeps the two from drifting (the very drift that opened the earlier Optional/Tuple
    #    gap — value() had them, _retype()'s hand-kept copy did not). Set-collapse / map-collision
    #    are guarded here, uniformly for both callers. Returns None if `tt` is not one of the six
    #    (the caller handles struct / key / any / enum / variant / commit_id / leaf). Vec/Mat are
    #    NOT here (numeric, cell-addressed, retype-only) nor is variant (arm-set semantics).
    def _map_elements(self, v, tt, elem_fn, site):
        tc = tt.type_code()
        if tc == "optional":
            vo = V.ValueOptional.cast(v)
            if vo.is_nil():
                return V.ValueOptional(tt)
            et = V.TypeOptional.cast(tt).element_type()
            return V.ValueOptional(tt, elem_fn(vo.unwrap(encoded=False), et, site))
        if tc == "vector":
            vv = V.ValueVector.cast(v)
            et = V.TypeVector.cast(tt).element_type()
            out = V.ValueVector(tt)
            esite = self._sub(site, "[]")
            for i in range(vv.size()):
                out.append(elem_fn(vv.at(i, encoded=False), et, esite))
            return out
        if tc == "set":
            vs = V.ValueSet.cast(v)
            et = V.TypeSet.cast(tt).element_type()
            out = V.ValueSet(tt)
            esite = self._sub(site, "{}")
            for i in range(vs.size()):
                self._set_add(out, elem_fn(vs.at(i, encoded=False), et, esite), site)
            return out
        if tc == "map":
            mt = V.TypeMap.cast(tt)
            kt, et = mt.key_type(), mt.element_type()
            vm = V.ValueMap.cast(v)
            out = V.ValueMap(tt)
            ksite, vsite = self._sub(site, "<key>"), self._sub(site, "<val>")
            for k, val in vm.items(encoded=False):
                self._map_set(out, elem_fn(k, kt, ksite), elem_fn(val, et, vsite), site)
            return out
        if tc == "xarray":
            # ATOMIC: the source layout (positions + tombstones) is copied opaquely inside
            # rebuild_from, and the re-mapped elements are installed in the SAME step.
            vx = V.ValueXArray.cast(v)
            et = V.TypeXArray.cast(tt).element_type()
            esite = self._sub(site, "[]")
            out = V.ValueXArray(tt)
            out.rebuild_from(vx, [(pos, elem_fn(val, et, esite)) for pos, val in vx.items(encoded=False)])
            return out
        if tc == "tuple":
            vt = V.ValueTuple.cast(v)
            ets = V.TypeTuple.cast(tt).types()
            if vt.size() != len(ets):                              # a tuple conversion is per-position;
                raise ValueError(f"[unsupported] tuple arity change {vt.size()}→{len(ets)} — a tuple "
                                 f"conversion must preserve arity (it is a per-position element conversion)")
            return V.ValueTuple(tt, [elem_fn(vt.at(i, encoded=False), ets[i], self._sub(site, f".{i}"))
                                     for i in range(vt.size())])
        return None

    # -- retype dispatcher: structural (unwrap, Vector→Set) + leaf (widen/narrow/
    #    format/parse). Class A converts automatically; Class B consults the policy
    #    ONLY on the offending value — in-domain values always convert exactly.
    def _retype(self, sv, tt, policy, site=None):
        sc, tc = sv.type_code(), tt.type_code()

        # -- structural
        if sc == "optional" and tc != "optional":                  # unwrap Optional<A> → A
            vo = V.ValueOptional.cast(sv)
            if vo.is_nil():
                return self._on_missing(tt, policy, "nil-unwrap", site, "nil")
            # The unwrapped value may itself need a POLICY-GOVERNED leaf conversion
            # (Optional<int64>→int32 narrowing, Optional<string>→int parse). Recursing
            # blindly through value() would land in its policy-blind primitive tail and
            # silently ignore the decree; route a differing leaf back through _retype so
            # the policy governs it too. Same type_code ⇒ no leaf conversion ⇒ plain
            # rewrite / composite recursion via value().
            inner = vo.unwrap(encoded=False)
            if inner.type_code() == tc:
                return self.value(inner, tt, site)
            return self._retype(inner, tt, policy, site)
        if sc == "vector" and tc == "set":                         # Vector → Set (collapse)
            et = V.TypeSet.cast(tt).element_type()
            out = V.ValueSet(tt)
            vv = V.ValueVector.cast(sv)
            esite = self._sub(site, "[]")
            for i in range(vv.size()):
                out.add(self.value(vv.at(i, encoded=False), et, esite))
            return out
        if sc == "set" and tc == "vector":                         # Set → Vector (Class A: the
            et = V.TypeVector.cast(tt).element_type()               # set's canonical total-order
            out = V.ValueVector(tt)                                 # sequence — nothing is lost)
            vs = V.ValueSet.cast(sv)
            esite = self._sub(site, "{}")
            for i in range(vs.size()):
                out.append(self.value(vs.at(i, encoded=False), et, esite))
            return out
        if sc == "vector" and tc == "xarray":                      # Vector → XArray (Class A:
            et = V.TypeXArray.cast(tt).element_type()               # order preserved). Positions
            out = V.ValueXArray(tt)                                 # must be DETERMINISTIC — the
            vv = V.ValueVector.cast(sv)                             # runtime's create_position()/
            esite = self._sub(site, "[]")
            for i in range(vv.size()):                             # append() mint RANDOM ids,
                pos = V.ValueUUId(f"00000001-0000-0000-0000-{i:012x}")   # which would break verify's
                out.insert(V.ValueXArray.END,                      # re-derivation; index-derived
                           self.value(vv.at(i, encoded=False), et, esite), pos)  # positions are stable.
            return out
        if sc == "xarray" and tc == "vector":                      # XArray → Vector (Class A):
            return self.value(V.ValueXArray.cast(sv).to_vector(), tt, site)   # live elements in
                                                                   # position order; positions/
                                                                   # tombstones are metadata, dropped.
        if sc in ("vec", "mat") and tc == "vector":                # Vec/Mat → Vector (Class A):
            out = V.ValueVector(tt)                                 # flatten, element type preserved.
            if sc == "vec":                                        # A Mat flattens in COLUMN-MAJOR
                vv = V.ValueVec.cast(sv)                           # (the runtime's native order — no
                for i in range(vv.size()):                        # C/F ambiguity going this way).
                    out.append(vv.at(i, encoded=False))
            else:
                vm = V.ValueMat.cast(sv)
                sm = V.TypeMat.cast(sv.type())
                for c in range(sm.columns()):
                    for r in range(sm.rows()):
                        out.append(vm.at(c, r, encoded=False))
            return out
        if sc == "vector" and tc == "vec":                         # Vector → Vec (Class B): the
            n = V.TypeVec.cast(tt).size()                          # runtime length must equal the
            vv = V.ValueVector.cast(sv)                            # fixed size; the policy governs
            elems = [vv.at(i, encoded=False) for i in range(vv.size())]   # the mismatch offender.
            if len(elems) == n:
                return V.ValueVec(tt, elems)                       # exact length → lossless
            op = f"Vector→Vec length {len(elems)}→{n}"
            if policy is None or policy == "fail":
                raise ValueError(f"[Class-B] Vector→Vec length {len(elems)} != fixed size {n}; "
                                 f"decree a policy")
            if policy == "drop-record":
                self._emit(op, site, policy, len(elems), None)
                raise Unrepresentable("length-fit")
            if isinstance(policy, tuple) and policy[0] == "fit":   # truncate the tail / pad the fill
                fitted = elems[:n] + [_native(policy[1])] * max(0, n - len(elems))
                self._emit(op, site, policy, len(elems), n)
                return V.ValueVec(tt, fitted)
            raise ValueError(f"unknown policy {policy!r} for Vector→Vec")

        if sc == "variant" and tc == "variant":                    # variant arm-set change
            vv = V.ValueVariant.cast(sv)
            inner = self.value(vv.unwrap(encoded=False), None, site)   # map the concrete arm value
            tgt_ids = {a.runtime_id().representation() for a in V.TypeVariant.cast(tt).types()}
            if inner.type().runtime_id().representation() in tgt_ids:   # arm survives (add / reorder):
                out = V.ValueVariant(tt)                               # re-wrap BY TYPE — index-safe,
                out.wrap(inner, inner.type())                         # lands at the target's arm index
                return out
            op = f"variant arm removed: {inner.type().representation()}"   # value on a removed arm (B)
            if policy is None or policy == "fail":
                raise ValueError(f"[Class-B] variant value on a removed arm "
                                 f"{inner.type().representation()}; decree a policy")
            if policy == "drop-record":
                self._emit(op, site, policy, inner, None)
                raise Unrepresentable("variant-arm-removed")
            if isinstance(policy, tuple) and policy[0] == "default":
                self._emit(op, site, policy, inner, policy[1])
                return policy[1]
            raise ValueError(f"unknown policy {policy!r} for variant arm removal")

        if sc == "vec" and tc == "vec":                            # Vec<T,n> → Vec<T',n>: element
            se = V.TypeVec.cast(sv.type()).element_type()          # widen (A) / narrow (B), at fixed
            te = V.TypeVec.cast(tt).element_type()                 # size. Reuse the SCALAR leaf path
            vv = V.ValueVec.cast(sv)                               # per element (no duplicated policy
            out = V.ValueVec(tt)                                   # logic): wrap each native as a leaf
            for i in range(V.TypeVec.cast(tt).size()):             # value, run _retype, unwrap back.
                conv = self._retype(V.Value.create(se, vv.at(i, encoded=False)), te,
                                    policy, self._sub(site, f"[{i}]"))
                out.set(i, V.Value.dumps(conv))
            return out
        if sc == "mat" and tc == "mat":                            # Mat<T,c,r> → Mat<T',c,r>: element
            tm = V.TypeMat.cast(tt)                                # widen/narrow, at fixed shape,
            se = V.TypeMat.cast(sv.type()).element_type()          # column-major (Viper-native order).
            te = tm.element_type()
            vm = V.ValueMat.cast(sv)
            out = V.ValueMat(tt)
            for c in range(tm.columns()):
                for r in range(tm.rows()):
                    conv = self._retype(V.Value.create(se, vm.at(c, r, encoded=False)), te,
                                        policy, self._sub(site, f"[{c},{r}]"))
                    out.set(c, r, V.Value.dumps(conv))
            return out

        # Same-kind container / holder element retype (optional / vector / set / map / xarray /
        # tuple; element widen A / narrow B) — the retype twin of value()'s traversal, through the
        # SAME `_map_elements` loop, so the two can never drift (the drift that opened the earlier
        # Optional/Tuple gap). Each element rides `_retype_element` (the policy-governed leaf path);
        # the container shape is preserved. A same-shape narrow that collides two elements is the
        # Class-B non-injective loss `_set_add` / `_map_set` guard. Guarded `sc == tc`: a cross-kind
        # pair (all bridged above) must NOT reach `_map_elements`, which dispatches on the target.
        if sc == tc:
            mapped = self._map_elements(
                sv, tt, lambda e, et, s: self._retype_element(e, et, policy, s), site)
            if mapped is not None:
                return mapped

        # -- leaf: a scalar↔scalar conversion from here on. Any COMPOSITE reaching this point has
        #    no conversion branch above — fail CLOSED (invariant #1), never the numeric tail's crash
        #    on a composite operand. A composite retype the engine does not express automatically
        #    (struct↔struct, enum↔enum, key↔key, any, …) belongs to a Class-C hook.
        if sc in COMPOSITES or tc in COMPOSITES:
            raise ValueError(f"[unsupported] retype {sc}→{tc}: no conversion branch for this "
                             f"composite retype — use a transform_field / transform_type hook, or "
                             f"an explicit directive; the engine will not guess a composite mapping")
        if (sc, tc) in WIDENING:
            return V.Value.create(tt, V.Value.dumps(sv))           # A: widen (lossless)
        if tc == "string":
            return V.ValueString(str(V.Value.dumps(sv)))           # A: format (total)
        if sc == "string":
            return self._parse(sv, tt, policy, site)               # B: parse
        native = V.Value.dumps(sv)                                 # numeric narrowing
        lo, hi = INT_RANGE.get(tc, (None, None))
        if sc in FLOATS and tc in INT_RANGE:                       # float → int (Class B)
            return self._float_to_int(native, tt, lo, hi, policy, site)
        if lo is not None and lo <= native <= hi:
            return V.Value.create(tt, native)                      # in range → exact (no loss)
        if policy is None or policy == "fail":
            raise ValueError(f"[Class-B] {sc}->{tc} out of range: {native}")
        op = f"narrow {sc}→{tc}"
        if policy == "saturate":
            clamped = max(lo, min(hi, native))
            self._emit(op, site, policy, native, clamped)
            return V.Value.create(tt, clamped)
        if isinstance(policy, tuple) and policy[0] == "default":
            self._emit(op, site, policy, native, policy[1])
            return policy[1]
        raise ValueError(f"unknown policy {policy!r}")

    def _float_to_int(self, native, tt, lo, hi, policy, site=None):
        """float → int (Class B). Truncate toward zero — the defined float→int semantics —
        then the decreed policy governs the *offenders*: a finite value out of range, or a
        non-finite one (`NaN` / ±inf) which has no int image at all. `saturate` clamps by
        Viper's total order (`NaN` and `-inf` are its low end → `lo`; `+inf` → `hi`)."""
        tcn = tt.type_code()                                       # sc is float/double
        if math.isfinite(native):
            truncated = int(native)                                # toward zero (C-cast / int())
            if lo <= truncated <= hi:
                if native != truncated:                            # in range, but a fraction is lost
                    self._emit(f"float→{tcn} truncate", site, policy, native, truncated)
                return V.Value.create(tt, truncated)               # in range after truncation
        op = f"float→{tcn} edge"                                   # non-finite / out of range
        if policy is None or policy == "fail":
            raise ValueError(f"[Class-B] float→{tt.type_code()}: {native} has no in-range int "
                             f"image (fraction / overflow / non-finite); decree a policy")
        if policy == "saturate":
            clamped = hi if native == math.inf else lo if not math.isfinite(native) \
                else max(lo, min(hi, int(native)))                 # NaN, -inf → lo; +inf → hi
            self._emit(op, site, policy, native, clamped)
            return V.Value.create(tt, clamped)
        if isinstance(policy, tuple) and policy[0] == "default":
            self._emit(op, site, policy, native, policy[1])
            return policy[1]
        if policy == "drop-record":
            self._emit(op, site, policy, native, None)
            raise Unrepresentable("float-narrow")
        raise ValueError(f"unknown policy {policy!r} for float→int")

    def _on_missing(self, tt, policy, kind, site=None, before=None):   # nil-unwrap / parse-fail
        if policy is None or policy == "fail":
            raise ValueError(f"[Class-B] {kind}: absent value; decree a policy")
        if policy == "drop-record":
            self._emit(kind, site, policy, before, None)
            raise Unrepresentable(kind)
        if isinstance(policy, tuple) and policy[0] == "default":
            self._emit(kind, site, policy, before, policy[1])
            return policy[1]
        raise ValueError(f"unknown policy {policy!r}")

    def _parse(self, sv, tt, policy, site=None):
        s, tc = V.Value.dumps(sv), tt.type_code()
        try:
            if tc in INT_RANGE:
                native = int(s)
                lo, hi = INT_RANGE[tc]
                if not (lo <= native <= hi):
                    raise ValueError("range")
            else:
                native = float(s)
            return V.Value.create(tt, native)                      # parseable → exact (no loss)
        except (ValueError, TypeError):
            return self._on_missing(tt, policy, f"parse→{tc}", site, s)

    # -- Vec/Mat DIMENSION transforms (family 2). Position-preserving: cell [i]/[i,j] keeps
    #    its coordinates. Grow fills the new cells; shrink drops the trailing ones. NO flatten
    #    is involved, so there is no layout ambiguity — column-major is preserved throughout.
    def _shrink_loss(self, on_shrink, site, before, after):
        if on_shrink == "fail":
            raise ValueError(f"[Class-B] resize would drop cells ({before} → {after}); pass "
                             f"on_shrink='accept' to keep the fit and discard the trailing cells")
        if on_shrink == "accept":
            self._emit("resize-shrink", site, on_shrink, before, after)
            return
        raise ValueError(f"unknown on_shrink {on_shrink!r}")

    def _resize(self, sv, tt, spec, site=None):
        kind, _dims, fill, on_shrink = spec
        if kind == "vec":
            svv = V.ValueVec.cast(sv)
            sn, n = svv.size(), V.TypeVec.cast(tt).size()
            if sn > n:                                             # shrink: the tail is dropped
                self._shrink_loss(on_shrink, site, sn, n)         # (raises on "fail")
            out = V.ValueVec(tt)                                  # inits to the type's zero
            for i in range(min(sn, n)):                           # preserve the overlap [0..min)
                out.set(i, svv.at(i, encoded=False))
            if n > sn and fill != "zero":                         # grow with a scalar fill
                for i in range(sn, n):
                    out.set(i, _native(fill))
            return out

        svm = V.ValueMat.cast(sv)                                 # kind == "mat"
        smt = V.TypeMat.cast(sv.type())
        sc, sr = smt.columns(), smt.rows()
        tmt = V.TypeMat.cast(tt)
        tc, tr = tmt.columns(), tmt.rows()
        if sc > tc or sr > tr:                                    # any dimension shrinks → loss
            self._shrink_loss(on_shrink, site, f"{sc}×{sr}", f"{tc}×{tr}")
        out = V.ValueMat(tt)                                      # inits to IDENTITY
        for c in range(min(sc, tc)):                              # preserve the source block; new
            for r in range(min(sr, tr)):                         # cells keep the identity default
                out.set(c, r, svm.at(c, r, encoded=False))       # → 'identity' fill is free here
        if fill != "identity":                                    # 'zero' / scalar: overwrite the
            fv = 0 if fill == "zero" else _native(fill)          # cells outside the source block
            for c in range(tc):
                for r in range(tr):
                    if c >= sc or r >= sr:
                        out.set(c, r, fv)
        return out

    def _transpose(self, sv, tt):
        svm = V.ValueMat.cast(sv)
        smt = V.TypeMat.cast(sv.type())
        out = V.ValueMat(tt)                                      # tt is Mat<r,c> (derived)
        for c in range(smt.columns()):
            for r in range(smt.rows()):
                out.set(r, c, svm.at(c, r, encoded=False))       # [c,r] → [r,c], lossless
        return out

    # -- Class-C hooks. The VALIDATION is where total-or-explicit-refusal survives user code:
    #    the hook returns a valid target value (used), raises `Unrepresentable` (drop the
    #    record), or raises (refuse) — anything it produces that is not a value of `target_type`
    #    is refused. Two flavours by context: a TYPE hook fires at any node (rides the
    #    recursion) so it is value-scoped; a FIELD hook is at a struct-field position so it
    #    sees the whole source struct (siblings) + the field name — enabling cross-field.
    def _validate_hook_output(self, result, target_type):
        if not hasattr(result, "type"):
            raise ValueError(f"[hook] transform must return a Value, got {type(result).__name__}")
        if result.type().runtime_id().representation() != target_type.runtime_id().representation():
            raise ValueError(f"[hook] transform returned {result.type().representation()}, "
                             f"expected {target_type.representation()}")

    def _apply_value_hook(self, fn, value, target_type, site):        # transform_type (value-scoped)
        if _hook_param_count(fn) >= 3:                               # opts into the non-local ctx
            result = fn(value, target_type, _HookContext(self))
        else:
            result = fn(value, target_type)                         # user code; may raise Unrepresentable
        self._validate_hook_output(result, target_type)
        self._emit("transform", site, getattr(fn, "__name__", "hook"), value, result)
        return result

    def _apply_field_hook(self, fn, source_struct, field_name, target_type, site):   # struct-scoped
        if _hook_param_count(fn) >= 4:                               # opts into the non-local ctx
            result = fn(source_struct, field_name, target_type, _HookContext(self))
        else:
            result = fn(source_struct, field_name, target_type)     # sees the struct (siblings) + field name
        self._validate_hook_output(result, target_type)
        try:
            before = source_struct.at(field_name, encoded=False)   # the old field value, if any (a new
        except Exception:                                          # / derived field has none)
            before = None
        self._emit("transform", site, getattr(fn, "__name__", "hook"), before, result)
        return result

    # -- target field name -> source field name (via renames), or None if added
    def _field_source(self, src, tgt):
        fren = self.d.field_renames.get(src.representation(), {})   # src -> tgt
        tgt_to_src = {t: s for s, t in fren.items()}
        src_names = {f.name() for f in src.fields()}
        out = {}
        for f in tgt.fields():
            if f.name() in tgt_to_src:
                out[f.name()] = tgt_to_src[f.name()]
            elif f.name() in src_names:
                out[f.name()] = f.name()
            else:
                out[f.name()] = None                               # added field
        return out

    # -- value ⇒ value, TARGET-DIRECTED. `tt` = the target type (derived if None).
    #    ONE engine for both families: family 1 = same shape (rename/id re-stamp);
    #    family 2 = walk the TARGET (drop absent, seed added, convert leaves).
    def value(self, v, tt=None, site=None):
        if tt is None:
            tt = self.map_type(v.type())
        # global Class-C hook: any node whose SOURCE type is transform_type'd (a field-level
        # transform_field on the same position wins — it is applied in the struct branch, which
        # never recurses here for that field). Rides the recursion → reaches nested occurrences.
        th = self.d.transformed_types.get(v.type().runtime_id().representation())
        if th is not None:
            return self._apply_value_hook(th[1], v, tt, site)
        tc = v.type_code()
        obs = self._sink is not None

        if tc == "struct":
            vs = V.ValueStructure.cast(v)
            src = vs.type_structure()
            srep = src.representation()
            tgt = V.TypeStructure.cast(tt)
            fsrc = self._field_source(src, tgt)
            retypes = self.d.retyped_fields.get(srep, {})
            resized = self.d.resized_fields.get(srep, {})
            transposed = self.d.transposed_fields.get(srep, set())
            transformed = self.d.transformed_fields.get(srep, {})
            adds = {name: (payload, derive)                        # target name -> (default | Type, derive?)
                    for name, payload, derive in self.d.added_fields.get(srep, [])}
            out = {}
            for f in tgt.fields():
                sn = fsrc[f.name()]
                if sn is None:                                     # added / derived field
                    payload, derive = adds.get(f.name(), (None, None))
                    if derive is not None:                         # Class-C: derived from the struct
                        out[f.name()] = self._apply_field_hook(derive, vs, f.name(), f.type(),
                                                               f"{srep}.{f.name()}" if obs else None)
                    else:                                          # static seed value (NOT
                        # f.default_value(): the runtime normalizes a type-zero default to
                        # no-default, which would lose the seed)
                        out[f.name()] = payload if payload is not None else f.default_value()
                elif sn in transformed:                            # Class-C field hook (sees the struct)
                    _nt, fn = transformed[sn]
                    out[f.name()] = self._apply_field_hook(fn, vs, sn, f.type(),
                                                           f"{srep}.{sn}" if obs else None)
                elif sn in resized:                                # family 2: Vec/Mat resize
                    out[f.name()] = self._resize(vs.at(sn, encoded=False), f.type(), resized[sn],
                                                 f"{srep}.{sn}" if obs else None)
                elif sn in transposed:                             # family 2: Mat transpose
                    out[f.name()] = self._transpose(vs.at(sn, encoded=False), f.type())
                elif sn in retypes:                                # family 2: retype
                    new_type, policy = retypes[sn]
                    out[f.name()] = self._retype(vs.at(sn, encoded=False), new_type, policy,
                                                 f"{srep}.{sn}" if obs else None)
                else:                                              # kept/renamed → recurse
                    out[f.name()] = self.value(vs.at(sn, encoded=False), f.type(),
                                               f"{srep}.{f.name()}" if obs else None)
            return V.ValueStructure(tgt, out)

        if tc == "key":
            # Rebuild on the target concept + stable instanceId, then retype to the
            # mapped Key<X> so the flavour (concept / club / any-concept) survives —
            # create() alone always yields a concept key, silently downgrading the rest.
            vk = V.ValueKey.cast(v)
            base = V.ValueKey.create(self._map_named(vk.type_concept()), vk.instance_id())
            return base.to_key(V.TypeKey.cast(tt))

        if tc == "any":
            va = V.ValueAny.cast(v)
            return V.ValueAny() if va.is_nil() else V.ValueAny(self.value(va.unwrap(encoded=False), None, site))

        if tc == "enum":
            ve = V.ValueEnumeration.cast(v)
            erepr = ve.type_enumeration().representation()
            name = ve.name()
            removed = self.d.removed_cases.get(erepr, {})
            if name in removed:                                    # Class B: removed case populated
                policy = removed[name]
                if isinstance(policy, tuple) and policy[0] == "map-case":
                    self._emit("remove-case", site, policy, name, policy[1])
                    name = policy[1]
                elif policy == "drop-record":
                    self._emit("remove-case", site, policy, name, None)
                    raise Unrepresentable("remove-case")
                else:                                              # fail / None
                    raise ValueError(f"[Class-B] removed case populated: {name}")
            else:
                name = self.d.case_renames.get(erepr, {}).get(name, name)
            return V.ValueEnumeration(V.TypeEnumeration.cast(tt), name)

        # Container / holder kinds (optional / vector / set / map / xarray / tuple) — one shared
        # traversal, so `value` and `_retype`'s same-kind element retype stay in lockstep.
        mapped = self._map_elements(v, tt, lambda e, et, s: self.value(e, et, s), site)
        if mapped is not None:
            return mapped

        if tc == "variant":
            vv = V.ValueVariant.cast(v)
            inner = self.value(vv.unwrap(encoded=False), None, site)
            out = V.ValueVariant(tt)
            out.wrap(inner, inner.type())
            return out

        if tc == "commit_id" and self._commit_id_remap is not None:
            # DAG replay: an intra-DAG commit reference is remapped to its re-issued
            # id (topological order guarantees it is already known); an external
            # commit_id (from another base) is kept verbatim.
            return self._commit_id_remap.get(v.representation(), v)

        # primitive leaf. GUARD: an unhandled *composite* must NOT pass through
        # verbatim (that would keep stale ids — silent corruption). Only genuine
        # leaves (no embedded id, no name) are verbatim / convertible.
        if tc not in PRIMITIVES:
            raise ValueError(f"[engine] unhandled composite type_code {tc!r} "
                             f"— refusing to pass it through unrewritten")
        if v.type_code() == tt.type_code():
            return v
        return V.Value.create(tt, V.Value.dumps(v))

    # -- map a source attachment (a named Map<Key,Doc>) to its target
    def attachment(self, a):
        return self.att_map[a.runtime_id().representation()]


# -- addressing an attachment in a directive -------------------------------------
#    An attachment's identity is its `identifier()` — `NS::KeyConcept.name` — and that is what
#    the directive API names (`rename_attachment(old_id, ...)`, `drop_attachment(identifier)`).
#    The bare local name is accepted as a legacy key, but it is NOT an identity: two concepts in
#    one namespace may each carry an attachment of the same name (`N::Customer.orders` and
#    `N::Vendor.orders`), and a directive written that way addresses every homonym at once.
#    Prefer the identifier; a name-based consumer (a source codemod) can only mirror that one.
def _att_keys(a):
    identifier = a.identifier()
    return identifier, identifier.rsplit(".", 1)[-1]


def _att_get(mapping, a, default=None):
    for key in _att_keys(a):
        if key in mapping:
            return mapping[key]
    return default


def _att_hit(container, a):
    return any(key in container for key in _att_keys(a))


# -- an add_field default lives in a TARGET field but is authored against the
#    SOURCE domain: it is only expressible across a migration if it references no
#    named (migrated) type — a primitive leaf or a container of such. A default
#    embedding a struct/enum/concept/club/key/Any would carry stale source ids.
def _default_domain_free(t):
    tc = t.type_code()
    if tc in PRIMITIVES:                                        # scalar leaves (incl. vec/mat)
        return True
    if tc == "optional":
        return _default_domain_free(V.TypeOptional.cast(t).element_type())
    if tc == "vector":
        return _default_domain_free(V.TypeVector.cast(t).element_type())
    if tc == "set":
        return _default_domain_free(V.TypeSet.cast(t).element_type())
    if tc == "map":
        m = V.TypeMap.cast(t)
        return _default_domain_free(m.key_type()) and _default_domain_free(m.element_type())
    if tc == "tuple":
        return all(_default_domain_free(x) for x in V.TypeTuple.cast(t).types())
    if tc == "variant":
        return all(_default_domain_free(x) for x in V.TypeVariant.cast(t).types())
    if tc == "xarray":
        return _default_domain_free(V.TypeXArray.cast(t).element_type())
    return False                                               # struct / enum / key / any


# -- Vec/Mat dimension ops: derive the target type (element T read from the source) and
#    validate the field is a DIRECT Vec/Mat. A nested Vec/Mat (Vector<Vec<T,n>>, a struct
#    field of Vec, ...) is not yet addressable — the directive names a struct field, and
#    there is no type-path vocabulary to reach a Vec/Mat buried inside a container.
def _resized_type(struct_repr, field, spec):
    kind, dims, fill, _on_shrink = spec
    t = field.type()
    if kind == "vec":
        if t.type_code() != "vec":
            raise ValueError(f"[unsupported] resize_vec_field on {struct_repr}.{field.name()}: "
                             f"its type is {t.representation()}, not a direct Vec — a nested "
                             f"Vec/Mat is not yet addressable (needs a type-path directive)")
        if fill == "identity":
            raise ValueError(f"[unsupported] resize_vec_field fill='identity' on "
                             f"{struct_repr}.{field.name()}: identity is Mat-only; use 'zero' or a scalar")
        return V.TypeVec(V.TypeVec.cast(t).element_type(), dims[0])
    if t.type_code() != "mat":                                  # kind == "mat"
        raise ValueError(f"[unsupported] resize_mat_field on {struct_repr}.{field.name()}: "
                         f"its type is {t.representation()}, not a direct Mat — a nested "
                         f"Vec/Mat is not yet addressable (needs a type-path directive)")
    return V.TypeMat(V.TypeMat.cast(t).element_type(), dims[0], dims[1])


def _transposed_type(struct_repr, field):
    t = field.type()
    if t.type_code() != "mat":
        raise ValueError(f"[unsupported] transpose_mat_field on {struct_repr}.{field.name()}: "
                         f"its type is {t.representation()}, not a direct Mat")
    m = V.TypeMat.cast(t)
    return V.TypeMat(m.element_type(), m.rows(), m.columns())    # Mat<c,r> -> Mat<r,c>


def _validate_dimension_ops(src, directives):
    """Validate every resize/transpose directive up front (direct-Vec/Mat + fill coherence),
    before any target is built or data touched. Shared by both construction paths."""
    by_repr = {s.representation(): s for s in src.structures()}
    for srep, fields in directives.resized_fields.items():
        s = by_repr.get(srep)
        if s is not None:
            for fname, spec in fields.items():
                _resized_type(srep, s.check(fname), spec)
    for srep, fset in directives.transposed_fields.items():
        s = by_repr.get(srep)
        if s is not None:
            for fname in fset:
                _transposed_type(srep, s.check(fname))


def _refuse_unknown_targets(src, directives):
    """Accumulate EVERY directive that names something the SOURCE schema does not hold, and refuse
    them together — before any definition is built.

    A directive names its target by its **source** name, so a misspelling matches nothing and the
    directive simply never fires: the target is built as if it had not been written, the digest
    agrees with it, and the migration reports success having done nothing. That silence is the
    failure mode this guard removes; it is worst for a source codemod, where the only evidence of
    success is a diff.

    Two families are deliberately NOT checked, and for the same reason in both cases — the name is
    not a source name:

    * `field_order` / `case_order` list the **target** member set (after renames, adds and drops),
      so their entries cannot be looked up in the source. Their struct/enum key is checked; their
      contents are validated by the build itself, which refuses a non-permutation.
    * `transform_type` keys its source by `runtimeId`, and that type need not occur in the
      persistence schema at all — a composite used only in a function-pool signature is the case
      this tool exists to handle. Refusing it here would break exactly that.
    """
    structures = {s.representation(): s for s in src.structures()}
    enumerations = {e.representation(): e for e in src.enumerations()}
    named = set(structures) | set(enumerations)
    named |= {c.representation() for c in src.concepts()}
    named |= {c.representation() for c in src.clubs()}
    namespaces = {d.type_name().name_space().uuid().representation()
                  for d in (*src.structures(), *src.enumerations(),
                            *src.concepts(), *src.clubs())}
    attachments = set()
    for a in src.attachments():
        attachments.update(_att_keys(a))

    findings = []

    def known_type(directive, repr_, pool, what):
        if repr_ in pool:
            return True
        findings.append(f"{directive}('{repr_}') — no such {what}")
        return False

    def known_members(directive, holder_repr, holders, names, what, extra=()):
        # the holder was already reported when unknown; do not report its members too
        holder_what = "structure" if what == "fields" else "enumeration"
        if not known_type(directive, holder_repr, set(holders), holder_what):
            return
        held = holders[holder_repr]
        have = {m.name() for m in (held.fields() if what == "fields" else held.cases())}
        have.update(extra)
        for name in names:
            if name not in have:
                findings.append(f"{directive}('{holder_repr}', '{name}') — "
                                f"no such {what[:-1]} in {holder_repr}")

    for repr_ in directives.type_renames:
        known_type("rename_type", repr_, named, "type")
    for repr_ in directives.type_docs:
        known_type("document_type", repr_, named, "type")
    for repr_ in directives.dropped_types:
        known_type("drop_type", repr_, named, "type")
    for repr_ in directives.type_namespaces:
        known_type("move_type", repr_, named, "type")

    for directive, group, names_of in (
            ("rename_field", directives.field_renames, dict.keys),
            ("drop_field", directives.dropped_fields, lambda v: v),
            ("retype_field", directives.retyped_fields, dict.keys),
            ("resize_field", directives.resized_fields, dict.keys),
            ("transpose_mat_field", directives.transposed_fields, lambda v: v),
            ("transform_field", directives.transformed_fields, dict.keys),
            ("document_field", directives.field_docs, dict.keys)):
        for holder, entry in group.items():
            # an ADDED field takes its doc from document_field, so it is a legal target there —
            # unlike an added case, whose doc the build does not carry (added cases default to
            # none), which this guard therefore reports rather than silently ignoring.
            added = ({name for name, _payload, _derive in directives.added_fields.get(holder, ())}
                     if directive == "document_field" else ())
            known_members(directive, holder, structures, names_of(entry), "fields", added)
    for holder in (*directives.added_fields, *directives.field_order):
        known_type("add_field / reorder_fields", holder, set(structures), "structure")

    for directive, group in (("rename_case", directives.case_renames),
                             ("remove_case", directives.removed_cases),
                             ("document_case", directives.case_docs)):
        for holder, entry in group.items():
            known_members(directive, holder, enumerations, entry.keys(), "cases")
    for holder in (*directives.added_cases, *directives.case_order):
        known_type("add_case / reorder_cases", holder, set(enumerations), "enumeration")

    for directive, group in (("rename_attachment", directives.attachment_renames),
                             ("document_attachment", directives.attachment_docs),
                             ("drop_attachment", directives.dropped_attachments),
                             ("move_attachment", directives.attachment_namespaces)):
        for identifier in group:
            known_type(directive, identifier, attachments, "attachment")

    for directive, group in (("rename_namespace", directives.namespace_names),
                             ("remap_namespace", directives.namespace_uuids)):
        for uuid in group:
            known_type(directive, uuid, namespaces, "namespace (by uuid)")

    if not findings:
        return
    raise ValueError(f"[unknown-target] {len(findings)} directive(s) name something the source "
                     "schema does not hold, so they would do nothing at all:\n"
                     + "\n".join(f"  {f}" for f in sorted(findings)) +
                     "\nA directive names its target by its SOURCE name (the schema you migrate "
                     "FROM), members by their source name too — check the spelling there.")


def _format_drop_report(src, directives, refs_dropped):
    """Accumulate EVERY surviving reference to a `drop_type`'d type into one legible report;
    return the report string, or `None` if the drops dangle nothing. Sites: struct fields
    (skipping a field another directive removes/replaces — `drop_field`/`retype_field`/
    `transform_field`), concept parents (`isa`), club memberships, attachment key/document
    types. Nothing references an attachment, so `drop_attachment` never appears here."""
    dropped_types = directives.dropped_types
    dropped_atts = directives.dropped_attachments
    findings = []                                     # (referrer, detail, dropped type)

    for s in src.structures():
        if s.representation() in dropped_types:
            continue
        drops = directives.dropped_fields.get(s.representation(), set())
        retypes = directives.retyped_fields.get(s.representation(), {})
        transformed = directives.transformed_fields.get(s.representation(), {})
        for f in s.fields():
            if f.name() in drops or f.name() in retypes or f.name() in transformed:
                continue                              # reference removed/replaced by a directive
            hit = refs_dropped(f.type())
            if hit:
                findings.append((s.representation(),
                                 f"field '{f.name()}' : {f.type().representation()}", hit))

    for c in src.concepts():
        if c.representation() in dropped_types:
            continue
        p = c.parent()
        if p is not None and p.representation() in dropped_types:
            findings.append((c.representation(), f"parent (isa {p.representation()})",
                             p.representation()))

    for cl in src.clubs():
        if cl.representation() in dropped_types:
            continue
        for m in cl.members():
            if m.representation() in dropped_types:
                findings.append((cl.representation(), f"member {m.representation()}",
                                 m.representation()))

    for a in src.attachments():
        if _att_hit(dropped_atts, a):
            continue
        for label, tt in (("key", a.key_type()), ("document", a.document_type())):
            hit = refs_dropped(tt)
            if hit:
                findings.append((f"attachment {a.identifier()}",
                                 f"{label} type : {tt.representation()}", hit))

    if not findings:
        return None
    lines = "\n".join(f"  {ref} — {detail}  ->  dropped {dropped}"
                      for ref, detail, dropped in sorted(findings))
    return ("[dropped-type-referenced] drop_type would leave "
            f"{len(findings)} dangling reference(s) in surviving definitions:\n" + lines +
            "\nHandle each too — drop_field / retype_field / transform_field the field, "
            "drop_type the referrer, or drop_attachment it — or keep the dropped type.")


# ------------------------------------------- definitions ⇒ definitions (target)
def build_target_definitions(source_defs, directives):
    """Construct the target `Definitions` from source + directives, in dependency
    order (concepts by parent, structures by field-type refs), minting the id map
    by construction. Returns (target_defs, tmap, att_map)."""
    for struct_repr, adds in directives.added_fields.items():
        for name, payload, derive in adds:
            if derive is not None:                       # derived field: `payload` is the target Type;
                continue                                 # the value comes from the hook (validated), no default
            if not _default_domain_free(payload.type()):
                raise ValueError(f"[unsupported] add_field('{name}') default of type "
                                 f"{payload.type().representation()} references a named "
                                 f"type — a composite default is not expressible across a "
                                 f"migration (it would carry stale source ids); use a "
                                 f"primitive-leaf default")
    src = _const(source_defs)
    _refuse_unknown_targets(src, directives)      # a misspelt target would silently do nothing
    _validate_dimension_ops(src, directives)      # resize/transpose: direct-Vec/Mat + fill, up front
    target = V.Definitions()
    tmap = {}

    def simple(src_type):                    # target simple name after type rename
        full = src_type.representation()
        return directives.type_renames.get(full, full).split("::")[-1]

    def tgt_ns(src_type):                    # target namespace: name axis + uuid axis
        ov = directives.type_namespaces.get(src_type.representation())
        if ov is not None:                   # per-definition move (split/precise-merge)
            return ov
        sns = src_type.type_name().name_space()
        key = sns.uuid().representation()
        new_uuid = directives.namespace_uuids.get(key, sns.uuid())
        new_name = directives.namespace_names.get(key, sns.name())
        return V.NameSpace(new_uuid, new_name)

    def att_ns(a):                           # attachment namespace: per-attachment move, else bulk
        ov = _att_get(directives.attachment_namespaces, a)
        return ov if ov is not None else tgt_ns(a)

    ELEM = {"optional": V.TypeOptional, "vector": V.TypeVector, "set": V.TypeSet,
            "xarray": V.TypeXArray, "key": V.TypeKey}

    def map_t(t):
        th = directives.transformed_types.get(t.runtime_id().representation())
        if th is not None:                                 # global hook: substitute the new_type
            return map_t(th[0])
        tc = t.type_code()
        if tc in ("struct", "enum", "concept", "club"):
            return tmap[t.runtime_id().representation()]
        if tc in ELEM:
            return ELEM[tc](map_t(ELEM[tc].cast(t).element_type()))
        if tc == "map":
            m = V.TypeMap.cast(t)
            return V.TypeMap(map_t(m.key_type()), map_t(m.element_type()))
        if tc == "tuple":
            return V.TypeTuple([map_t(x) for x in V.TypeTuple.cast(t).types()])
        if tc == "variant":
            return V.TypeVariant([map_t(x) for x in V.TypeVariant.cast(t).types()])
        return t

    def ready(t):
        th = directives.transformed_types.get(t.runtime_id().representation())
        if th is not None:                                 # a hooked type is ready iff new_type is
            return ready(th[0])
        tc = t.type_code()
        if tc in ("struct", "enum", "concept", "club"):
            return t.runtime_id().representation() in tmap
        if tc in ELEM:
            return ready(ELEM[tc].cast(t).element_type())
        if tc == "map":
            m = V.TypeMap.cast(t)
            return ready(m.key_type()) and ready(m.element_type())
        if tc == "tuple":
            return all(ready(x) for x in V.TypeTuple.cast(t).types())
        if tc == "variant":
            return all(ready(x) for x in V.TypeVariant.cast(t).types())
        return True

    def hooked(t):                           # transform_type'd: not built — it maps to new_type
        return t.runtime_id().representation() in directives.transformed_types

    dropped_types = directives.dropped_types
    dropped_atts = directives.dropped_attachments

    def dropped(t):                          # a named type explicitly removed by drop_type
        return t.representation() in dropped_types

    def refs_dropped(t):
        """The dropped type an *effective* target reference would dangle on, or None. Reuses
        the build's own type-walk (mirrors `map_t`): a hooked type resolves to its new_type;
        named leaves test membership in `dropped_types`; containers recurse. This is a
        membership test on resolved ids, not name resolution (that is the runtime's job)."""
        th = directives.transformed_types.get(t.runtime_id().representation())
        if th is not None:
            return refs_dropped(th[0])
        tc = t.type_code()
        if tc in ("struct", "enum", "concept", "club"):
            return t.representation() if t.representation() in dropped_types else None
        if tc in ELEM:
            return refs_dropped(ELEM[tc].cast(t).element_type())
        if tc == "map":
            m = V.TypeMap.cast(t)
            return refs_dropped(m.key_type()) or refs_dropped(m.element_type())
        if tc in ("tuple", "variant"):
            cls = V.TypeTuple if tc == "tuple" else V.TypeVariant
            for x in cls.cast(t).types():
                r = refs_dropped(x)
                if r:
                    return r
        return None

    # -- Upstream drop check: a dropped type must not stay referenced by a SURVIVING
    #    definition (else the rebuild dangles). Accumulate EVERY offending site and report
    #    them together — fail closed and early, before a single create_* or any data. A
    #    reference removed by another directive (drop_field / retype_field / transform_field)
    #    no longer counts. Nothing references an attachment, so dropping one dangles nothing.
    if dropped_types:
        report = _format_drop_report(src, directives, refs_dropped)
        if report:
            raise ValueError(report)

    # -- Upstream collision check: a namespace move / merge can land two definitions in the
    #    same target (namespace, name) slot; construction would refuse the second with a terse
    #    "already registered". Detect ALL such clashes up front and report them together.
    if (directives.type_namespaces or directives.attachment_namespaces
            or directives.namespace_names or directives.namespace_uuids):
        slots = {}
        for t in (list(src.structures()) + list(src.enumerations())
                  + list(src.concepts()) + list(src.clubs())):
            if hooked(t) or dropped(t):
                continue
            slots.setdefault(f"{tgt_ns(t).name()}::{simple(t)}", []).append(t.representation())
        clashes = {rep: sorted(s) for rep, s in slots.items() if len(s) > 1}
        if clashes:
            lines = "\n".join(f"  {rep}  <-  {', '.join(s)}" for rep, s in sorted(clashes.items()))
            raise ValueError(
                f"[namespace-collision] {len(clashes)} target name(s) claimed by more than one "
                f"definition after the namespace move/merge:\n" + lines +
                "\nRename one side (rename_type) or send them to distinct namespaces (move_type).")

    # concepts — topological by parent
    pending = [c for c in src.concepts() if not hooked(c) and not dropped(c)]
    while pending:
        still, progressed = [], False
        for c in pending:
            p = c.parent()
            if p is None or p.runtime_id().representation() in tmap:
                cdoc = directives.type_docs.get(c.representation(), c.documentation())
                if p is None:
                    nc = target.create_concept(tgt_ns(c), simple(c), documentation=cdoc)
                else:
                    nc = target.create_concept(tgt_ns(c), simple(c), documentation=cdoc,
                                               parent=tmap[p.runtime_id().representation()])
                tmap[c.runtime_id().representation()] = nc
                progressed = True
            else:
                still.append(c)
        if not progressed and still:
            raise ValueError("concept parent cycle")
        pending = still

    # clubs + memberships
    for cl in src.clubs():
        if hooked(cl) or dropped(cl):
            continue
        ncl = target.create_club(tgt_ns(cl), simple(cl),
                                 documentation=directives.type_docs.get(cl.representation(),
                                                                        cl.documentation()))
        tmap[cl.runtime_id().representation()] = ncl
        for member in cl.members():
            target.create_membership(ncl, tmap[member.runtime_id().representation()])

    # enumerations — cases renamed / removed / added / reordered
    for e in src.enumerations():
        if hooked(e) or dropped(e):
            continue
        cren = directives.case_renames.get(e.representation(), {})
        removed = directives.removed_cases.get(e.representation(), {})
        names = [cren.get(c.name(), c.name()) for c in e.cases() if c.name() not in removed]
        names += directives.added_cases.get(e.representation(), [])          # add at end
        # documentation (Class A): carried by TARGET case name, overridden by document_case
        # (named by SOURCE case name); added cases default to none.
        authored = directives.case_docs.get(e.representation(), {})
        case_docs = {cren.get(c.name(), c.name()): authored.get(c.name(), c.documentation())
                     for c in e.cases() if c.name() not in removed}
        order = directives.case_order.get(e.representation())
        if order:
            if set(order) != set(names):
                raise ValueError(f"reorder_cases({e.representation()}) not a permutation of {names}")
            names = order
        de = V.TypeEnumerationDescriptor(
            simple(e), documentation=directives.type_docs.get(e.representation(), e.documentation()))
        for name in names:
            de.add_case(name, case_docs.get(name, ""))
        tmap[e.runtime_id().representation()] = target.create_enumeration(tgt_ns(e), de)

    # structures — topological by field-type dependencies, fields renamed
    pending = [s for s in src.structures() if not hooked(s) and not dropped(s)]
    while pending:
        still, progressed = [], False
        for s in pending:
            retypes = directives.retyped_fields.get(s.representation(), {})
            transformed = directives.transformed_fields.get(s.representation(), {})
            adds = directives.added_fields.get(s.representation(), [])
            drops = directives.dropped_fields.get(s.representation(), set())
            # readiness follows the TARGET field type: a retype / transform / derived add to a
            # named type depends on that type being built, not on the source field type. A
            # dropped field imposes no dependency (its type is gone from the target).
            def dep(f):
                if f.name() in transformed:
                    return transformed[f.name()][0]
                if f.name() in retypes:
                    return retypes[f.name()][0]
                return f.type()
            if (all(ready(dep(f)) for f in s.fields() if f.name() not in drops)
                    and all(ready(payload) for _n, payload, derive in adds if derive is not None)):
                fren = directives.field_renames.get(s.representation(), {})
                resized = directives.resized_fields.get(s.representation(), {})
                transposed = directives.transposed_fields.get(s.representation(), set())
                fdocs = directives.field_docs.get(s.representation(), {})   # authored, by source name
                fields = []                                         # (target name, Type or default Value, doc)
                for f in s.fields():
                    if f.name() in drops:                          # family 2: drop
                        continue
                    if f.name() in transformed:                    # Class-C hook: target = mapped new_type
                        ftype = map_t(transformed[f.name()][0])
                    elif f.name() in resized:                      # family 2: Vec/Mat resize
                        ftype = _resized_type(s.representation(), f, resized[f.name()])
                    elif f.name() in transposed:                    # family 2: Mat transpose
                        ftype = _transposed_type(s.representation(), f)
                    elif f.name() in retypes:                       # family 2: retype
                        ftype = retypes[f.name()][0]
                    else:                                           # carried (kept or renamed): same type
                        default = f.default_value()                 # carry a domain-free source default (part of
                        if default is not None and _default_domain_free(f.type()):  # the runtimeId, like a doc):
                            ftype = V.Value.create(map_t(f.type()), default)  # under the MAPPED type, so a
                        else:                                       # transform_type/remap of the default's own
                            ftype = map_t(f.type())                 # type flows through it, not just the field
                    fields.append((fren.get(f.name(), f.name()), ftype,
                                   fdocs.get(f.name(), f.documentation())))
                for name, payload, derive in adds:                  # family 2: add (static or derived)
                    fields.append((name, map_t(payload) if derive is not None else payload,
                                   fdocs.get(name, "")))            # added field: authored or none
                order = directives.field_order.get(s.representation())
                if order:                                           # family 2: reorder
                    by_name = {n: (n, t, d) for n, t, d in fields}
                    if set(order) != set(by_name):
                        raise ValueError(f"reorder_fields({s.representation()}) not a permutation of {list(by_name)}")
                    fields = [by_name[n] for n in order]
                ds = V.TypeStructureDescriptor(
                    simple(s), documentation=directives.type_docs.get(s.representation(),
                                                                      s.documentation()))
                for name, t, doc in fields:                          # documentation carried (Class A)
                    ds.add_field(name, t, doc)
                tmap[s.runtime_id().representation()] = target.create_structure(tgt_ns(s), ds)
                progressed = True
            else:
                still.append(s)
        if not progressed and still:
            raise ValueError("structure dependency cycle")
        pending = still

    # attachments — a named Map<Key, Document>: remap key + document types, rename id
    att_map = {}
    for a in src.attachments():
        if _att_hit(dropped_atts, a):                    # drop_attachment: not recreated
            continue
        local = a.identifier().rsplit(".", 1)[-1]        # identifier() is NS::KeyConcept.name
        renamed = _att_get(directives.attachment_renames, a, local)
        na = target.create_attachment(att_ns(a), renamed.rsplit(".", 1)[-1],   # a new id may be
                                      map_t(a.key_type()), map_t(a.document_type()),  # qualified
                                      documentation=_att_get(directives.attachment_docs, a,
                                                             a.documentation()))
        att_map[a.runtime_id().representation()] = na

    return target, tmap, att_map
