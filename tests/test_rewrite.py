"""Engine tests — the target-directed rewrite over the dsviper binding.

Source + target `Definitions` are built programmatically; values are rewritten and
round-tripped through the real codec against the target registry (well-formed in the
new registry, not merely constructed). See REWRITE.md for the algorithm.
"""

import math
import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsRewriter, Unrepresentable)

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


def rt(rewriter, target, value, source_type):
    """Round-trip a rewritten value through the real codec vs the target defs."""
    return V.Value.decode(V.Value.encode(value), rewriter.map_type(source_type), target.const())


class TestFamily1(unittest.TestCase):
    def test_rename_field_moves_id_keeps_data(self):
        src = V.Definitions()
        s = struct(src, "Order", [("amount", T.INT32), ("note", T.STRING)])
        d = TransformationDirectives()
        d.rename_field(s.representation(), "amount", "total")
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        r = rewriter.value(V.ValueStructure(s, {"amount": 42, "note": "x"}))
        back = V.ValueStructure.cast(rt(rewriter, target, r, s))
        self.assertEqual(42, back.at("total", encoded=False))
        self.assertEqual("x", back.at("note", encoded=False))
        self.assertNotEqual(s.runtime_id().representation(),
                            back.type().runtime_id().representation())

    def test_rename_type_restamps_key(self):
        src = V.Definitions()
        c = src.create_concept(NS, "Account")
        d = TransformationDirectives()
        d.rename_type(c.representation(), "Demo::UserAccount")
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        rk = rewriter.value(V.ValueKey.create(c, "11111111-1111-1111-1111-111111111111"))
        self.assertEqual("Demo::UserAccount", rk.type_concept().representation())

    def test_any_restamped(self):
        src = V.Definitions()
        s = struct(src, "Address", [("x", T.INT32)])
        d = TransformationDirectives()
        d.rename_type(s.representation(), "Demo::PostalAddress")
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        r = rewriter.value(V.ValueAny(V.ValueStructure(s, {"x": 7})))
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
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        self.assertEqual("DemoRenamed::Account", rewriter.map_type(c).representation())
        self.assertEqual(c.runtime_id().representation(),
                         rewriter.map_type(c).runtime_id().representation())

    def test_remap_namespace_changes_id_not_representation(self):
        # the UUID axis: new runtimeId, same representation
        src = V.Definitions()
        c = src.create_concept(NS, "Account")
        d = TransformationDirectives()
        d.remap_namespace(NS, V.ValueUUId(self.U2))
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        self.assertEqual("Demo::Account", rewriter.map_type(c).representation())
        self.assertNotEqual(c.runtime_id().representation(),
                            rewriter.map_type(c).runtime_id().representation())
        rk = rewriter.value(V.ValueKey.create(c, "44444444-4444-4444-4444-444444444444"))
        self.assertEqual("Demo::Account", rk.type_concept().representation())

    def test_namespace_axes_compose(self):
        # both axes at once: new representation AND new runtimeId
        src = V.Definitions()
        c = src.create_concept(NS, "Account")
        d = TransformationDirectives()
        d.rename_namespace(NS, "DemoV2")
        d.remap_namespace(NS, V.ValueUUId(self.U2))
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        self.assertEqual("DemoV2::Account", rewriter.map_type(c).representation())
        self.assertNotEqual(c.runtime_id().representation(),
                            rewriter.map_type(c).runtime_id().representation())


