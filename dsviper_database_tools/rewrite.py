"""Definitions-directed document rewriting — the engine.

`DefinitionsTransformer.from_directives(source_defs, directives)` builds the target
`Definitions` from the source + the edit script, and rewrites any value from the
source domain to the target domain with a single TARGET-DIRECTED engine (`value()`)
that spans both transformation families:

  * family 1 — renames (size-preserving): the value is re-stamped, ids follow.
  * family 2 — shape changes: the walk is driven by the *target* type (drop absent,
    seed added defaults, convert leaves), with a decreed policy on every lossy op.

It is a composition of already-bound runtime atoms — no C++, the `dsviper` runtime
untouched. See `ARCHITECTURE.md` for the algorithm and its correctness argument (P1
name-completeness + P2 shape-invariance for family 1; policy-completeness for the
lossy family-2 operations).
"""

import dsviper as V

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


class DropRecord(Exception):
    """Loop-level signal: the converter cannot produce a value for this document
    under the decreed `drop-record` policy; the migration loop catches it per
    record and skips that document/key. Top-level position only."""


def _const(defs):
    """Accept either a mutable `Definitions` or an already-const `DefinitionsConst`
    (as returned by `Database.definitions()`)."""
    return defs.const() if hasattr(defs, "const") else defs


class DefinitionsTransformer:
    _commit_id_remap = None            # {src commit repr -> new ValueCommitId}, set during a DAG replay

    def __init__(self, source_defs, target_defs, directives):
        self.source = _const(source_defs)
        self.target = _const(target_defs)
        self.d = directives
        self.att_map = {}
        self._build_maps()
        self._shape_guard()
        self._policy_completeness()

    # -- build the target Definitions from source + directives (definitions ⇒
    #    definitions), then wire the transformer against it. Returns (tr, target).
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
                sc = s.check(fname).type().type_code()
                tc = new_type.type_code()
                if (sc in ("vec", "mat") or tc in ("vec", "mat")) and \
                        new_type.representation() != s.check(fname).type().representation():
                    raise ValueError(f"[unsupported] Vec/Mat retype on "
                                     f"{s.representation()}.{fname} ({sc}->{tc}) is not "
                                     f"supported — carry it verbatim (no directive) or model "
                                     f"the change as a new field")
                class_a = (sc, tc) in WIDENING or tc == "string"   # widen or format
                if not class_a and policy is None:
                    raise ValueError(f"[policy-completeness] {sc}->{tc} on "
                                     f"{s.representation()}.{fname} needs a decreed policy")
        for e in self.source.enumerations():
            for case, policy in self.d.removed_cases.get(e.representation(), {}).items():
                if policy is None:
                    raise ValueError(f"[policy-completeness] removed case "
                                     f"{e.representation()}.{case} needs a decreed policy")

    # -- type ⇒ type
    def map_type(self, t):
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

    # -- retype dispatcher: structural (unwrap, Vector→Set) + leaf (widen/narrow/
    #    format/parse). Class A converts automatically; Class B consults the policy
    #    ONLY on the offending value — in-domain values always convert exactly.
    def _retype(self, sv, tt, policy):
        sc, tc = sv.type_code(), tt.type_code()

        # -- structural
        if sc == "optional" and tc != "optional":                  # unwrap Optional<A> → A
            vo = V.ValueOptional.cast(sv)
            if vo.is_nil():
                return self._on_missing(tt, policy, "nil-unwrap")
            return self.value(vo.unwrap(encoded=False), tt)
        if sc == "vector" and tc == "set":                         # Vector → Set (collapse)
            et = V.TypeSet.cast(tt).element_type()
            out = V.ValueSet(tt)
            vv = V.ValueVector.cast(sv)
            for i in range(vv.size()):
                out.add(self.value(vv.at(i, encoded=False), et))
            return out

        # -- leaf
        if (sc, tc) in WIDENING:
            return V.Value.create(tt, V.Value.dumps(sv))           # A: widen (lossless)
        if tc == "string":
            return V.ValueString(str(V.Value.dumps(sv)))           # A: format (total)
        if sc == "string":
            return self._parse(sv, tt, policy)                     # B: parse
        native = V.Value.dumps(sv)                                 # numeric narrowing
        lo, hi = INT_RANGE.get(tc, (None, None))
        if lo is not None and lo <= native <= hi:
            return V.Value.create(tt, native)                      # in range → exact
        if policy is None or policy == "fail":
            raise ValueError(f"[Class-B] {sc}->{tc} out of range: {native}")
        if policy == "saturate":
            return V.Value.create(tt, max(lo, min(hi, native)))
        if isinstance(policy, tuple) and policy[0] == "default":
            return policy[1]
        raise ValueError(f"unknown policy {policy!r}")

    def _on_missing(self, tt, policy, kind):                       # nil-unwrap / parse-fail
        if policy is None or policy == "fail":
            raise ValueError(f"[Class-B] {kind}: absent value; decree a policy")
        if policy == "drop-record":
            raise DropRecord(kind)
        if isinstance(policy, tuple) and policy[0] == "default":
            return policy[1]
        raise ValueError(f"unknown policy {policy!r}")

    def _parse(self, sv, tt, policy):
        s, tc = V.Value.dumps(sv), tt.type_code()
        try:
            if tc in INT_RANGE:
                native = int(s)
                lo, hi = INT_RANGE[tc]
                if not (lo <= native <= hi):
                    raise ValueError("range")
            else:
                native = float(s)
            return V.Value.create(tt, native)
        except (ValueError, TypeError):
            return self._on_missing(tt, policy, "parse")

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
    def value(self, v, tt=None):
        if tt is None:
            tt = self.map_type(v.type())
        tc = v.type_code()

        if tc == "struct":
            vs = V.ValueStructure.cast(v)
            src = vs.type_structure()
            tgt = V.TypeStructure.cast(tt)
            fsrc = self._field_source(src, tgt)
            retypes = self.d.retyped_fields.get(src.representation(), {})
            adds = dict(self.d.added_fields.get(src.representation(), []))
            out = {}
            for f in tgt.fields():
                sn = fsrc[f.name()]
                if sn is None:                                     # added → the directive's
                    # seed value (NOT f.default_value(): the runtime normalizes a
                    # type-zero default to no-default, which would lose the seed)
                    out[f.name()] = adds.get(f.name(), f.default_value())
                elif sn in retypes:                                # family 2: retype
                    new_type, policy = retypes[sn]
                    out[f.name()] = self._retype(vs.at(sn, encoded=False), new_type, policy)
                else:                                              # kept/renamed → recurse
                    out[f.name()] = self.value(vs.at(sn, encoded=False), f.type())
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
            return V.ValueAny() if va.is_nil() else V.ValueAny(self.value(va.unwrap(encoded=False)))

        if tc == "enum":
            ve = V.ValueEnumeration.cast(v)
            erepr = ve.type_enumeration().representation()
            name = ve.name()
            removed = self.d.removed_cases.get(erepr, {})
            if name in removed:                                    # Class B: removed case populated
                policy = removed[name]
                if isinstance(policy, tuple) and policy[0] == "map-case":
                    name = policy[1]
                elif policy == "drop-record":
                    raise DropRecord("remove-case")
                else:                                              # fail / None
                    raise ValueError(f"[Class-B] removed case populated: {name}")
            else:
                name = self.d.case_renames.get(erepr, {}).get(name, name)
            return V.ValueEnumeration(V.TypeEnumeration.cast(tt), name)

        if tc == "optional":
            vo = V.ValueOptional.cast(v)
            et = V.TypeOptional.cast(tt).element_type()
            if vo.is_nil():
                return V.ValueOptional(tt)
            return V.ValueOptional(tt, self.value(vo.unwrap(encoded=False), et))

        if tc == "vector":
            vv = V.ValueVector.cast(v)
            et = V.TypeVector.cast(tt).element_type()
            out = V.ValueVector(tt)
            for i in range(vv.size()):
                out.append(self.value(vv.at(i, encoded=False), et))
            return out

        if tc == "set":
            vs = V.ValueSet.cast(v)
            et = V.TypeSet.cast(tt).element_type()
            out = V.ValueSet(tt)
            for i in range(vs.size()):
                ne = self.value(vs.at(i, encoded=False), et)
                if ne in out:                                  # Class B: element collapse
                    if self.d.collision_policy == "fail":      # non-injective element migration
                        raise ValueError(f"[Class-B] set element collapse: "
                                         f"{ne.representation()} — a non-injective element "
                                         f"migration would silently drop a member; decree "
                                         f"resolve_collisions('first'|'last')")
                    continue                                   # first/last both = collapse to one
                out.add(ne)
            return out

        if tc == "map":
            vm = V.ValueMap.cast(v)
            mt = V.TypeMap.cast(tt)
            kt, et = mt.key_type(), mt.element_type()
            out = V.ValueMap(tt)
            for k, val in vm.items(encoded=False):
                nk, nv = self.value(k, kt), self.value(val, et)
                if out.contains(nk):                           # Class B: key collision
                    pol = self.d.collision_policy
                    if pol == "last":
                        out.set(nk, nv)                        # overwrite
                    elif pol == "first":
                        pass                                   # keep existing, drop this
                    else:                                      # fail
                        raise ValueError(f"[Class-B] map key collision: {nk.representation()}")
                else:
                    out.set(nk, nv)
            return out

        if tc == "tuple":
            vt = V.ValueTuple.cast(v)
            ets = V.TypeTuple.cast(tt).types()
            return V.ValueTuple(tt, [self.value(vt.at(i, encoded=False), ets[i])
                                     for i in range(vt.size())])

        if tc == "variant":
            vv = V.ValueVariant.cast(v)
            inner = self.value(vv.unwrap(encoded=False))
            out = V.ValueVariant(tt)
            out.wrap(inner, inner.type())
            return out

        if tc == "xarray":
            # Trans-definitions (de)serialization of the XArray, ATOMIC: source's
            # layout (positions + tombstones) is copied opaquely inside rebuild_from,
            # and the elements — re-mapped to the target domain here — are installed in
            # the SAME step. `out` never passes through a partial state.
            vx = V.ValueXArray.cast(v)
            et = V.TypeXArray.cast(tt).element_type()
            out = V.ValueXArray(tt)
            out.rebuild_from(vx, [(pos, self.value(val, et)) for pos, val in vx.items(encoded=False)])
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


