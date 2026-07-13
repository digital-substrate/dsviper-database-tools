"""Engine tests — the target-directed rewrite over the dsviper binding.

Source + target `Definitions` are built programmatically; values are rewritten and
round-tripped through the real codec against the target registry (well-formed in the
new registry, not merely constructed). See ARCHITECTURE.md for the algorithm.
"""

import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsTransformer, DropRecord)

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


def rt(tr, target, value, source_type):
    """Round-trip a rewritten value through the real codec vs the target defs."""
    return V.Value.decode(V.Value.encode(value), tr.map_type(source_type), target.const())


class TestFamily1(unittest.TestCase):
    def test_rename_field_moves_id_keeps_data(self):
        src = V.Definitions()
        s = struct(src, "Order", [("amount", T.INT32), ("note", T.STRING)])
        d = TransformationDirectives()
        d.rename_field(s.representation(), "amount", "total")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        r = tr.value(V.ValueStructure(s, {"amount": 42, "note": "x"}))
        back = V.ValueStructure.cast(rt(tr, target, r, s))
        self.assertEqual(42, back.at("total", encoded=False))
        self.assertEqual("x", back.at("note", encoded=False))
        self.assertNotEqual(s.runtime_id().representation(),
                            back.type().runtime_id().representation())

    def test_rename_type_restamps_key(self):
        src = V.Definitions()
        c = src.create_concept(NS, "Account")
        d = TransformationDirectives()
        d.rename_type(c.representation(), "Demo::UserAccount")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        rk = tr.value(V.ValueKey.create(c, "11111111-1111-1111-1111-111111111111"))
        self.assertEqual("Demo::UserAccount", rk.type_concept().representation())

    def test_any_restamped(self):
        src = V.Definitions()
        s = struct(src, "Address", [("x", T.INT32)])
        d = TransformationDirectives()
        d.rename_type(s.representation(), "Demo::PostalAddress")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        r = tr.value(V.ValueAny(V.ValueStructure(s, {"x": 7})))
        inner = V.ValueAny.cast(r).unwrap(encoded=False)
        self.assertEqual("Demo::PostalAddress", inner.type().representation())
        self.assertEqual(7, inner.at("x", encoded=False))

    U2 = "7c9e6679-7425-40de-944b-e07fc1f90ae7"

    def test_rename_namespace_changes_representation_not_id(self):
        # the NAME axis: new representation (Namespace::Type), same runtimeId
        src = V.Definitions()
        c = src.create_concept(NS, "Account")
        d = TransformationDirectives()
        d.rename_namespace(NS, "DemoRenamed")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        self.assertEqual("DemoRenamed::Account", tr.map_type(c).representation())
        self.assertEqual(c.runtime_id().representation(),
                         tr.map_type(c).runtime_id().representation())

    def test_remap_namespace_changes_id_not_representation(self):
        # the UUID axis: new runtimeId, same representation
        src = V.Definitions()
        c = src.create_concept(NS, "Account")
        d = TransformationDirectives()
        d.remap_namespace(NS, V.ValueUUId(self.U2))
        tr, target = DefinitionsTransformer.from_directives(src, d)
        self.assertEqual("Demo::Account", tr.map_type(c).representation())
        self.assertNotEqual(c.runtime_id().representation(),
                            tr.map_type(c).runtime_id().representation())
        rk = tr.value(V.ValueKey.create(c, "44444444-4444-4444-4444-444444444444"))
        self.assertEqual("Demo::Account", rk.type_concept().representation())

    def test_namespace_axes_compose(self):
        # both axes at once: new representation AND new runtimeId
        src = V.Definitions()
        c = src.create_concept(NS, "Account")
        d = TransformationDirectives()
        d.rename_namespace(NS, "DemoV2")
        d.remap_namespace(NS, V.ValueUUId(self.U2))
        tr, target = DefinitionsTransformer.from_directives(src, d)
        self.assertEqual("DemoV2::Account", tr.map_type(c).representation())
        self.assertNotEqual(c.runtime_id().representation(),
                            tr.map_type(c).runtime_id().representation())