class TestFamily2ClassA(unittest.TestCase):
    def test_add_field_with_default(self):
        src = V.Definitions()
        s = struct(src, "Cfg", [("a", T.INT32)])
        d = TransformationDirectives()
        d.add_field(s.representation(), "b", V.ValueString("default-b"))
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"a": 1})), s))
        self.assertEqual(1, back.at("a", encoded=False))
        self.assertEqual("default-b", back.at("b", encoded=False))

    def test_add_field_type_zero_default_is_seeded(self):
        # a type-zero default (empty string) must still seed the field: the runtime
        # normalizes it to no-default, but the directive's seed value wins
        src = V.Definitions()
        s = struct(src, "Cfg", [("a", T.INT32)])
        d = TransformationDirectives()
        d.add_field(s.representation(), "note", V.ValueString(""))
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"a": 1})), s))
        self.assertEqual("", back.at("note", encoded=False))

    def test_drop_field(self):
        src = V.Definitions()
        s = struct(src, "Cfg", [("a", T.INT32), ("legacy", T.STRING)])
        d = TransformationDirectives()
        d.drop_field(s.representation(), "legacy")
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        self.assertEqual(["a"], [f.name() for f in target.const().structures()[0].fields()])
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"a": 7, "legacy": "z"})), s))
        self.assertEqual(7, back.at("a", encoded=False))

    def test_reorder_fields(self):
        src = V.Definitions()
        s = struct(src, "R", [("a", T.INT32), ("b", T.STRING), ("c", T.INT32)])
        d = TransformationDirectives()
        d.reorder_fields(s.representation(), ["c", "b", "a"])
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        self.assertEqual(["c", "b", "a"], [f.name() for f in target.const().structures()[0].fields()])
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"a": 1, "b": "x", "c": 9})), s))
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
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        self.assertEqual(["D", "C", "A", "B"],
                         [c.name() for c in target.const().enumerations()[0].cases()])
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"m": V.ValueEnumeration(e, "A")})), s))
        self.assertEqual("A", V.ValueEnumeration.cast(back.at("m", encoded=False)).name())

    def test_set_to_vector_is_class_a(self):
        # Set → Vector: every element preserved in the set's canonical (total-order) order —
        # lossless, no policy required (the dual of Vector→Set, which loses order + dups).
        src = V.Definitions()
        s = struct(src, "R", [("tags", V.TypeSet(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "tags", V.TypeVector(T.INT32))   # NO policy
        tr, target = DefinitionsRewriter.from_directives(src, d)            # must not require one
        st = V.ValueSet(V.TypeSet(T.INT32))
        for x in (3, 1, 2):
            st.add(x)
        out = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(s, {"tags": st})), s))
        vec = V.ValueVector.cast(out.at("tags", encoded=False))
        self.assertEqual(3, vec.size())
        self.assertEqual({1, 2, 3}, {vec.at(i, encoded=False) for i in range(vec.size())})

    def test_vector_to_xarray_is_class_a_and_deterministic(self):
        # Vector → XArray: order preserved, no policy — but positions must be DETERMINISTIC
        # (index-derived), else verify's re-derivation would not match the migration.
        src2 = V.Definitions()
        s2 = struct(src2, "R", [("items", V.TypeVector(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s2.representation(), "items", V.TypeXArray(T.INT32))     # NO policy
        tr, target = DefinitionsRewriter.from_directives(src2, d)
        vec = V.ValueVector(V.TypeVector(T.INT32))
        for x in (10, 20, 30):
            vec.append(x)
        doc = V.ValueStructure(s2, {"items": vec})
        self.assertEqual(tr.value(doc), tr.value(doc))                         # deterministic (verify-safe)
        back = V.ValueStructure.cast(rt(tr, target, tr.value(doc), s2))        # round-trips in target
        xa = V.ValueXArray.cast(back.at("items", encoded=False))
        self.assertEqual([10, 20, 30], [xa.at(p, encoded=False) for p in xa.positions()
                                        if xa.at(p, encoded=False) is not None])

    def test_xarray_to_vector_is_class_a(self):
        # XArray → Vector (dual): live elements in position order, positions/tombstones
        # dropped — lossless, no policy. Reuses the runtime's to_vector().
        src = V.Definitions()
        s = struct(src, "R", [("items", V.TypeXArray(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "items", V.TypeVector(T.INT32))   # NO policy
        tr, target = DefinitionsRewriter.from_directives(src, d)
        xa = V.ValueXArray(V.TypeXArray(T.INT32))
        p = xa.insert(V.ValueXArray.END, V.ValueInt32(10), V.ValueXArray.create_position())
        xa.insert(V.ValueXArray.END, V.ValueInt32(20), V.ValueXArray.create_position())
        xa.insert(V.ValueXArray.END, V.ValueInt32(30), V.ValueXArray.create_position())
        xa.disable_position(p)                                              # tombstone the first
        back = V.ValueStructure.cast(rt(tr, target, tr.value(V.ValueStructure(s, {"items": xa})), s))
        vec = V.ValueVector.cast(back.at("items", encoded=False))
        self.assertEqual([20, 30], [vec.at(i, encoded=False) for i in range(vec.size())])  # live only, in order

    def test_widen_int32_to_int64(self):
        src = V.Definitions()
        s = struct(src, "M", [("n", T.INT32)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "n", T.INT64)     # widening → automatic, no policy
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"n": 2147483647})), s))
        self.assertEqual(2147483647, back.at("n", encoded=False))


class TestClassBPolicies(unittest.TestCase):
    def _narrow(self, policy):
        src = V.Definitions()
        s = struct(src, "W", [("n", T.INT64)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "n", T.INT32, policy=policy)
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        return rewriter, s

    def test_narrowing_fail_in_range_exact(self):
        rewriter, s = self._narrow("fail")
        self.assertEqual(100, rewriter.value(V.ValueStructure(s, {"n": 100})).at("n", encoded=False))

    def test_narrowing_fail_out_of_range_aborts(self):
        rewriter, s = self._narrow("fail")
        with self.assertRaises(ValueError):
            rewriter.value(V.ValueStructure(s, {"n": 2**40}))

    def test_narrowing_saturate(self):
        rewriter, s = self._narrow("saturate")
        self.assertEqual(2**31 - 1, rewriter.value(V.ValueStructure(s, {"n": 2**40})).at("n", encoded=False))

    def test_narrowing_default(self):
        rewriter, s = self._narrow(("default", V.ValueInt32(-1)))
        self.assertEqual(-1, rewriter.value(V.ValueStructure(s, {"n": 2**40})).at("n", encoded=False))

    def test_policy_completeness_refuses_unpolicied_narrowing(self):
        src = V.Definitions()
        s = struct(src, "W", [("n", T.INT64)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "n", T.INT32)     # no policy
        with self.assertRaises(ValueError):
            DefinitionsRewriter.from_directives(src, d)

    def test_string_format_and_parse(self):
        src = V.Definitions()
        s = struct(src, "R", [("n", T.INT32)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "n", T.STRING)    # X→string: Class A
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        self.assertEqual("42", rewriter.value(V.ValueStructure(s, {"n": 42})).at("n", encoded=False))

        src2 = V.Definitions()
        s2 = struct(src2, "R", [("n", T.STRING)])
        d2 = TransformationDirectives()
        d2.retype_field(s2.representation(), "n", T.INT32, policy=("default", V.ValueInt32(-1)))
        rewriter2, _ = DefinitionsRewriter.from_directives(src2, d2)
        self.assertEqual(7, rewriter2.value(V.ValueStructure(s2, {"n": "7"})).at("n", encoded=False))
        self.assertEqual(-1, rewriter2.value(V.ValueStructure(s2, {"n": "abc"})).at("n", encoded=False))

    def test_optional_unwrap(self):
        src = V.Definitions()
        s = struct(src, "R", [("x", V.TypeOptional(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "x", T.INT32, policy=("default", V.ValueInt32(0)))
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        ot = V.TypeOptional(T.INT32)
        self.assertEqual(9, rewriter.value(V.ValueStructure(s, {"x": V.ValueOptional(ot, 9)})).at("x", encoded=False))
        self.assertEqual(0, rewriter.value(V.ValueStructure(s, {"x": V.ValueOptional(ot)})).at("x", encoded=False))

    def test_drop_record_signal(self):
        src = V.Definitions()
        s = struct(src, "R", [("x", V.TypeOptional(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "x", T.INT32, policy="drop-record")
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        with self.assertRaises(Unrepresentable):
            rewriter.value(V.ValueStructure(s, {"x": V.ValueOptional(V.TypeOptional(T.INT32))}))

    def test_vector_to_set_collapse(self):
        src = V.Definitions()
        s = struct(src, "R", [("tags", V.TypeVector(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "tags", V.TypeSet(T.INT32), policy="collapse")
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        vec = V.ValueVector(V.TypeVector(T.INT32))
        for x in (1, 2, 2, 3, 3, 3):
            vec.append(x)
        out = rewriter.value(V.ValueStructure(s, {"tags": vec}))
        self.assertEqual(3, V.ValueSet.cast(out.at("tags", encoded=False)).size())

    def test_remove_case_map_case(self):
        src = V.Definitions()
        ed = V.TypeEnumerationDescriptor("Mode")
        ed.add_case("Old"); ed.add_case("New")
        e = src.create_enumeration(NS, ed)
        s = struct(src, "R", [("m", e)])
        d = TransformationDirectives()
        d.remove_case(e.representation(), "Old", ("map-case", "New"))
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        mv = rewriter.value(V.ValueStructure(s, {"m": V.ValueEnumeration(e, "Old")})).at("m", encoded=False)
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
            rewriter, target = DefinitionsRewriter.from_directives(src, d)
            mv = V.ValueMap(V.TypeMap(e, T.INT32))
            mv.set(V.ValueEnumeration(e, "Old"), 1)
            mv.set(V.ValueEnumeration(e, "New"), 2)
            return rewriter, target, s, mv

        rewriter, target, s, mv = setup(None)
        with self.assertRaises(ValueError):                  # fail (default) → aborts
            rewriter.value(V.ValueStructure(s, {"cfgs": mv}))

        def survivor(winner):
            rewriter, target, s, mv = setup(winner)
            om = V.ValueMap.cast(rewriter.value(V.ValueStructure(s, {"cfgs": mv})).at("cfgs", encoded=False))
            self.assertEqual(1, om.size())
            tgt_enum = target.const().enumerations()[0]
            return om.at(V.ValueEnumeration(tgt_enum, "New"), encoded=False)

        self.assertEqual({survivor("first"), survivor("last")}, {1, 2})


class TestClassBPolicyComposition(unittest.TestCase):
    """RW-F2: a retype that unwraps AND narrows/parses must honor the decreed policy for
    the leaf conversion too — the unwrapped value must not slip through value()'s
    policy-blind primitive path (which would ignore the decree and crash on overflow)."""

    def _opt_retype(self, tgt, policy, src_elem):
        src = V.Definitions()
        s = struct(src, "W", [("x", V.TypeOptional(src_elem))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "x", tgt, policy=policy)
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        return rewriter, s

    def _run(self, rewriter, s, src_elem, n):
        ot = V.TypeOptional(src_elem)
        return rewriter.value(V.ValueStructure(s, {"x": V.ValueOptional(ot, n)})).at("x", encoded=False)

    def test_unwrap_then_narrow_saturate(self):
        rewriter, s = self._opt_retype(T.INT32, "saturate", T.INT64)
        self.assertEqual(2**31 - 1, self._run(rewriter, s, T.INT64, 2**40))   # was: opaque overflow crash

    def test_unwrap_then_narrow_default(self):
        rewriter, s = self._opt_retype(T.INT32, ("default", V.ValueInt32(-1)), T.INT64)
        self.assertEqual(-1, self._run(rewriter, s, T.INT64, 2**40))

    def test_unwrap_narrow_in_range_is_exact(self):
        rewriter, s = self._opt_retype(T.INT32, "saturate", T.INT64)
        self.assertEqual(100, self._run(rewriter, s, T.INT64, 100))

    def test_unwrap_then_parse_default(self):
        rewriter, s = self._opt_retype(T.INT32, ("default", V.ValueInt32(-1)), T.STRING)
        self.assertEqual(-1, self._run(rewriter, s, T.STRING, "abc"))
        self.assertEqual(7, self._run(rewriter, s, T.STRING, "7"))

    def test_unwrap_then_widen_is_exact(self):
        # unwrap needs a nil policy; the inner widening (int32→int64) stays lossless
        rewriter, s = self._opt_retype(T.INT64, "fail", T.INT32)
        self.assertEqual(5, self._run(rewriter, s, T.INT32, 5))


class TestFloatToInt(unittest.TestCase):
    """RW-F3: float→int is Class B — truncate toward zero, then the policy governs the
    offenders (finite out-of-range + non-finite NaN/±inf). `saturate` clamps by Viper's
    total order (NaN and -inf → low end; +inf → high end)."""

    def _mk(self, policy):
        src = V.Definitions()
        s = struct(src, "M", [("d", T.DOUBLE)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "d", T.INT32, policy=policy)
        r, _ = DefinitionsRewriter.from_directives(src, d)
        return r, s

    def _go(self, r, s, v):
        return r.value(V.ValueStructure(s, {"d": v})).at("d", encoded=False)

    def test_truncates_toward_zero(self):
        r, s = self._mk("fail")
        self.assertEqual(3, self._go(r, s, 3.7))
        self.assertEqual(-3, self._go(r, s, -3.7))       # in range after truncation: policy never fires

    def test_saturate_clamps_by_total_order(self):
        r, s = self._mk("saturate")
        self.assertEqual(2**31 - 1, self._go(r, s, 1e300))       # finite out of range → hi
        self.assertEqual(2**31 - 1, self._go(r, s, math.inf))
        self.assertEqual(-2**31, self._go(r, s, -math.inf))
        self.assertEqual(-2**31, self._go(r, s, math.nan))       # NaN is the order's low end → lo

    def test_nonfinite_fail_raises(self):
        r, s = self._mk("fail")
        with self.assertRaises(ValueError):
            self._go(r, s, math.nan)                              # loud, not a silent int-max

    def test_nonfinite_default(self):
        r, s = self._mk(("default", V.ValueInt32(-1)))
        self.assertEqual(-1, self._go(r, s, math.nan))

    def test_nonfinite_drop_record(self):
        r, s = self._mk("drop-record")
        with self.assertRaises(Unrepresentable):
            self._go(r, s, math.nan)


class TestGuards(unittest.TestCase):
    def test_shape_guard_refuses_added_field_via_external_target(self):
        src = V.Definitions()
        tgt = V.Definitions()
        struct(src, "Rec", [("a", T.INT32)])
        struct(tgt, "Rec", [("a", T.INT32), ("b", T.STRING)])
        with self.assertRaises(ValueError):
            DefinitionsRewriter(src, tgt, TransformationDirectives())


class TestContainers(unittest.TestCase):
    def test_nested_vector_optional_struct(self):
        src = V.Definitions()
        s_it = struct(src, "Item", [("qty", T.INT32)])
        d = TransformationDirectives()
        d.rename_field(s_it.representation(), "qty", "count")
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        vec_t = V.TypeVector(V.TypeOptional(s_it))
        vec = V.ValueVector(vec_t)
        vec.append(V.ValueOptional(V.TypeOptional(s_it), V.ValueStructure(s_it, {"qty": 3})))
        vec.append(V.ValueOptional(V.TypeOptional(s_it)))
        r = V.ValueVector.cast(rewriter.value(vec))
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
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        tup = V.ValueTuple(V.TypeTuple([T.INT32, s_el]), [5, V.ValueStructure(s_el, {"x": 7})])
        var = V.ValueVariant(V.TypeVariant([T.INT32, s_el]), V.ValueStructure(s_el, {"x": 9}))
        r = rewriter.value(V.ValueStructure(s_rt, {"t": tup, "var": var}))
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
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        doc = V.ValueStructure(s, {"p": V.ValueVec(V.TypeVec(T.FLOAT, 3), [1.0, 2.0, 3.0]),
                                   "m": V.ValueMat(V.TypeMat(T.DOUBLE, 2, 2), [[1.0, 2.0], [3.0, 4.0]]),
                                   "label": "x"})
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(doc), s))
        self.assertEqual((1.0, 2.0, 3.0), V.Value.dumps(back.at("p", encoded=False)))
        self.assertEqual(((1.0, 2.0), (3.0, 4.0)), V.Value.dumps(back.at("m", encoded=False)))

    def test_xarray_with_tombstone(self):
        src = V.Definitions()
        s_el = struct(src, "Node", [("v", T.INT32)])
        s = struct(src, "R", [("nodes", V.TypeXArray(s_el))])
        d = TransformationDirectives()
        d.rename_field(s_el.representation(), "v", "val")
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        xa = V.ValueXArray(V.TypeXArray(s_el))
        xa.insert(V.ValueXArray.END, V.ValueStructure(s_el, {"v": 10}), V.ValueXArray.create_position())
        p1 = xa.insert(V.ValueXArray.END, V.ValueStructure(s_el, {"v": 20}), V.ValueXArray.create_position())
        xa.insert(V.ValueXArray.END, V.ValueStructure(s_el, {"v": 30}), V.ValueXArray.create_position())
        xa.disable_position(p1)
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"nodes": xa})), s))
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
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        tatt = target.const().attachments()[0]
        self.assertEqual("OrderLog", tatt.identifier().split(".")[-1])
        self.assertEqual(tatt.runtime_id().representation(),
                         rewriter.attachment(att).runtime_id().representation())
        r = rewriter.value(V.ValueStructure(s_doc, {"qty": 5}))
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
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        return concept, club, rewriter

    @staticmethod
    def _flavor(k):
        tk = k.type_key()
        return ("concept" if tk.is_concept() else "club" if tk.is_club()
                else "any-concept" if tk.is_any_concept() else "?")

    def test_concept_key_preserved_and_remapped(self):
        concept, _club, rewriter = self._setup()
        out = rewriter.value(V.ValueKey.create(concept, self.INST))
        self.assertEqual("concept", self._flavor(out))
        self.assertEqual("Demo::StandardMaterial", out.type_concept().representation())

    def test_any_concept_key_flavor_preserved(self):
        concept, _club, rewriter = self._setup()
        out = rewriter.value(V.ValueKey.create(concept, self.INST).to_any_concept_key())
        self.assertEqual("any-concept", self._flavor(out))
        self.assertEqual("Demo::StandardMaterial", out.type_concept().representation())

    def test_club_key_flavor_preserved(self):
        concept, club, rewriter = self._setup()
        out = rewriter.value(V.ValueKey.create(concept, self.INST).to_club_key(club))
        self.assertEqual("club", self._flavor(out))

    def test_instance_id_is_stable(self):
        concept, _club, rewriter = self._setup()
        out = rewriter.value(V.ValueKey.create(concept, self.INST))
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
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        out = rewriter.value(V.ValueAny(V.ValueStructure(s, {"keep": 7, "legacy": "gone"})))
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
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        st = V.ValueSet(V.TypeSet(e))
        st.add(V.ValueEnumeration(e, "Old"))
        st.add(V.ValueEnumeration(e, "New"))
        return rewriter, s, st

    def test_set_collapse_refused_without_policy(self):
        rewriter, s, st = self._set_merge(None)
        with self.assertRaises(ValueError):
            rewriter.value(V.ValueStructure(s, {"modes": st}))

    def test_set_collapse_authorized_with_policy(self):
        rewriter, s, st = self._set_merge("last")
        out = V.ValueSet.cast(rewriter.value(V.ValueStructure(s, {"modes": st})).at("modes", encoded=False))
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
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)     # no collision policy
        st = V.ValueSet(V.TypeSet(e))
        st.add(V.ValueEnumeration(e, "A")); st.add(V.ValueEnumeration(e, "B"))
        out = V.ValueSet.cast(rewriter.value(V.ValueStructure(s, {"modes": st})).at("modes", encoded=False))
        self.assertEqual(2, out.size())

    # VL7 — Vec/Mat ELEMENT conversion at fixed dimensions: widening is Class A (automatic),
    #        narrowing is Class B (policied), applied over the contiguous block. A DIMENSION
    #        change or a Vec<->Mat conversion is still refused (needs a resize/transpose directive).
    def test_vec_element_widen_lossless(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.INT32, 3))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVec(T.INT64, 3))          # widen: no policy
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        doc = V.ValueStructure(s, {"p": V.ValueVec(V.TypeVec(T.INT32, 3), [1, 2, 3])})
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(doc), s))
        self.assertEqual((1, 2, 3), V.Value.dumps(back.at("p", encoded=False)))
        self.assertEqual("vec<int64, 3>", back.at("p", encoded=False).type().representation())

    def test_mat_element_widen_lossless(self):
        src = V.Definitions()
        s = struct(src, "S", [("m", V.TypeMat(T.FLOAT, 2, 2))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "m", V.TypeMat(T.DOUBLE, 2, 2))
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        doc = V.ValueStructure(s, {"m": V.ValueMat(V.TypeMat(T.FLOAT, 2, 2), [[1.0, 2.0], [3.0, 4.0]])})
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(doc), s))
        self.assertEqual(((1.0, 2.0), (3.0, 4.0)), V.Value.dumps(back.at("m", encoded=False)))

    def test_vec_element_narrow_saturate_per_element(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.INT64, 3))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVec(T.INT32, 3), policy="saturate")
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        doc = V.ValueStructure(s, {"p": V.ValueVec(V.TypeVec(T.INT64, 3), [2**40, 5, -2**40])})
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(doc), s))
        self.assertEqual((2**31 - 1, 5, -2**31), V.Value.dumps(back.at("p", encoded=False)))  # offenders clamped

    def test_vec_element_narrow_needs_policy(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.INT64, 3))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVec(T.INT32, 3))          # narrow, NO policy
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("policy", str(cm.exception))

    def test_vec_dimension_change_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.INT32, 3))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVec(T.INT32, 4), policy="saturate")
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("dimension change", str(cm.exception))

    def test_vec_to_mat_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.INT32, 4))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeMat(T.INT32, 2, 2), policy="saturate")
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("not supported", str(cm.exception))

    # The Vector bridge — flatten (Vec/Mat → Vector) is Class A (relaxation, lossless);
    # un-flatten (Vector → Vec) is Class B (the runtime length must fit the fixed size);
    # Vector → Mat is refused (length + column/row-major un-flatten both ambiguous).
    def test_vec_to_vector_flatten(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.INT32, 4))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVector(T.INT32))          # no policy — Class A
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        doc = V.ValueStructure(s, {"p": V.ValueVec(V.TypeVec(T.INT32, 4), [10, 20, 30, 40])})
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(doc), s))
        vec = V.ValueVector.cast(back.at("p", encoded=False))
        self.assertEqual([10, 20, 30, 40], [vec.at(i, encoded=False) for i in range(vec.size())])

    def test_mat_to_vector_flatten_column_major(self):
        src = V.Definitions()
        s = struct(src, "S", [("m", V.TypeMat(T.INT32, 2, 3))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "m", V.TypeVector(T.INT32))
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        mat = V.ValueMat(V.TypeMat(T.INT32, 2, 3))
        n = 0
        for c in range(2):
            for r in range(3):
                mat.set(c, r, n); n += 1
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"m": mat})), s))
        vec = V.ValueVector.cast(back.at("m", encoded=False))
        self.assertEqual([0, 1, 2, 3, 4, 5],                                    # column-major order
                         [vec.at(i, encoded=False) for i in range(vec.size())])

    def _vector_to_vec(self, policy):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVector(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVec(T.INT32, 4), policy=policy)
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        return rewriter, s

    def _vec_of(self, *xs):
        v = V.ValueVector(V.TypeVector(T.INT32))
        for x in xs:
            v.append(x)
        return v

    def test_vector_to_vec_exact_length(self):
        rewriter, s = self._vector_to_vec("fail")
        out = rewriter.value(V.ValueStructure(s, {"p": self._vec_of(1, 2, 3, 4)}))
        self.assertEqual((1, 2, 3, 4), V.Value.dumps(out.at("p", encoded=False)))

    def test_vector_to_vec_fit_pads_short_and_truncates_long(self):
        rewriter, s = self._vector_to_vec(("fit", 0))
        short = rewriter.value(V.ValueStructure(s, {"p": self._vec_of(1, 2)}))
        self.assertEqual((1, 2, 0, 0), V.Value.dumps(short.at("p", encoded=False)))
        long = rewriter.value(V.ValueStructure(s, {"p": self._vec_of(1, 2, 3, 4, 5, 6)}))
        self.assertEqual((1, 2, 3, 4), V.Value.dumps(long.at("p", encoded=False)))

    def test_vector_to_vec_fail_on_mismatch(self):
        rewriter, s = self._vector_to_vec("fail")
        with self.assertRaises(ValueError):
            rewriter.value(V.ValueStructure(s, {"p": self._vec_of(1, 2, 3)}))

    def test_vector_to_vec_drop_record_on_mismatch(self):
        rewriter, s = self._vector_to_vec("drop-record")
        with self.assertRaises(Unrepresentable):
            rewriter.value(V.ValueStructure(s, {"p": self._vec_of(1, 2, 3)}))

    def test_vector_to_vec_needs_policy(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVector(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVec(T.INT32, 4))          # no policy
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("policy", str(cm.exception))

    def test_vector_to_mat_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVector(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeMat(T.INT32, 2, 2), policy="fail")
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("un-flatten", str(cm.exception))

    def test_flatten_with_element_type_change_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.INT32, 4))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVector(T.INT64))          # bridge + T-change
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("preserves T", str(cm.exception))

    def test_vec_verbatim_still_ok(self):        # rename ≠ retype: Vec carried verbatim
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.FLOAT, 3)), ("label", T.STRING)])
        d = TransformationDirectives()
        d.rename_field(s.representation(), "label", "name")
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        doc = V.ValueStructure(s, {"p": V.ValueVec(V.TypeVec(T.FLOAT, 3), [1.0, 2.0, 3.0]), "label": "x"})
        out = rewriter.value(doc)
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
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("add_field", str(cm.exception))

    def test_primitive_default_accepted(self):
        src = V.Definitions()
        host = struct(src, "Host", [("a", T.INT32)])
        d = TransformationDirectives()
        d.add_field(host.representation(), "note", V.ValueString("hi"))
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(host, {"a": 1})), host))
        self.assertEqual("hi", back.at("note", encoded=False))

    def test_xarray_primitive_default_accepted(self):
        # xarray of a primitive is domain-free (like vector) — accepted end-to-end
        src = V.Definitions()
        host = struct(src, "Host", [("a", T.INT32)])
        d = TransformationDirectives()
        d.add_field(host.representation(), "tags", V.ValueXArray(V.TypeXArray(T.INT32)))
        DefinitionsRewriter.from_directives(src, d)              # must not raise

    def test_domain_free_predicate_recurses_xarray_and_variant(self):
        # the domain-free gate answers only "references a named type?" — it recurses
        # into xarray and variant like vector/tuple (variant defaults are separately
        # refused by the runtime, an orthogonal concern this gate does not own)
        from dsviper_database_tools.rewrite.engine import _default_domain_free
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
                DefinitionsRewriter.from_directives(src, d)
            self.assertIn("named type", str(cm.exception))