# ------------------------------------------- definitions ⇒ definitions (target)
def build_target_definitions(source_defs, directives):
    """Construct the target `Definitions` from source + directives, in dependency
    order (concepts by parent, structures by field-type refs), minting the id map
    by construction. Returns (target_defs, tmap, att_map)."""
    for struct_repr, adds in directives.added_fields.items():
        for name, default_value in adds:
            if not _default_domain_free(default_value.type()):
                raise ValueError(f"[unsupported] add_field('{name}') default of type "
                                 f"{default_value.type().representation()} references a named "
                                 f"type — a composite default is not expressible across a "
                                 f"migration (it would carry stale source ids); use a "
                                 f"primitive-leaf default")
    src = _const(source_defs)
    target = V.Definitions()
    tmap = {}

    def simple(src_type):                    # target simple name after type rename
        full = src_type.representation()
        return directives.type_renames.get(full, full).split("::")[-1]

    def tgt_ns(src_type):                    # target namespace: name axis + uuid axis
        sns = src_type.type_name().name_space()
        key = sns.uuid().representation()
        new_uuid = directives.namespace_uuids.get(key, sns.uuid())
        new_name = directives.namespace_names.get(key, sns.name())
        return V.NameSpace(new_uuid, new_name)

    ELEM = {"optional": V.TypeOptional, "vector": V.TypeVector, "set": V.TypeSet,
            "xarray": V.TypeXArray, "key": V.TypeKey}

    def map_t(t):
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

    # concepts — topological by parent
    pending = list(src.concepts())
    while pending:
        still, progressed = [], False
        for c in pending:
            p = c.parent()
            if p is None or p.runtime_id().representation() in tmap:
                if p is None:
                    nc = target.create_concept(tgt_ns(c), simple(c))
                else:
                    nc = target.create_concept(tgt_ns(c), simple(c),
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
        ncl = target.create_club(tgt_ns(cl), simple(cl))
        tmap[cl.runtime_id().representation()] = ncl
        for member in cl.members():
            target.create_membership(ncl, tmap[member.runtime_id().representation()])

    # enumerations — cases renamed / removed / added / reordered
    for e in src.enumerations():
        cren = directives.case_renames.get(e.representation(), {})
        removed = directives.removed_cases.get(e.representation(), {})
        names = [cren.get(c.name(), c.name()) for c in e.cases() if c.name() not in removed]
        names += directives.added_cases.get(e.representation(), [])          # add at end
        order = directives.case_order.get(e.representation())
        if order:
            if set(order) != set(names):
                raise ValueError(f"reorder_cases({e.representation()}) not a permutation of {names}")
            names = order
        de = V.TypeEnumerationDescriptor(simple(e))
        for name in names:
            de.add_case(name)
        tmap[e.runtime_id().representation()] = target.create_enumeration(tgt_ns(e), de)

    # structures — topological by field-type dependencies, fields renamed
    pending = list(src.structures())
    while pending:
        still, progressed = [], False
        for s in pending:
            if all(ready(f.type()) for f in s.fields()):
                fren = directives.field_renames.get(s.representation(), {})
                drops = directives.dropped_fields.get(s.representation(), set())
                retypes = directives.retyped_fields.get(s.representation(), {})
                adds = directives.added_fields.get(s.representation(), [])
                fields = []                                         # (target name, Type or default Value)
                for f in s.fields():
                    if f.name() in drops:                          # family 2: drop
                        continue
                    entry = retypes.get(f.name())                   # family 2: retype
                    ftype = entry[0] if entry else map_t(f.type())
                    fields.append((fren.get(f.name(), f.name()), ftype))
                for name, default_value in adds:                    # family 2: add + default
                    fields.append((name, default_value))
                order = directives.field_order.get(s.representation())
                if order:                                           # family 2: reorder
                    by_name = {n: (n, t) for n, t in fields}
                    if set(order) != set(by_name):
                        raise ValueError(f"reorder_fields({s.representation()}) not a permutation of {list(by_name)}")
                    fields = [by_name[n] for n in order]
                ds = V.TypeStructureDescriptor(simple(s))
                for name, t in fields:
                    ds.add_field(name, t)
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
        local = a.identifier().split(".")[-1]            # identifier() is fully-qualified
        name = directives.attachment_renames.get(local, local)
        na = target.create_attachment(tgt_ns(a), name,
                                      map_t(a.key_type()), map_t(a.document_type()))
        att_map[a.runtime_id().representation()] = na

    return target, tmap, att_map