class TestFamily2ClassA(unittest.TestCase):
    def test_add_field_with_default(self):
        src = V.Definitions()
        s = struct(src, "Cfg", [("a", T.INT32)])
        d = TransformationDirectives()
        d.add_field(s.representation(), "b", V.ValueString("default-b"))
        tr, target = DefinitionsTransformer.from_directives(src, d)
        back = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(s, {"a": 1})), s))
        self.assertEqual(1, back.at("a", encoded=False))
        self.assertEqual("default-b", back.at("b", encoded=False))

    def test_add_field_type_zero_default_is_seeded(self):
        # a type-zero default (empty string) must still seed the field: the runtime
        # normalizes it to no-default, but the directive's seed value wins
        src = V.Definitions()
        s = struct(src, "Cfg", [("a", T.INT32)])
        d = TransformationDirectives()
        d.add_field(s.representation(), "note", V.ValueString(""))
        tr, target = DefinitionsTransformer.from_directives(src, d)
        back = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(s, {"a": 1})), s))
        self.assertEqual("", back.at("note", encoded=False))

    def test_drop_field(self):
        src = V.Definitions()
        s = struct(src, "Cfg", [("a", T.INT32), ("legacy", T.STRING)])
        d = TransformationDirectives()
        d.drop_field(s.representation(), "legacy")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        self.assertEqual(["a"], [f.name() for f in target.const().structures()[0].fields()])
        back = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(s, {"a": 7, "legacy": "z"})), s))
        self.assertEqual(7, back.at("a", encoded=False))

    def test_reorder_fields(self):
        src = V.Definitions()
        s = struct(src, "R", [("a", T.INT32), ("b", T.STRING), ("c", T.INT32)])
        d = TransformationDirectives()
        d.reorder_fields(s.representation(), ["c", "b", "a"])
        tr, target = DefinitionsTransformer.from_directives(src, d)
        self.assertEqual(["c", "b", "a"], [f.name() for f in target.const().structures()[0].fields()])
        back = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(s, {"a": 1, "b": "x", "c": 9})), s))
        self.assertEqual((1, 9), (back.at("a", encoded=False), back.at("c", encoded=False)))

    def test_add_and_reorder_cases(self):
        src = V.Definitions()
        ed = V.TypeEnumerationDescriptor("E")
        for x in ("A", "B", "C"):
            ed.add_case(x)
        e = src.create_enumeration(NS, ed)
        s = struct(src, "R", [("m", e)])
        d = TransformationDirectives()
        d.add_case(e.representation(), "D")
        d.reorder_cases(e.representation(), ["D", "C", "A", "B"])
        tr, target = DefinitionsTransformer.from_directives(src, d)
        self.assertEqual(["D", "C", "A", "B"],
                         [c.name() for c in target.const().enumerations()[0].cases()])
        back = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(s, {"m": V.ValueEnumeration(e, "A")})), s))
        self.assertEqual("A", V.ValueEnumeration.cast(back.at("m", encoded=False)).name())

    def test_widen_int32_to_int64(self):
        src = V.Definitions()
        s = struct(src, "M", [("n", T.INT32)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "n", T.INT64)     # widening → automatic, no policy
        tr, target = DefinitionsTransformer.from_directives(src, d)
        back = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(s, {"n": 2147483647})), s))
        self.assertEqual(2147483647, back.at("n", encoded=False))