class TestFuzzProperties(unittest.TestCase):
    """Property-based coverage (RW-F4): the seeded `Fuzzer` generates random source
    documents across the whole type surface (all containers, nesting, enum, key), and the
    rewriter must uphold its invariants on every one. A single seeded pass exercises the
    container × nesting × key-flavour cross-product far beyond hand-written cases; a
    failing seed is reproducible."""

    SEED = 20260714
    N = 200

    def _rich_source(self):
        src = V.Definitions()
        concept = src.create_concept(NS, "Thing")
        ed = V.TypeEnumerationDescriptor("Mode")
        for c in ("A", "B", "C"):
            ed.add_case(c)
        mode = src.create_enumeration(NS, ed)
        inner = struct(src, "Inner", [("x", T.INT32)])
        rich = struct(src, "Rich", [
            ("n", T.INT32), ("s", T.STRING), ("opt", V.TypeOptional(T.INT32)),
            ("vec", V.TypeVector(T.INT32)), ("st", V.TypeSet(T.STRING)),
            ("mp", V.TypeMap(T.STRING, T.INT32)), ("tup", V.TypeTuple([T.INT32, T.STRING])),
            ("var", V.TypeVariant([T.INT32, T.STRING])), ("xa", V.TypeXArray(T.INT32)),
            ("mode", mode), ("ref", V.TypeKey(concept)), ("nested", inner),
            ("amt", T.DOUBLE)])
        src.create_attachment(NS, "Things", concept, rich)
        return src, rich

    def test_rename_only_is_total_over_fuzzed_docs(self):
        # family 1 is size-preserving and lossless -> the rewriter is TOTAL: every
        # well-formed source value must rewrite and round-trip in the target registry.
        src, rich = self._rich_source()
        d = TransformationDirectives()
        d.rename_field(rich.representation(), "n", "num")
        d.rename_type("Demo::Inner", "Demo::InnerV2")
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        fuzzer = V.Fuzzer(src.const(), seed=self.SEED)
        for i in range(self.N):
            doc = fuzzer.fuzz(rich)
            try:
                out = rewriter.value(doc)               # must not raise
                rt(rewriter, target, out, rich)         # well-formed in the TARGET registry
            except Exception as e:
                self.fail(f"rename-only broke on fuzz seed={self.SEED} iter={i}: "
                          f"{type(e).__name__}: {e}")

    def test_family2_is_rewrite_or_decreed_drop(self):
        # total-or-explicit-refusal over random data: every fuzzed doc either produces a
        # valid target value or raises `Unrepresentable` (a decreed drop) — never anything else.
        src, rich = self._rich_source()
        d = TransformationDirectives()
        d.retype_field(rich.representation(), "n", T.INT16, policy="saturate")        # narrow
        d.retype_field(rich.representation(), "opt", T.INT32, policy="drop-record")   # unwrap-or-drop
        d.retype_field(rich.representation(), "amt", T.INT32, policy="saturate")      # float→int (RW-F3)
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        fuzzer = V.Fuzzer(src.const(), seed=self.SEED)
        kept = drops = 0
        for i in range(self.N):
            doc = fuzzer.fuzz(rich)
            try:
                out = rewriter.value(doc)
            except Unrepresentable:
                drops += 1                              # decreed drop — acceptable
                continue
            except Exception as e:
                self.fail(f"unexpected error on fuzz seed={self.SEED} iter={i}: "
                          f"{type(e).__name__}: {e}")
            rt(rewriter, target, out, rich)             # must round-trip in target
            kept += 1
        self.assertEqual(self.N, kept + drops)          # every doc: rewritten or decreed-dropped


class TestVecMatDimensions(unittest.TestCase):
    """Vec/Mat dimension changes via explicit named directives (never inferred from the
    target type). Position-preserving: grow fills the new cells with the type's born-default
    (Vec zero / Mat identity) or a decreed value; shrink drops the trailing cells under
    on_shrink; transpose permutes [i,j]->[j,i]. Column-major throughout."""

    def _vec(self, *xs):
        v = V.ValueVec(V.TypeVec(T.INT32, len(xs)))
        for i, x in enumerate(xs):
            v.set(i, x)
        return v

    def _mat(self, cols):                              # cols = list of column-lists
        tm = V.TypeMat(T.INT32, len(cols), len(cols[0]))
        m = V.ValueMat(tm)
        for c, col in enumerate(cols):
            for r, x in enumerate(col):
                m.set(c, r, x)
        return m

    def _run(self, src_t, build, value):
        src = V.Definitions()
        s = struct(src, "S", [("f", src_t)])
        d = TransformationDirectives()
        build(d, s.representation())
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"f": value})), s))
        return V.Value.dumps(back.at("f", encoded=False))

    def test_resize_vec_grow_zero_and_scalar(self):
        self.assertEqual((1, 2, 3, 0, 0),
                         self._run(V.TypeVec(T.INT32, 3), lambda d, s: d.resize_vec_field(s, "f", 5), self._vec(1, 2, 3)))
        self.assertEqual((1, 2, 3, 9, 9),
                         self._run(V.TypeVec(T.INT32, 3), lambda d, s: d.resize_vec_field(s, "f", 5, fill=9), self._vec(1, 2, 3)))

    def test_resize_vec_shrink_fail_vs_accept(self):
        with self.assertRaises(ValueError):
            self._run(V.TypeVec(T.INT32, 5), lambda d, s: d.resize_vec_field(s, "f", 3), self._vec(1, 2, 3, 4, 5))
        self.assertEqual((1, 2, 3),
                         self._run(V.TypeVec(T.INT32, 5), lambda d, s: d.resize_vec_field(s, "f", 3, on_shrink="accept"),
                                   self._vec(1, 2, 3, 4, 5)))

    def test_resize_mat_grow_identity_extends_diagonal(self):
        self.assertEqual(((1, 2, 0), (3, 4, 0), (0, 0, 1)),            # [2][2]=1 — identity-extend
                         self._run(V.TypeMat(T.INT32, 2, 2), lambda d, s: d.resize_mat_field(s, "f", 3, 3),
                                   self._mat([[1, 2], [3, 4]])))

    def test_resize_mat_grow_zero_fill(self):
        self.assertEqual(((1, 2, 0), (3, 4, 0), (0, 0, 0)),            # new cells all 0
                         self._run(V.TypeMat(T.INT32, 2, 2), lambda d, s: d.resize_mat_field(s, "f", 3, 3, fill="zero"),
                                   self._mat([[1, 2], [3, 4]])))

    def test_resize_mat_shrink_needs_accept(self):
        with self.assertRaises(ValueError):                            # a row is dropped → fail
            self._run(V.TypeMat(T.INT32, 2, 3), lambda d, s: d.resize_mat_field(s, "f", 3, 2),
                      self._mat([[0, 1, 2], [3, 4, 5]]))

    def test_transpose_permutes_indices(self):
        self.assertEqual(((0, 3), (1, 4), (2, 5)),
                         self._run(V.TypeMat(T.INT32, 2, 3), lambda d, s: d.transpose_mat_field(s, "f"),
                                   self._mat([[0, 1, 2], [3, 4, 5]])))

    def test_resize_and_transpose_to_same_shape_differ(self):
        # Mat<2,3> -> Mat<3,2> by both, but different content — why the intent must be named
        src = self._mat([[0, 1, 2], [3, 4, 5]])
        resized = self._run(V.TypeMat(T.INT32, 2, 3),
                            lambda d, s: d.resize_mat_field(s, "f", 3, 2, on_shrink="accept"), src)
        transposed = self._run(V.TypeMat(T.INT32, 2, 3), lambda d, s: d.transpose_mat_field(s, "f"), src)
        self.assertEqual(((0, 1), (3, 4), (0, 0)), resized)            # position-preserving + fill
        self.assertEqual(((0, 3), (1, 4), (2, 5)), transposed)         # [i,j]->[j,i]
        self.assertNotEqual(resized, transposed)

    def test_resize_vec_fill_identity_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("f", V.TypeVec(T.INT32, 3))])
        d = TransformationDirectives()
        d.resize_vec_field(s.representation(), "f", 5, fill="identity")
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("identity is Mat-only", str(cm.exception))

    def test_resize_nested_vec_refused(self):
        # a Vec buried in a Vector is not addressable — the directive names a struct field
        src = V.Definitions()
        s = struct(src, "S", [("f", V.TypeVector(V.TypeVec(T.INT32, 3)))])
        d = TransformationDirectives()
        d.resize_vec_field(s.representation(), "f", 5)
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("not a direct Vec", str(cm.exception))

    def test_transpose_non_mat_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("f", V.TypeVec(T.INT32, 4))])
        d = TransformationDirectives()
        d.transpose_mat_field(s.representation(), "f")
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("not a direct Mat", str(cm.exception))