class TestClassBPolicies(unittest.TestCase):
    def _narrow(self, policy):
        src = V.Definitions()
        s = struct(src, "W", [("n", T.INT64)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "n", T.INT32, policy=policy)
        tr, target = DefinitionsTransformer.from_directives(src, d)
        return tr, s

    def test_narrowing_fail_in_range_exact(self):
        tr, s = self._narrow("fail")
        self.assertEqual(100, tr.value(V.ValueStructure(s, {"n": 100})).at("n", encoded=False))

    def test_narrowing_fail_out_of_range_aborts(self):
        tr, s = self._narrow("fail")
        with self.assertRaises(ValueError):
            tr.value(V.ValueStructure(s, {"n": 2**40}))

    def test_narrowing_saturate(self):
        tr, s = self._narrow("saturate")
        self.assertEqual(2**31 - 1, tr.value(V.ValueStructure(s, {"n": 2**40})).at("n", encoded=False))

    def test_narrowing_default(self):
        tr, s = self._narrow(("default", V.ValueInt32(-1)))
        self.assertEqual(-1, tr.value(V.ValueStructure(s, {"n": 2**40})).at("n", encoded=False))

    def test_policy_completeness_refuses_unpolicied_narrowing(self):
        src = V.Definitions()
        s = struct(src, "W", [("n", T.INT64)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "n", T.INT32)     # no policy
        with self.assertRaises(ValueError):
            DefinitionsTransformer.from_directives(src, d)

    def test_string_format_and_parse(self):
        src = V.Definitions()
        s = struct(src, "R", [("n", T.INT32)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "n", T.STRING)    # X→string: Class A
        tr, _ = DefinitionsTransformer.from_directives(src, d)
        self.assertEqual("42", tr.value(V.ValueStructure(s, {"n": 42})).at("n", encoded=False))

        src2 = V.Definitions()
        s2 = struct(src2, "R", [("n", T.STRING)])
        d2 = TransformationDirectives()
        d2.retype_field(s2.representation(), "n", T.INT32, policy=("default", V.ValueInt32(-1)))
        tr2, _ = DefinitionsTransformer.from_directives(src2, d2)
        self.assertEqual(7, tr2.value(V.ValueStructure(s2, {"n": "7"})).at("n", encoded=False))
        self.assertEqual(-1, tr2.value(V.ValueStructure(s2, {"n": "abc"})).at("n", encoded=False))

    def test_optional_unwrap(self):
        src = V.Definitions()
        s = struct(src, "R", [("x", V.TypeOptional(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "x", T.INT32, policy=("default", V.ValueInt32(0)))
        tr, _ = DefinitionsTransformer.from_directives(src, d)
        ot = V.TypeOptional(T.INT32)
        self.assertEqual(9, tr.value(V.ValueStructure(s, {"x": V.ValueOptional(ot, 9)})).at("x", encoded=False))
        self.assertEqual(0, tr.value(V.ValueStructure(s, {"x": V.ValueOptional(ot)})).at("x", encoded=False))

    def test_drop_record_signal(self):
        src = V.Definitions()
        s = struct(src, "R", [("x", V.TypeOptional(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "x", T.INT32, policy="drop-record")
        tr, _ = DefinitionsTransformer.from_directives(src, d)
        with self.assertRaises(DropRecord):
            tr.value(V.ValueStructure(s, {"x": V.ValueOptional(V.TypeOptional(T.INT32))}))

    def test_vector_to_set_collapse(self):
        src = V.Definitions()
        s = struct(src, "R", [("tags", V.TypeVector(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "tags", V.TypeSet(T.INT32), policy="collapse")
        tr, _ = DefinitionsTransformer.from_directives(src, d)
        vec = V.ValueVector(V.TypeVector(T.INT32))
        for x in (1, 2, 2, 3, 3, 3):
            vec.append(x)
        out = tr.value(V.ValueStructure(s, {"tags": vec}))
        self.assertEqual(3, V.ValueSet.cast(out.at("tags", encoded=False)).size())

    def test_remove_case_map_case(self):
        src = V.Definitions()
        ed = V.TypeEnumerationDescriptor("Mode")
        ed.add_case("Old"); ed.add_case("New")
        e = src.create_enumeration(NS, ed)
        s = struct(src, "R", [("m", e)])
        d = TransformationDirectives()
        d.remove_case(e.representation(), "Old", ("map-case", "New"))
        tr, _ = DefinitionsTransformer.from_directives(src, d)
        mv = tr.value(V.ValueStructure(s, {"m": V.ValueEnumeration(e, "Old")})).at("m", encoded=False)
        self.assertEqual("New", V.ValueEnumeration.cast(mv).name())

    def test_map_key_collision_winner_discriminates(self):
        def setup(winner):
            src = V.Definitions()
            ed = V.TypeEnumerationDescriptor("Mode")
            ed.add_case("Old"); ed.add_case("New")
            e = src.create_enumeration(NS, ed)
            s = struct(src, "R", [("cfgs", V.TypeMap(e, T.INT32))])
            d = TransformationDirectives()
            d.remove_case(e.representation(), "Old", ("map-case", "New"))
            if winner:
                d.resolve_collisions(winner)
            tr, target = DefinitionsTransformer.from_directives(src, d)
            mv = V.ValueMap(V.TypeMap(e, T.INT32))
            mv.set(V.ValueEnumeration(e, "Old"), 1)
            mv.set(V.ValueEnumeration(e, "New"), 2)
            return tr, target, s, mv

        tr, target, s, mv = setup(None)
        with self.assertRaises(ValueError):                  # fail (default) → aborts
            tr.value(V.ValueStructure(s, {"cfgs": mv}))

        def survivor(winner):
            tr, target, s, mv = setup(winner)
            om = V.ValueMap.cast(tr.value(V.ValueStructure(s, {"cfgs": mv})).at("cfgs", encoded=False))
            self.assertEqual(1, om.size())
            tgt_enum = target.const().enumerations()[0]
            return om.at(V.ValueEnumeration(tgt_enum, "New"), encoded=False)

        self.assertEqual({survivor("first"), survivor("last")}, {1, 2})


class TestGuards(unittest.TestCase):
    def test_shape_guard_refuses_added_field_via_external_target(self):
        src = V.Definitions()
        tgt = V.Definitions()
        struct(src, "Rec", [("a", T.INT32)])
        struct(tgt, "Rec", [("a", T.INT32), ("b", T.STRING)])
        with self.assertRaises(ValueError):
            DefinitionsTransformer(src, tgt, TransformationDirectives())


class TestContainers(unittest.TestCase):
    def test_nested_vector_optional_struct(self):
        src = V.Definitions()
        s_it = struct(src, "Item", [("qty", T.INT32)])
        d = TransformationDirectives()
        d.rename_field(s_it.representation(), "qty", "count")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        vec_t = V.TypeVector(V.TypeOptional(s_it))
        vec = V.ValueVector(vec_t)
        vec.append(V.ValueOptional(V.TypeOptional(s_it), V.ValueStructure(s_it, {"qty": 3})))
        vec.append(V.ValueOptional(V.TypeOptional(s_it)))
        r = V.ValueVector.cast(tr.value(vec))
        got = V.ValueOptional.cast(r.at(0, encoded=False)).unwrap(encoded=False)
        self.assertEqual(3, got.at("count", encoded=False))
        self.assertTrue(V.ValueOptional.cast(r.at(1, encoded=False)).is_nil())

    def test_tuple_and_variant(self):
        src = V.Definitions()
        s_el = struct(src, "P", [("x", T.INT32)])
        s_rt = struct(src, "Rt", [("t", V.TypeTuple([T.INT32, s_el])),
                                  ("var", V.TypeVariant([T.INT32, s_el]))])
        d = TransformationDirectives()
        d.rename_field(s_el.representation(), "x", "xx")
        tr, _ = DefinitionsTransformer.from_directives(src, d)
        tup = V.ValueTuple(V.TypeTuple([T.INT32, s_el]), [5, V.ValueStructure(s_el, {"x": 7})])
        var = V.ValueVariant(V.TypeVariant([T.INT32, s_el]), V.ValueStructure(s_el, {"x": 9}))
        r = tr.value(V.ValueStructure(s_rt, {"t": tup, "var": var}))
        rtup = V.ValueTuple.cast(r.at("t", encoded=False))
        self.assertEqual(5, rtup.at(0, encoded=False))
        self.assertEqual(7, V.ValueStructure.cast(rtup.at(1, encoded=False)).at("xx", encoded=False))
        rvar = V.ValueVariant.cast(r.at("var", encoded=False))
        self.assertEqual(9, V.ValueStructure.cast(rvar.unwrap(encoded=False)).at("xx", encoded=False))

    def test_vec_and_mat_verbatim(self):
        src = V.Definitions()
        s = struct(src, "Sample", [("p", V.TypeVec(T.FLOAT, 3)),
                               ("m", V.TypeMat(T.DOUBLE, 2, 2)),
                               ("label", T.STRING)])
        d = TransformationDirectives()
        d.rename_field(s.representation(), "label", "name")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        doc = V.ValueStructure(s, {"p": V.ValueVec(V.TypeVec(T.FLOAT, 3), [1.0, 2.0, 3.0]),
                                   "m": V.ValueMat(V.TypeMat(T.DOUBLE, 2, 2), [[1.0, 2.0], [3.0, 4.0]]),
                                   "label": "x"})
        back = V.ValueStructure.cast(rt(tr, target, tr.value(doc), s))
        self.assertEqual((1.0, 2.0, 3.0), V.Value.dumps(back.at("p", encoded=False)))
        self.assertEqual(((1.0, 2.0), (3.0, 4.0)), V.Value.dumps(back.at("m", encoded=False)))

    def test_xarray_with_tombstone(self):
        src = V.Definitions()
        s_el = struct(src, "Node", [("v", T.INT32)])
        s = struct(src, "R", [("nodes", V.TypeXArray(s_el))])
        d = TransformationDirectives()
        d.rename_field(s_el.representation(), "v", "val")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        xa = V.ValueXArray(V.TypeXArray(s_el))
        xa.insert(V.ValueXArray.END, V.ValueStructure(s_el, {"v": 10}), V.ValueXArray.create_position())
        p1 = xa.insert(V.ValueXArray.END, V.ValueStructure(s_el, {"v": 20}), V.ValueXArray.create_position())
        xa.insert(V.ValueXArray.END, V.ValueStructure(s_el, {"v": 30}), V.ValueXArray.create_position())
        xa.disable_position(p1)
        back = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(s, {"nodes": xa})), s))
        rx = V.ValueXArray.cast(back.at("nodes", encoded=False))
        self.assertEqual([p.representation() for p in xa.positions()],
                         [p.representation() for p in rx.positions()])
        self.assertEqual([10, 30], [V.ValueStructure.cast(v).at("val", encoded=False) for _, v in rx.items()])


class TestAttachments(unittest.TestCase):
    def test_attachment_created_and_renamed(self):
        src = V.Definitions()
        c = src.create_concept(NS, "Customer")
        s_doc = struct(src, "Order", [("qty", T.INT32)])
        att = src.create_attachment(NS, "Orders", c, s_doc)
        d = TransformationDirectives()
        d.rename_field(s_doc.representation(), "qty", "count")
        d.rename_attachment("Orders", "OrderLog")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        tatt = target.const().attachments()[0]
        self.assertEqual("OrderLog", tatt.identifier().split(".")[-1])
        self.assertEqual(tatt.runtime_id().representation(),
                         tr.attachment(att).runtime_id().representation())
        r = tr.value(V.ValueStructure(s_doc, {"qty": 5}))
        self.assertEqual(5, r.at("count", encoded=False))


class TestKeyFlavors(unittest.TestCase):
    """A ValueKey is (typeKey, typeConcept, instanceId), typeKey = Key<X> with
    X in {concept, club, any-concept}. The rewrite must preserve the flavour under
    a concept rename — create() alone yields a concept key and would downgrade
    club / any-concept keys, breaking attachment conformance downstream."""

    INST = "11111111-1111-1111-1111-111111111111"

    def _setup(self):
        src = V.Definitions()
        parent = src.create_concept(NS, "Material")
        concept = src.create_concept(NS, "MaterialStandard", parent=parent)
        club = src.create_club(NS, "Certified")
        src.create_membership(club, concept)
        d = TransformationDirectives()
        d.rename_type("Demo::MaterialStandard", "Demo::StandardMaterial")
        tr, _ = DefinitionsTransformer.from_directives(src, d)
        return concept, club, tr

    @staticmethod
    def _flavor(k):
        tk = k.type_key()
        return ("concept" if tk.is_concept() else "club" if tk.is_club()
                else "any-concept" if tk.is_any_concept() else "?")

    def test_concept_key_preserved_and_remapped(self):
        concept, _club, tr = self._setup()
        out = tr.value(V.ValueKey.create(concept, self.INST))
        self.assertEqual("concept", self._flavor(out))
        self.assertEqual("Demo::StandardMaterial", out.type_concept().representation())

    def test_any_concept_key_flavor_preserved(self):
        concept, _club, tr = self._setup()
        out = tr.value(V.ValueKey.create(concept, self.INST).to_any_concept_key())
        self.assertEqual("any-concept", self._flavor(out))
        self.assertEqual("Demo::StandardMaterial", out.type_concept().representation())

    def test_club_key_flavor_preserved(self):
        concept, club, tr = self._setup()
        out = tr.value(V.ValueKey.create(concept, self.INST).to_club_key(club))
        self.assertEqual("club", self._flavor(out))

    def test_instance_id_is_stable(self):
        concept, _club, tr = self._setup()
        out = tr.value(V.ValueKey.create(concept, self.INST))
        self.assertEqual(self.INST, out.instance_id().representation())


class TestValueLevelRobustness(unittest.TestCase):
    """The engine is total-or-explicit-refusal: every value-level case either
    migrates faithfully or refuses cleanly at construction — never silent loss."""

    # VL11 — Any migrates its inner value by recursion, INCLUDING a shape change
    def test_any_inner_shape_change_migrates(self):
        src = V.Definitions()
        s = struct(src, "Payload", [("keep", T.INT32), ("legacy", T.STRING)])
        d = TransformationDirectives()
        d.drop_field(s.representation(), "legacy")
        tr, target = DefinitionsTransformer.from_directives(src, d)
        out = tr.value(V.ValueAny(V.ValueStructure(s, {"keep": 7, "legacy": "gone"})))
        inner = V.ValueStructure.cast(V.ValueAny.cast(out).unwrap(encoded=False))
        self.assertEqual(["keep"], [f.name() for f in inner.type_structure().fields()])
        self.assertEqual(7, inner.at("keep", encoded=False))

    # VL8 — a Set whose element migration is non-injective would silently collapse;
    #        the engine refuses unless the collapse is decreed.
    def _set_merge(self, winner):
        src = V.Definitions()
        ed = V.TypeEnumerationDescriptor("Mode")
        ed.add_case("Old"); ed.add_case("New")
        e = src.create_enumeration(NS, ed)
        s = struct(src, "R", [("modes", V.TypeSet(e))])
        d = TransformationDirectives()
        d.remove_case(e.representation(), "Old", ("map-case", "New"))
        if winner:
            d.resolve_collisions(winner)
        tr, _ = DefinitionsTransformer.from_directives(src, d)
        st = V.ValueSet(V.TypeSet(e))
        st.add(V.ValueEnumeration(e, "Old"))
        st.add(V.ValueEnumeration(e, "New"))
        return tr, s, st

    def test_set_collapse_refused_without_policy(self):
        tr, s, st = self._set_merge(None)
        with self.assertRaises(ValueError):
            tr.value(V.ValueStructure(s, {"modes": st}))

    def test_set_collapse_authorized_with_policy(self):
        tr, s, st = self._set_merge("last")
        out = V.ValueSet.cast(tr.value(V.ValueStructure(s, {"modes": st})).at("modes", encoded=False))
        self.assertEqual(1, out.size())
        self.assertEqual("New", V.ValueEnumeration.cast(out.at(0, encoded=False)).name())

    def test_set_injective_rename_needs_no_policy(self):
        src = V.Definitions()
        ed = V.TypeEnumerationDescriptor("Mode")
        ed.add_case("A"); ed.add_case("B")
        e = src.create_enumeration(NS, ed)
        s = struct(src, "R", [("modes", V.TypeSet(e))])
        d = TransformationDirectives()
        d.rename_case(e.representation(), "A", "Alpha")
        tr, _ = DefinitionsTransformer.from_directives(src, d)     # no collision policy
        st = V.ValueSet(V.TypeSet(e))
        st.add(V.ValueEnumeration(e, "A")); st.add(V.ValueEnumeration(e, "B"))
        out = V.ValueSet.cast(tr.value(V.ValueStructure(s, {"modes": st})).at("modes", encoded=False))
        self.assertEqual(2, out.size())

    # VL7 — a Vec/Mat element retype is refused cleanly at construction
    def test_vec_element_retype_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.FLOAT, 3))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVec(T.DOUBLE, 3), policy="saturate")
        with self.assertRaises(ValueError) as cm:
            DefinitionsTransformer.from_directives(src, d)
        self.assertIn("Vec/Mat", str(cm.exception))

    def test_vec_verbatim_still_ok(self):        # rename ≠ retype: Vec carried verbatim
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.FLOAT, 3)), ("label", T.STRING)])
        d = TransformationDirectives()
        d.rename_field(s.representation(), "label", "name")
        tr, _ = DefinitionsTransformer.from_directives(src, d)
        doc = V.ValueStructure(s, {"p": V.ValueVec(V.TypeVec(T.FLOAT, 3), [1.0, 2.0, 3.0]), "label": "x"})
        out = tr.value(doc)
        self.assertEqual((1.0, 2.0, 3.0), V.Value.dumps(out.at("p", encoded=False)))

    # VL14 — a composite add_field default (references a named type) is refused;
    #         a primitive-leaf default is accepted.
    def test_composite_default_refused(self):
        src = V.Definitions()
        inner = struct(src, "Inner", [("x", T.INT32)])
        host = struct(src, "Host", [("a", T.INT32)])
        d = TransformationDirectives()
        d.rename_type(inner.representation(), "Demo::InnerV2")
        d.add_field(host.representation(), "cfg", V.ValueStructure(inner, {"x": 9}))
        with self.assertRaises(ValueError) as cm:
            DefinitionsTransformer.from_directives(src, d)
        self.assertIn("add_field", str(cm.exception))

    def test_primitive_default_accepted(self):
        src = V.Definitions()
        host = struct(src, "Host", [("a", T.INT32)])
        d = TransformationDirectives()
        d.add_field(host.representation(), "note", V.ValueString("hi"))
        tr, target = DefinitionsTransformer.from_directives(src, d)
        back = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(host, {"a": 1})), host))
        self.assertEqual("hi", back.at("note", encoded=False))

    def test_xarray_primitive_default_accepted(self):
        # xarray of a primitive is domain-free (like vector) — accepted end-to-end
        src = V.Definitions()
        host = struct(src, "Host", [("a", T.INT32)])
        d = TransformationDirectives()
        d.add_field(host.representation(), "tags", V.ValueXArray(V.TypeXArray(T.INT32)))
        DefinitionsTransformer.from_directives(src, d)              # must not raise

    def test_domain_free_predicate_recurses_xarray_and_variant(self):
        # the domain-free gate answers only "references a named type?" — it recurses
        # into xarray and variant like vector/tuple (variant defaults are separately
        # refused by the runtime, an orthogonal concern this gate does not own)
        from dsviper_database_tools.rewrite import _default_domain_free
        self.assertTrue(_default_domain_free(V.TypeXArray(T.INT32)))
        self.assertTrue(_default_domain_free(V.TypeVariant([T.INT32, T.STRING])))

    def test_container_default_referencing_named_refused(self):
        # a container default whose element is a named (migrated) type is refused
        # early by the domain-free gate — xarray and variant recurse like vector/tuple
        for kind in ("xarray", "variant"):
            src = V.Definitions()
            inner = struct(src, "Inner", [("x", T.INT32)])
            host = struct(src, "Host", [("a", T.INT32)])
            if kind == "xarray":
                default = V.ValueXArray(V.TypeXArray(inner))
            else:
                default = V.ValueVariant(V.TypeVariant([T.INT32, inner]))
                default.wrap(V.ValueInt32(1), T.INT32)
            d = TransformationDirectives()
            d.add_field(host.representation(), "extra", default)
            with self.assertRaises(ValueError) as cm:
                DefinitionsTransformer.from_directives(src, d)
            self.assertIn("named type", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