class TestVariantArmSet(unittest.TestCase):
    """Variant arm-set changes via retype_field (the target variant fully specifies the arms —
    no ambiguity, so no new vocabulary). Membership is by runtimeId: a source arm present in
    the target survives (add/reorder = Class A, re-wrapped index-safe); a source arm absent
    from the target is removed (Class B — a value on it has no image, governed by a policy)."""

    IS = V.TypeVariant([T.INT32, T.STRING])
    ISD = V.TypeVariant([T.INT32, T.STRING, T.DOUBLE])

    def _wrap(self, vt, val):
        vv = V.ValueVariant(vt)
        vv.wrap(val)
        return vv

    def _mk(self, src_v, tgt_v, policy=None):
        src = V.Definitions()
        s = struct(src, "S", [("v", src_v)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "v", tgt_v, policy=policy)
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        return rewriter, target, s

    def test_add_arm_is_lossless(self):
        rewriter, target, s = self._mk(self.IS, self.ISD)              # no policy — Class A
        doc = V.ValueStructure(s, {"v": self._wrap(self.IS, V.ValueInt32(42))})
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(doc), s))
        out = V.ValueVariant.cast(back.at("v", encoded=False))
        self.assertEqual(42, V.Value.dumps(out.unwrap(encoded=False)))
        self.assertEqual("int32|string|double", out.type().representation())

    def test_reorder_arms_preserves_value_index_safe(self):
        rewriter, target, s = self._mk(self.IS, V.TypeVariant([T.STRING, T.INT32]))
        doc = V.ValueStructure(s, {"v": self._wrap(self.IS, V.ValueString("hi"))})
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(doc), s))  # round-trips
        out = V.ValueVariant.cast(back.at("v", encoded=False))
        self.assertEqual("hi", V.Value.dumps(out.unwrap(encoded=False)))
        self.assertEqual("string|int32", out.type().representation())

    def test_remove_arm_needs_policy(self):
        with self.assertRaises(ValueError) as cm:
            self._mk(self.ISD, self.IS)                                # removal, no policy
        self.assertIn("arm removal", str(cm.exception))

    def test_remove_arm_drop_record_skips_offender_keeps_survivors(self):
        rewriter, target, s = self._mk(self.ISD, self.IS, policy="drop-record")
        with self.assertRaises(Unrepresentable):                       # value on removed double arm
            rewriter.value(V.ValueStructure(s, {"v": self._wrap(self.ISD, V.ValueDouble(1.5))}))
        surv = rewriter.value(V.ValueStructure(s, {"v": self._wrap(self.ISD, V.ValueInt32(7))}))
        self.assertEqual(7, V.Value.dumps(V.ValueVariant.cast(surv.at("v", encoded=False)).unwrap(encoded=False)))

    def test_remove_arm_default_replaces_offender(self):
        default = self._wrap(self.IS, V.ValueString("n/a"))
        rewriter, target, s = self._mk(self.ISD, self.IS, policy=("default", default))
        out = rewriter.value(V.ValueStructure(s, {"v": self._wrap(self.ISD, V.ValueDouble(1.5))}))
        got = V.ValueVariant.cast(out.at("v", encoded=False))
        self.assertEqual("n/a", V.Value.dumps(got.unwrap(encoded=False)))

    def test_variant_to_non_variant_refused(self):
        with self.assertRaises(ValueError) as cm:
            self._mk(self.IS, T.INT32, policy="fail")
        self.assertIn("variant", str(cm.exception))


class TestTransformFieldHook(unittest.TestCase):
    """The Class-C escape hatch, STRUCT-scoped: a field hook is `fn(source_struct, field_name,
    target_type) -> value` — it sees the whole source struct (siblings) and its own field name,
    so it handles a field retype AND a cross-field correction. Total-or-explicit-refusal
    survives user code: the engine validates the output against the target — valid → used,
    `Unrepresentable` → drop, wrong-typed / non-Value → refuse."""

    def test_hook_primitive_retype(self):
        src = V.Definitions()
        s = struct(src, "S", [("n", T.INT32)])
        d = TransformationDirectives()
        d.transform_field(s.representation(), "n", T.STRING,
                          lambda st, f, tt: V.ValueString("#" + str(V.Value.dumps(st.at(f, encoded=False)))))
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(s, {"n": 42})), s))
        self.assertEqual("#42", back.at("n", encoded=False))

    def test_hook_cross_field_correction(self):
        # correct an EXISTING field from a SIBLING — the whole point of struct scope
        src = V.Definitions()
        s = struct(src, "Money", [("amount", T.INT32), ("scale", T.INT32)])
        d = TransformationDirectives()
        d.transform_field(s.representation(), "amount", T.INT32,
                          lambda st, f, tt: V.ValueInt32(V.Value.dumps(st.at(f, encoded=False)) * V.Value.dumps(st.at("scale", encoded=False))))
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        out = rewriter.value(V.ValueStructure(s, {"amount": 3, "scale": 100}))
        self.assertEqual(300, V.Value.dumps(out.at("amount", encoded=False)))

    def test_hook_reusable_via_field_name(self):
        # one fn registered for two fields, using field_name
        src = V.Definitions()
        s = struct(src, "S", [("a", T.INT32), ("b", T.INT32)])
        tag = lambda st, f, tt: V.ValueString(f + "=" + str(V.Value.dumps(st.at(f, encoded=False))))
        d = TransformationDirectives()
        d.transform_field(s.representation(), "a", T.STRING, tag)
        d.transform_field(s.representation(), "b", T.STRING, tag)
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        out = rewriter.value(V.ValueStructure(s, {"a": 1, "b": 2}))
        self.assertEqual(("a=1", "b=2"), (out.at("a", encoded=False), out.at("b", encoded=False)))

    def test_hook_to_unrelated_named_type(self):        # SG7
        src = V.Definitions()
        foo = struct(src, "Foo", [("a", T.INT32)])
        bar = struct(src, "Bar", [("label", T.STRING)])
        host = struct(src, "Host", [("meta", foo)])
        def foo_to_bar(st, f, bar_t):
            a = V.ValueStructure.cast(st.at(f, encoded=False)).at("a", encoded=False)
            return V.ValueStructure(V.TypeStructure.cast(bar_t), {"label": f"a={a}"})
        d = TransformationDirectives()
        d.transform_field(host.representation(), "meta", bar, foo_to_bar)
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        doc = V.ValueStructure(host, {"meta": V.ValueStructure(foo, {"a": 7})})
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(doc), host))
        out = V.ValueStructure.cast(back.at("meta", encoded=False))
        self.assertEqual("Demo::Bar", out.type().representation())
        self.assertEqual("a=7", out.at("label", encoded=False))

    def test_hook_wrong_type_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("n", T.INT32)])
        d = TransformationDirectives()
        d.transform_field(s.representation(), "n", T.STRING, lambda st, f, tt: V.ValueInt32(1))  # not a string
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        with self.assertRaises(ValueError) as cm:
            rewriter.value(V.ValueStructure(s, {"n": 42}))
        self.assertIn("hook", str(cm.exception))

    def test_hook_non_value_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("n", T.INT32)])
        d = TransformationDirectives()
        d.transform_field(s.representation(), "n", T.STRING, lambda st, f, tt: "raw python str")
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        with self.assertRaises(ValueError) as cm:
            rewriter.value(V.ValueStructure(s, {"n": 42}))
        self.assertIn("must return a Value", str(cm.exception))

    def test_hook_may_drop_record(self):
        src = V.Definitions()
        s = struct(src, "S", [("n", T.INT32)])
        def dropper(st, f, tt):
            raise Unrepresentable("author decided to drop")
        d = TransformationDirectives()
        d.transform_field(s.representation(), "n", T.STRING, dropper)
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        with self.assertRaises(Unrepresentable):
            rewriter.value(V.ValueStructure(s, {"n": 42}))


class TestDeriveField(unittest.TestCase):
    """add_field(name, type, derive=fn) — a NEW field computed from the source struct (its
    siblings). With drop_field, this expresses SG4 merge/split. Same struct-scoped contract."""

    def test_merge_two_fields_into_one(self):
        src = V.Definitions()
        p = struct(src, "P", [("first", T.STRING), ("last", T.STRING)])
        d = TransformationDirectives()
        d.drop_field(p.representation(), "first")
        d.drop_field(p.representation(), "last")
        d.add_field(p.representation(), "full", T.STRING,
                    derive=lambda st, f, tt: V.ValueString(V.Value.dumps(st.at("first", encoded=False)) + " " + V.Value.dumps(st.at("last", encoded=False))))
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(p, {"first": "Ada", "last": "L"})), p))
        self.assertEqual("Ada L", back.at("full", encoded=False))
        self.assertEqual(["full"], [f.name() for f in back.type_structure().fields()])   # first/last gone

    def test_static_default_still_works(self):
        src = V.Definitions()
        host = struct(src, "Host", [("a", T.INT32)])
        d = TransformationDirectives()
        d.add_field(host.representation(), "note", V.ValueString("hi"))          # unchanged 3-arg form
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(host, {"a": 1})), host))
        self.assertEqual("hi", back.at("note", encoded=False))

    def test_derived_field_wrong_type_refused(self):
        src = V.Definitions()
        p = struct(src, "P", [("x", T.INT32)])
        d = TransformationDirectives()
        d.add_field(p.representation(), "y", T.STRING, derive=lambda st, f, tt: V.ValueInt32(1))  # not a string
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        with self.assertRaises(ValueError):
            rewriter.value(V.ValueStructure(p, {"x": 1}))


class TestTransformTypeHook(unittest.TestCase):
    """The GLOBAL hook: transform_type rewrites EVERY occurrence of a type in one directive
    (no per-field duplication), riding the target-directed recursion so it reaches nested
    occurrences for free. A field-level transform_field on the same position overrides it
    (resolution: field > type)."""

    def _foo_defs(self):
        src = V.Definitions()
        foo = struct(src, "Foo", [("a", T.INT32)])
        return src, foo

    def _foo_to_str(self, v, tt):
        return V.ValueString("a=" + str(V.ValueStructure.cast(v).at("a", encoded=False)))

    def test_one_directive_transforms_all_fields_of_the_type(self):
        src, foo = self._foo_defs()
        host = struct(src, "Host", [("x", foo), ("y", foo)])
        d = TransformationDirectives()
        d.transform_type(foo, T.STRING, self._foo_to_str)
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        doc = V.ValueStructure(host, {"x": V.ValueStructure(foo, {"a": 1}),
                                      "y": V.ValueStructure(foo, {"a": 2})})
        out = rewriter.value(doc)
        self.assertEqual("a=1", out.at("x", encoded=False))
        self.assertEqual("a=2", out.at("y", encoded=False))
        self.assertNotIn("Demo::Foo", [s.representation() for s in target.const().structures()])  # replaced

    def test_reaches_nested_occurrence(self):
        src, foo = self._foo_defs()
        host = struct(src, "H", [("items", V.TypeVector(foo))])
        d = TransformationDirectives()
        d.transform_type(foo, T.STRING, self._foo_to_str)
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        vec = V.ValueVector(V.TypeVector(foo))
        for i in (10, 20):
            vec.append(V.ValueStructure(foo, {"a": i}))
        back = V.ValueStructure.cast(rt(rewriter, target, rewriter.value(V.ValueStructure(host, {"items": vec})), host))
        out = V.ValueVector.cast(back.at("items", encoded=False))
        self.assertEqual(["a=10", "a=20"], [out.at(i, encoded=False) for i in range(out.size())])
        self.assertEqual("vector<string>", out.type().representation())

    def test_field_overrides_type_resolution(self):
        src, foo = self._foo_defs()
        host = struct(src, "Host", [("x", foo), ("y", foo)])
        d = TransformationDirectives()
        d.transform_type(foo, T.STRING, self._foo_to_str)                        # global: Foo → string
        d.transform_field(host.representation(), "x", T.INT32, lambda st, f, tt: V.ValueInt32(999))
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        out = rewriter.value(V.ValueStructure(host, {"x": V.ValueStructure(foo, {"a": 1}),
                                                     "y": V.ValueStructure(foo, {"a": 2})}))
        self.assertEqual(999, V.Value.dumps(out.at("x", encoded=False)))          # field hook wins
        self.assertEqual("a=2", out.at("y", encoded=False))                       # type hook fallback

    def test_transform_type_to_named_target(self):        # SG7, globally
        src, foo = self._foo_defs()
        bar = struct(src, "Bar", [("label", T.STRING)])
        host = struct(src, "Host", [("m", foo)])
        def foo_to_bar(v, bar_t):
            a = V.ValueStructure.cast(v).at("a", encoded=False)
            return V.ValueStructure(V.TypeStructure.cast(bar_t), {"label": f"a={a}"})
        d = TransformationDirectives()
        d.transform_type(foo, bar, foo_to_bar)
        rewriter, target = DefinitionsRewriter.from_directives(src, d)
        out = V.ValueStructure.cast(rewriter.value(V.ValueStructure(host, {"m": V.ValueStructure(foo, {"a": 7})})).at("m", encoded=False))
        self.assertEqual("Demo::Bar", out.type().representation())
        self.assertEqual("a=7", out.at("label", encoded=False))


def enum(defs, name, cases):
    d = V.TypeEnumerationDescriptor(name)
    for c in cases:
        d.add_case(c)
    return defs.create_enumeration(NS, d)


class TestByConstructionProof(unittest.TestCase):
    """Measure the by-construction guarantees rather than assert them: P1 name-completeness,
    P2 shape-invariance (the external-target path), the build-time permutation checks, and the
    commit_id remap. (The unhandled-composite net and the type/concept cycle raises are dead-
    defensive: every composite type_code is dispatched, and Viper forbids type cycles at
    construction — so they cannot be reached from a valid source.)"""

    def test_p1_missing_target_refused(self):
        src = V.Definitions()
        struct(src, "A", [("x", T.INT32)])
        struct(src, "B", [("y", T.INT32)])
        tgt = V.Definitions()
        struct(tgt, "A", [("x", T.INT32)])                       # no target for B
        with self.assertRaises(KeyError) as cm:
            DefinitionsRewriter(src, tgt, TransformationDirectives())
        self.assertIn("P1", str(cm.exception))

    def test_p2_field_count_differs_refused(self):
        src = V.Definitions()
        struct(src, "A", [("x", T.INT32), ("y", T.INT32)])
        tgt = V.Definitions()
        struct(tgt, "A", [("x", T.INT32)])                       # shape change, not a rename
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter(src, tgt, TransformationDirectives())
        self.assertIn("P2", str(cm.exception))

    def test_p2_field_order_differs_refused(self):
        src = V.Definitions()
        struct(src, "A", [("x", T.INT32), ("y", T.INT32)])
        tgt = V.Definitions()
        struct(tgt, "A", [("y", T.INT32), ("x", T.INT32)])       # reordered
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter(src, tgt, TransformationDirectives())
        self.assertIn("P2", str(cm.exception))

    def test_p2_case_count_differs_refused(self):
        src = V.Definitions()
        enum(src, "E", ["A", "B"])
        tgt = V.Definitions()
        enum(tgt, "E", ["A"])                                    # a case vanished
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter(src, tgt, TransformationDirectives())
        self.assertIn("P2", str(cm.exception))

    def test_reorder_fields_non_permutation_refused(self):
        src = V.Definitions()
        s = struct(src, "S", [("a", T.INT32), ("b", T.INT32)])
        d = TransformationDirectives()
        d.reorder_fields(s.representation(), ["a", "c"])         # "c" is not a field
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("permutation", str(cm.exception))

    def test_reorder_cases_non_permutation_refused(self):
        src = V.Definitions()
        e = enum(src, "E", ["A", "B"])
        d = TransformationDirectives()
        d.reorder_cases(e.representation(), ["A", "Z"])          # "Z" is not a case
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(src, d)
        self.assertIn("permutation", str(cm.exception))

    def test_commit_id_remap_hits_and_misses(self):
        rewriter, _ = DefinitionsRewriter.from_directives(V.Definitions(), TransformationDirectives())
        internal, reissued = V.ValueCommitId("a" * 40), V.ValueCommitId("b" * 40)
        external = V.ValueCommitId("c" * 40)
        rewriter._commit_id_remap = {internal.representation(): reissued}
        self.assertEqual(reissued.representation(), rewriter.value(internal).representation())  # remapped
        self.assertEqual(external.representation(), rewriter.value(external).representation())   # kept verbatim


class TestDocumentationPreserved(unittest.TestCase):
    """Documentation is metadata *outside* the runtimeId (proven: two structs differing only
    in field doc share an id), so carrying it is lossless (Class A) and always safe. The build
    must carry it on every definition family — a migration that silently dropped docstrings
    (even for untouched fields under a plain rename) would violate no-silent-loss at the
    metadata level."""

    def _defs(self):
        defs = V.Definitions()
        cust = defs.create_concept(NS, "Customer", documentation="a customer")
        ed = V.TypeEnumerationDescriptor("Mode", documentation="the mode")
        ed.add_case("A", "case A doc")
        ed.add_case("B", "case B doc")
        en = defs.create_enumeration(NS, ed)
        sd = V.TypeStructureDescriptor("Order", documentation="an order")
        sd.add_field("qty", T.INT32, "the quantity")
        sd.add_field("label", T.STRING, "the label")
        sd.add_field("mode", en, "the mode field")
        od = defs.create_structure(NS, sd)
        att = defs.create_attachment(NS, "Orders", cust, od, documentation="the orders")
        return defs, en, od

    def test_rename_preserves_documentation_everywhere(self):
        defs, en, od = self._defs()
        d = TransformationDirectives()
        d.rename_field(od.representation(), "qty", "count")     # touch one field
        d.rename_case(en.representation(), "A", "Alpha")        # touch one case
        _, tgt = DefinitionsRewriter.from_directives(defs.const(), d)
        tc = tgt.const()

        st = next(V.TypeStructure.cast(s) for s in tc.structures()
                  if s.representation().endswith("Order"))
        em = next(V.TypeEnumeration.cast(e) for e in tc.enumerations()
                  if e.representation().endswith("Mode"))
        cc = next(c for c in tc.concepts() if c.representation().endswith("Customer"))
        at = tc.attachments()[0]

        self.assertEqual("an order", st.documentation())
        # renamed field keeps its doc; untouched fields keep theirs
        self.assertEqual({"count": "the quantity", "label": "the label",
                          "mode": "the mode field"},
                         {f.name(): f.documentation() for f in st.fields()})
        self.assertEqual("the mode", em.documentation())
        self.assertEqual({"Alpha": "case A doc", "B": "case B doc"},
                         {c.name(): c.documentation() for c in em.cases()})
        self.assertEqual("a customer", cc.documentation())
        self.assertEqual("the orders", at.documentation())

    def test_documentation_is_outside_runtime_id(self):
        # The property the carry relies on: doc changes do not re-id (hence never re-key data).
        a = V.Definitions()
        sa = V.TypeStructureDescriptor("Order"); sa.add_field("qty", T.INT32, "doc A")
        oa = a.create_structure(NS, sa)
        b = V.Definitions()
        sb = V.TypeStructureDescriptor("Order"); sb.add_field("qty", T.INT32, "totally different")
        ob = b.create_structure(NS, sb)
        self.assertEqual(oa.runtime_id().representation(), ob.runtime_id().representation())


class TestDocumentationAuthoring(unittest.TestCase):
    """The `document_*` directives set/override the carried doc (Class A, no policy). Members
    are named by SOURCE name (as renames are); `text=""` clears; an added field can be
    documented; `document_type` is polymorphic over struct/enum/concept."""

    def _defs(self):
        defs = V.Definitions()
        cust = defs.create_concept(NS, "Customer", documentation="old customer")
        ed = V.TypeEnumerationDescriptor("Mode", documentation="old mode")
        ed.add_case("A", "old A"); ed.add_case("B", "keep B")
        en = defs.create_enumeration(NS, ed)
        sd = V.TypeStructureDescriptor("Order", documentation="old order")
        sd.add_field("qty", T.INT32, "old qty"); sd.add_field("label", T.STRING, "keep label")
        od = defs.create_structure(NS, sd)
        defs.create_attachment(NS, "Orders", cust, od, documentation="old orders")
        return defs, en, od, cust

    def test_override_add_and_clear(self):
        defs, en, od, cust = self._defs()
        d = TransformationDirectives()
        d.rename_field(od.representation(), "qty", "count")
        d.document_type(od.representation(), "A customer order.")       # struct
        d.document_field(od.representation(), "qty", "Units ordered.")  # by SOURCE name
        d.document_field(od.representation(), "label", "")              # clear
        d.add_field(od.representation(), "note", V.ValueString("n/a"))
        d.document_field(od.representation(), "note", "Free-text note.")   # document an added field
        d.document_type(cust.representation(), "A registered customer.")   # concept (polymorphic)
        d.document_type(en.representation(), "Order lifecycle mode.")      # enum (polymorphic)
        d.document_case(en.representation(), "A", "The A case.")           # by SOURCE name
        d.document_attachment("Orders", "Orders keyed by customer.")

        _, tgt = DefinitionsRewriter.from_directives(defs.const(), d)
        tc = tgt.const()
        st = next(V.TypeStructure.cast(s) for s in tc.structures()
                  if s.representation().endswith("Order"))
        em = next(V.TypeEnumeration.cast(e) for e in tc.enumerations()
                  if e.representation().endswith("Mode"))
        cc = next(c for c in tc.concepts() if c.representation().endswith("Customer"))

        self.assertEqual("A customer order.", st.documentation())
        self.assertEqual({"count": "Units ordered.", "label": "", "note": "Free-text note."},
                         {f.name(): f.documentation() for f in st.fields()})
        self.assertEqual("Order lifecycle mode.", em.documentation())
        self.assertEqual({"A": "The A case.", "B": "keep B"},
                         {c.name(): c.documentation() for c in em.cases()})
        self.assertEqual("A registered customer.", cc.documentation())
        self.assertEqual("Orders keyed by customer.", tc.attachments()[0].documentation())


class TestDropDefinition(unittest.TestCase):
    """Definition-level drops — the co-direction of the additive build. drop_type omits a
    type (struct/enum/concept/club); if a SURVIVING definition still references it the build
    refuses up front with ONE accumulated report (every dangling site at once), reusing the
    build's own type-walk (not name resolution). drop_attachment omits an attachment (nothing
    references an attachment, so it dangles nothing)."""

    def _defs(self):
        defs = V.Definitions()
        cust = defs.create_concept(NS, "Customer")
        li = V.TypeStructureDescriptor("LineItem"); li.add_field("sku", T.STRING)
        lineitem = defs.create_structure(NS, li)
        iv = V.TypeStructureDescriptor("Invoice"); iv.add_field("line", lineitem)
        inv = defs.create_structure(NS, iv)
        od = V.TypeStructureDescriptor("Order")
        od.add_field("items", V.TypeVector(lineitem)); od.add_field("best", lineitem)
        order = defs.create_structure(NS, od)
        defs.create_attachment(NS, "Orders", cust, order)
        return defs, lineitem, inv, order

    def _names(self, defs):
        return {s.representation().split("::")[-1] for s in defs.const().structures()}

    def test_drop_unreferenced_type_is_omitted(self):
        defs, lineitem, inv, order = self._defs()
        free = V.TypeStructureDescriptor("Scratch"); free.add_field("n", T.INT32)
        scratch = defs.create_structure(NS, free)
        d = TransformationDirectives(); d.drop_type(scratch.representation())
        _, tgt = DefinitionsRewriter.from_directives(defs.const(), d)
        self.assertNotIn("Scratch", self._names(tgt))

    def test_referenced_drop_reports_every_site(self):
        defs, lineitem, inv, order = self._defs()
        d = TransformationDirectives(); d.drop_type(lineitem.representation())
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(defs.const(), d)
        msg = str(cm.exception)
        self.assertIn("dropped-type-referenced", msg)
        self.assertIn("3 dangling", msg)                 # Invoice.line, Order.items, Order.best
        self.assertIn("Demo::Invoice", msg)
        self.assertIn("Demo::Order", msg)

    def test_handled_referrers_let_the_drop_through(self):
        defs, lineitem, inv, order = self._defs()
        d = TransformationDirectives()
        d.drop_type(lineitem.representation())
        d.drop_type(inv.representation())                # drop the other referrer entirely
        d.drop_field(order.representation(), "items")    # drop one referring field
        d.retype_field(order.representation(), "best", T.STRING)   # retype the other
        _, tgt = DefinitionsRewriter.from_directives(defs.const(), d)
        self.assertEqual({"Order"}, self._names(tgt))    # only Order survives, LineItem/Invoice gone

    def test_drop_type_polymorphic_enum(self):
        defs = V.Definitions()
        ed = V.TypeEnumerationDescriptor("Mode"); ed.add_case("A"); ed.add_case("B")
        en = defs.create_enumeration(NS, ed)
        sd = V.TypeStructureDescriptor("Keep"); sd.add_field("x", T.INT32)
        defs.create_structure(NS, sd)
        d = TransformationDirectives(); d.drop_type(en.representation())
        _, tgt = DefinitionsRewriter.from_directives(defs.const(), d)
        self.assertEqual([], list(tgt.const().enumerations()))

    def test_drop_attachment_omits_it(self):
        defs, lineitem, inv, order = self._defs()
        d = TransformationDirectives(); d.drop_attachment("Orders")
        _, tgt = DefinitionsRewriter.from_directives(defs.const(), d)
        self.assertEqual([], list(tgt.const().attachments()))


class TestNamespaceMove(unittest.TestCase):
    """Per-definition namespace assignment — the n:m namespace algebra. `move_type` reassigns
    a single definition's namespace (a lossless Class-A re-id, since the namespace uuid is in
    the runtimeId); references follow via the type map. It expresses **split** (types of one
    namespace to different targets) and precise **merge** (into a shared namespace). A move that
    lands two definitions in one target slot is refused up front with an accumulated report."""

    SHOP = V.NameSpace(V.ValueUUId("11111111-1111-1111-1111-111111111111"), "Shop")
    ORDERS = V.NameSpace(V.ValueUUId("44444444-4444-4444-4444-444444444444"), "Orders")
    PRODUCTS = V.NameSpace(V.ValueUUId("55555555-5555-5555-5555-555555555555"), "Products")

    def test_split_one_namespace_into_two(self):
        d = V.Definitions()
        s1 = V.TypeStructureDescriptor("Order"); s1.add_field("q", T.INT32)
        o = d.create_structure(self.SHOP, s1)
        s2 = V.TypeStructureDescriptor("Product"); s2.add_field("sku", T.STRING)
        p = d.create_structure(self.SHOP, s2)
        dr = TransformationDirectives()
        dr.move_type(o.representation(), self.ORDERS)
        dr.move_type(p.representation(), self.PRODUCTS)
        _, tgt = DefinitionsRewriter.from_directives(d.const(), dr)
        self.assertEqual(["Orders::Order", "Products::Product"],
                         sorted(s.representation() for s in tgt.const().structures()))

    def test_reference_follows_the_move(self):
        d = V.Definitions()
        s1 = V.TypeStructureDescriptor("Order"); s1.add_field("q", T.INT32)
        o = d.create_structure(self.SHOP, s1)
        iv = V.TypeStructureDescriptor("Invoice"); iv.add_field("line", o)
        d.create_structure(self.SHOP, iv)
        dr = TransformationDirectives(); dr.move_type(o.representation(), self.ORDERS)
        _, tgt = DefinitionsRewriter.from_directives(d.const(), dr)
        inv = next(V.TypeStructure.cast(s) for s in tgt.const().structures()
                   if s.representation().endswith("Invoice"))
        self.assertEqual("Orders::Order", inv.check("line").type().representation())

    def test_collision_is_reported(self):
        d = V.Definitions()
        a = V.TypeStructureDescriptor("Order"); a.add_field("q", T.INT32)
        oa = d.create_structure(self.SHOP, a)
        b = V.TypeStructureDescriptor("Receipt"); b.add_field("n", T.STRING)
        ob = d.create_structure(self.SHOP, b)
        dr = TransformationDirectives()
        dr.move_type(oa.representation(), self.ORDERS)
        dr.move_type(ob.representation(), self.ORDERS)
        dr.rename_type(ob.representation(), "Shop::Order")     # both -> Orders::Order
        with self.assertRaises(ValueError) as cm:
            DefinitionsRewriter.from_directives(d.const(), dr)
        msg = str(cm.exception)
        self.assertIn("namespace-collision", msg)
        self.assertIn("Orders::Order", msg)
        self.assertIn("Shop::Order", msg)
        self.assertIn("Shop::Receipt", msg)


class TestContainerElementRetype(unittest.TestCase):
    """`Set/Vector/XArray/Map<A>` → same container `<B>` with a changed ELEMENT type: widen (Class A,
    automatic) / narrow (Class B, policied) per element, reusing the scalar leaf path — the retype
    twin of the container `value()` branches. Previously this crashed (the whole container reached
    the scalar narrowing path); now it is handled uniformly, incl. nested containers and the
    set-collapse / map-collision guards."""

    def _mk(self, src_v, tgt_v, policy=None, collisions=None):
        src = V.Definitions()
        s = struct(src, "S", [("f", src_v)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "f", tgt_v, policy=policy)
        if collisions:
            d.resolve_collisions(collisions)
        rewriter, _target = DefinitionsRewriter.from_directives(src, d)
        return rewriter, s

    def test_set_widen_is_class_a_no_policy(self):
        rw, s = self._mk(V.TypeSet(T.INT32), V.TypeSet(T.INT64))        # no policy — lossless
        out = rw.value(V.ValueStructure(s, {"f": V.ValueSet(V.TypeSet(T.INT32), [1, 2])}))
        self.assertEqual({1, 2}, out.at("f", encoded=True))

    def test_vector_narrow_saturate_per_element(self):
        rw, s = self._mk(V.TypeVector(T.INT64), V.TypeVector(T.INT32), policy="saturate")
        out = rw.value(V.ValueStructure(s, {"f": V.ValueVector(V.TypeVector(T.INT64), [1, 2 ** 40])}))
        self.assertEqual([1, 2 ** 31 - 1], out.at("f", encoded=True))

    def test_map_value_narrow_saturate(self):
        rw, s = self._mk(V.TypeMap(T.STRING, T.INT64), V.TypeMap(T.STRING, T.INT32), policy="saturate")
        out = rw.value(V.ValueStructure(
            s, {"f": V.ValueMap(V.TypeMap(T.STRING, T.INT64), {"a": 2 ** 40, "b": 3})}))
        self.assertEqual({"a": 2 ** 31 - 1, "b": 3}, out.at("f", encoded=True))

    def test_xarray_narrow_preserves_positions(self):
        rw, s = self._mk(V.TypeXArray(T.INT64), V.TypeXArray(T.INT32), policy="saturate")
        x = V.ValueXArray(V.TypeXArray(T.INT64))
        x.insert(V.ValueXArray.END, 5, V.ValueUUId("00000001-0000-0000-0000-000000000001"))
        x.insert(V.ValueXArray.END, 2 ** 40, V.ValueUUId("00000001-0000-0000-0000-000000000002"))
        out = rw.value(V.ValueStructure(s, {"f": x}))
        vec = V.ValueXArray.cast(out.at("f", encoded=False)).to_vector()   # live elems, position order
        self.assertEqual([5, 2 ** 31 - 1], [vec.at(i, encoded=True) for i in range(vec.size())])

    def test_narrow_without_policy_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            self._mk(V.TypeSet(T.INT64), V.TypeSet(T.INT32))           # narrow, no policy
        self.assertIn("element narrowing", str(cm.exception))

    def test_set_element_collapse_needs_collision_policy(self):
        # two int64 saturate to the same int32 max → the set collapses a member (Class B)
        rw, s = self._mk(V.TypeSet(T.INT64), V.TypeSet(T.INT32), policy="saturate")
        with self.assertRaises(ValueError) as cm:
            rw.value(V.ValueStructure(s, {"f": V.ValueSet(V.TypeSet(T.INT64), [2 ** 40, 2 ** 41])}))
        self.assertIn("collapse", str(cm.exception))

    def test_set_element_collapse_first_resolves(self):
        rw, s = self._mk(V.TypeSet(T.INT64), V.TypeSet(T.INT32), policy="saturate", collisions="first")
        out = rw.value(V.ValueStructure(s, {"f": V.ValueSet(V.TypeSet(T.INT64), [2 ** 40, 2 ** 41])}))
        self.assertEqual({2 ** 31 - 1}, out.at("f", encoded=True))

    def test_nested_container_narrow_is_policied(self):
        # Set<Vector<int64>> → Set<Vector<int32>>: the INNER narrowing is policied through the
        # recursion (not silently converted nor crashed).
        sv, tv = V.TypeSet(V.TypeVector(T.INT64)), V.TypeSet(V.TypeVector(T.INT32))
        rw, s = self._mk(sv, tv, policy="saturate")
        inner = V.ValueSet(sv)
        inner.add(V.ValueVector(V.TypeVector(T.INT64), [1, 2 ** 40]))
        out = rw.value(V.ValueStructure(s, {"f": inner}))
        self.assertEqual([[1, 2 ** 31 - 1]], out.at("f", encoded=True))

    def test_nested_container_narrow_without_policy_refused(self):
        sv, tv = V.TypeSet(V.TypeVector(T.INT64)), V.TypeSet(V.TypeVector(T.INT32))
        with self.assertRaises(ValueError) as cm:
            self._mk(sv, tv)                                            # nested narrow, no policy
        self.assertIn("element narrowing", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
