"""Diagnostic-sink tests — the dynamic report of REAL per-site loss.

The engine notifies a `DiagnosticSink` each time a Class-B policy actually bites (an
offender saturated / defaulted / dropped, a member collapsed); exact conversions
(in-range, parseable, non-nil) never emit. These tests attach a sink directly to the
rewriter and drive `value()` — the format-agnostic engine, no database — then assert the
aggregate: which site, which op, how many, and the before→after samples. A second class
covers `DiagnosticSink` aggregation itself, and a third the `dry_run` integration.
"""

import math
import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsRewriter, Unrepresentable,
    DiagnosticSink, format_report, migrate_database)

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


def observe(rewriter, sv, max_samples=5):
    """Rewrite `sv` under a fresh sink; return the aggregate report."""
    sink = DiagnosticSink(max_samples=max_samples)
    rewriter._sink = sink
    try:
        rewriter.value(sv)
    finally:
        rewriter._sink = None
    return sink.report()


def only(report):
    """Assert exactly one lossy site and return it."""
    sites = report["sites"]
    assert len(sites) == 1, f"expected 1 site, got {sites}"
    return sites[0]


class TestLeafEmissions(unittest.TestCase):
    def _retype(self, src_leaf, tgt_leaf, policy, field="n"):
        src = V.Definitions()
        s = struct(src, "W", [(field, src_leaf)])
        d = TransformationDirectives()
        d.retype_field(s.representation(), field, tgt_leaf, policy=policy)
        rewriter, _ = DefinitionsRewriter.from_directives(src, d)
        return rewriter, s

    def test_narrow_saturate_emits_before_after(self):
        r, s = self._retype(T.INT64, T.INT32, "saturate")
        rep = observe(r, V.ValueStructure(s, {"n": 2**40}))
        site = only(rep)
        self.assertEqual("Demo::W.n", site["site"])
        self.assertEqual("narrow int64→int32", site["op"])
        self.assertEqual("saturate", site["policy"])
        self.assertEqual(1, site["count"])
        self.assertEqual([(repr(2**40), "2147483647")], site["samples"])

    def test_narrow_default_emits(self):
        r, s = self._retype(T.INT64, T.INT32, ("default", V.ValueInt32(-1)))
        site = only(observe(r, V.ValueStructure(s, {"n": 2**40})))
        self.assertEqual("narrow int64→int32", site["op"])
        self.assertEqual((repr(2**40), "-1"), site["samples"][0])

    def test_in_range_exact_does_not_emit(self):
        r, s = self._retype(T.INT64, T.INT32, "saturate")
        rep = observe(r, V.ValueStructure(s, {"n": 100}))
        self.assertEqual([], rep["sites"])          # lossless: the contract is silent

    def test_float_fraction_truncation_emits(self):
        r, s = self._retype(T.DOUBLE, T.INT32, "saturate")
        site = only(observe(r, V.ValueStructure(s, {"n": 3.7})))
        self.assertEqual("float→int32 truncate", site["op"])
        self.assertEqual(("3.7", "3"), site["samples"][0])

    def test_float_integral_value_does_not_emit(self):
        r, s = self._retype(T.DOUBLE, T.INT32, "saturate")
        self.assertEqual([], observe(r, V.ValueStructure(s, {"n": 3.0}))["sites"])

    def test_float_nonfinite_saturate_emits_at_total_order_edge(self):
        r, s = self._retype(T.DOUBLE, T.INT32, "saturate")
        site = only(observe(r, V.ValueStructure(s, {"n": math.nan})))
        self.assertEqual("float→int32 edge", site["op"])  # NaN → low end of the total order
        self.assertEqual("-2147483648", site["samples"][0][1])

    def test_parse_failure_defaults_and_emits(self):
        r, s = self._retype(T.STRING, T.INT32, ("default", V.ValueInt32(-1)))
        rep = observe(r, V.ValueStructure(s, {"n": "abc"}))
        site = only(rep)
        self.assertEqual("parse→int32", site["op"])
        self.assertEqual(("abc", "-1"), site["samples"][0])

    def test_parse_success_does_not_emit(self):
        r, s = self._retype(T.STRING, T.INT32, ("default", V.ValueInt32(-1)))
        self.assertEqual([], observe(r, V.ValueStructure(s, {"n": "7"}))["sites"])

    def test_nil_unwrap_default_emits(self):
        r, s = self._retype(V.TypeOptional(T.INT32), T.INT32, ("default", V.ValueInt32(0)))
        site = only(observe(r, V.ValueStructure(s, {"n": V.ValueOptional(V.TypeOptional(T.INT32))})))
        self.assertEqual("nil-unwrap", site["op"])
        self.assertEqual(("nil", "0"), site["samples"][0])

    def test_nil_unwrap_drop_record_emits_then_raises(self):
        r, s = self._retype(V.TypeOptional(T.INT32), T.INT32, "drop-record")
        sink = DiagnosticSink()
        r._sink = sink
        try:
            with self.assertRaises(Unrepresentable):
                r.value(V.ValueStructure(s, {"n": V.ValueOptional(V.TypeOptional(T.INT32))}))
        finally:
            r._sink = None
        site = only(sink.report())
        self.assertEqual("nil-unwrap", site["op"])
        self.assertEqual(("nil", None), site["samples"][0])    # None = the value was elided


class TestVecMatEmissions(unittest.TestCase):
    def test_vec_element_narrow_emits_per_element_site(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVec(T.INT64, 3))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVec(T.INT32, 3), policy="saturate")
        r, _ = DefinitionsRewriter.from_directives(src, d)
        vec = V.ValueVec(V.TypeVec(T.INT64, 3), [2**40, 5, -2**40])   # indices 0 & 2 overflow, 1 in range
        rep = observe(r, V.ValueStructure(s, {"p": vec}))
        self.assertEqual({"Demo::S.p[0]", "Demo::S.p[2]"}, {x["site"] for x in rep["sites"]})
        self.assertTrue(all(x["op"] == "narrow int64→int32" for x in rep["sites"]))

    def test_vector_to_vec_length_fit_emits(self):
        src = V.Definitions()
        s = struct(src, "S", [("p", V.TypeVector(T.INT32))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "p", V.TypeVec(T.INT32, 4), policy=("fit", 0))
        r, _ = DefinitionsRewriter.from_directives(src, d)
        vv = V.ValueVector(V.TypeVector(T.INT32))
        for x in (1, 2):
            vv.append(x)
        site = only(observe(r, V.ValueStructure(s, {"p": vv})))
        self.assertEqual("Vector→Vec length 2→4", site["op"])
        self.assertEqual(("2", "4"), site["samples"][0])       # length 2 fitted to 4

    def test_resize_shrink_accept_emits(self):
        src = V.Definitions()
        s = struct(src, "S", [("m", V.TypeMat(T.INT32, 2, 3))])
        d = TransformationDirectives()
        d.resize_mat_field(s.representation(), "m", 2, 2, on_shrink="accept")   # drops a row
        r, _ = DefinitionsRewriter.from_directives(src, d)
        mat = V.ValueMat(V.TypeMat(T.INT32, 2, 3))
        for c in range(2):
            for r_ in range(3):
                mat.set(c, r_, 0)
        site = only(observe(r, V.ValueStructure(s, {"m": mat})))
        self.assertEqual("resize-shrink", site["op"])
        self.assertEqual(("2×3", "2×2"), site["samples"][0])

    def test_mat_element_widen_is_silent(self):
        # widening loses nothing → no finding, even element-wise
        src = V.Definitions()
        s = struct(src, "S", [("m", V.TypeMat(T.FLOAT, 2, 2))])
        d = TransformationDirectives()
        d.retype_field(s.representation(), "m", V.TypeMat(T.DOUBLE, 2, 2))
        r, _ = DefinitionsRewriter.from_directives(src, d)
        rep = observe(r, V.ValueStructure(s, {"m": V.ValueMat(V.TypeMat(T.FLOAT, 2, 2), [[1.0, 2.0], [3.0, 4.0]])}))
        self.assertEqual([], rep["sites"])


class TestEnumAndContainerEmissions(unittest.TestCase):
    def _enum_defs(self):
        src = V.Definitions()
        ed = V.TypeEnumerationDescriptor("Mode")
        ed.add_case("Old"); ed.add_case("New")
        e = src.create_enumeration(NS, ed)
        return src, e

    def test_remove_case_map_case_emits(self):
        src, e = self._enum_defs()
        s = struct(src, "R", [("m", e)])
        d = TransformationDirectives()
        d.remove_case(e.representation(), "Old", ("map-case", "New"))
        r, _ = DefinitionsRewriter.from_directives(src, d)
        site = only(observe(r, V.ValueStructure(s, {"m": V.ValueEnumeration(e, "Old")})))
        self.assertEqual("remove-case", site["op"])
        self.assertEqual(("Old", "New"), site["samples"][0])

    def test_remove_case_inside_vector_attributes_element_site(self):
        # the loss is INSIDE a collection element — the site carries the `[]` marker
        src, e = self._enum_defs()
        s = struct(src, "R", [("modes", V.TypeVector(e))])
        d = TransformationDirectives()
        d.remove_case(e.representation(), "Old", ("map-case", "New"))
        r, _ = DefinitionsRewriter.from_directives(src, d)
        vec = V.ValueVector(V.TypeVector(e))
        vec.append(V.ValueEnumeration(e, "Old"))
        vec.append(V.ValueEnumeration(e, "New"))          # not removed → no emit
        site = only(observe(r, V.ValueStructure(s, {"modes": vec})))
        self.assertEqual("Demo::R.modes[]", site["site"])
        self.assertEqual(1, site["count"])

    def test_set_collapse_emits_per_dropped_member(self):
        # Set<Mode> with both Old and New; Old→New makes them equal → one member collapses
        src, e = self._enum_defs()
        s = struct(src, "R", [("tags", V.TypeSet(e))])
        d = TransformationDirectives()
        d.remove_case(e.representation(), "Old", ("map-case", "New"))
        d.resolve_collisions("first")
        r, _ = DefinitionsRewriter.from_directives(src, d)
        st = V.ValueSet(V.TypeSet(e))
        st.add(V.ValueEnumeration(e, "Old"))
        st.add(V.ValueEnumeration(e, "New"))
        rep = observe(r, V.ValueStructure(s, {"tags": st}))
        ops = {x["op"] for x in rep["sites"]}
        self.assertIn("set-collapse", ops)
        collapse = [x for x in rep["sites"] if x["op"] == "set-collapse"][0]
        self.assertEqual("Demo::R.tags", collapse["site"])
        self.assertEqual("first", collapse["policy"])

    def test_map_collision_last_reports_overwritten_value(self):
        src, e = self._enum_defs()
        s = struct(src, "R", [("cfgs", V.TypeMap(e, T.INT32))])
        d = TransformationDirectives()
        d.remove_case(e.representation(), "Old", ("map-case", "New"))
        d.resolve_collisions("last")
        r, _ = DefinitionsRewriter.from_directives(src, d)
        mv = V.ValueMap(V.TypeMap(e, T.INT32))
        mv.set(V.ValueEnumeration(e, "Old"), 1)           # keyed Old → New
        mv.set(V.ValueEnumeration(e, "New"), 2)           # collides on New
        rep = observe(r, V.ValueStructure(s, {"cfgs": mv}))
        coll = [x for x in rep["sites"] if x["op"] == "map-collision"]
        self.assertEqual(1, len(coll))
        self.assertEqual("Demo::R.cfgs", coll[0]["site"])
        self.assertEqual("last", coll[0]["policy"])


class TestSinkAggregation(unittest.TestCase):
    def test_counts_are_exact_but_samples_are_capped(self):
        sink = DiagnosticSink(max_samples=2)
        for i in range(5):
            sink({"site": "S.f", "op": "narrow int64→int32", "policy": "saturate",
                  "before": str(i), "after": "0"})
        site = only_report(sink.report())
        self.assertEqual(5, site["count"])                 # count exact
        self.assertEqual(2, len(site["samples"]))          # samples bounded

    def test_distinct_site_op_groups_are_separate(self):
        sink = DiagnosticSink()
        sink({"site": "S.a", "op": "narrow", "policy": None, "before": "1", "after": "0"})
        sink({"site": "S.b", "op": "narrow", "policy": None, "before": "1", "after": "0"})
        sink({"site": "S.a", "op": "parse", "policy": None, "before": "x", "after": "0"})
        rep = sink.report()
        self.assertEqual(3, rep["summary"]["sites"])
        self.assertEqual(3, rep["summary"]["findings"])

    def test_dropped_summary_counts_elided_findings(self):
        sink = DiagnosticSink()
        sink({"site": "S.x", "op": "nil-unwrap", "policy": "drop-record",
              "before": "nil", "after": None})
        self.assertEqual(1, sink.report()["summary"]["dropped"])

    def test_format_report_is_readable(self):
        sink = DiagnosticSink()
        sink({"site": "Demo::W.n", "op": "narrow int64→int32", "policy": "saturate",
              "before": "1099511627776", "after": "2147483647"})
        text = format_report(sink.report())
        self.assertIn("narrow int64→int32", text)
        self.assertIn("Demo::W.n", text)
        self.assertIn("→", text)

    def test_empty_report_says_nothing_lost(self):
        self.assertIn("nothing was lost", format_report(DiagnosticSink().report()))


def only_report(report):
    sites = report["sites"]
    assert len(sites) == 1, sites
    return sites[0]


class TestDryRunDiagnostics(unittest.TestCase):
    """The dry-run surfaces the same findings over a whole Database, with no writes."""

    def test_dry_run_reports_real_saturations_with_samples(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        concept = defs.create_concept(NS, "Acc")
        doc_t = struct(defs, "Doc", [("n", T.INT64)])
        defs.create_attachment(NS, "Docs", concept, doc_t)
        src_db.extend_definitions(defs.const())
        att = src_db.definitions().attachments()[0]
        uuids = ["11111111-1111-1111-1111-111111111111",
                 "22222222-2222-2222-2222-222222222222",
                 "33333333-3333-3333-3333-333333333333"]
        src_db.begin_transaction()
        for uid, val in zip(uuids, (2**40, 5, 2**41)):     # two offenders, one in range
            src_db.set(att, att.create_key(V.ValueUUId(uid)),
                       V.ValueStructure(doc_t, {"n": val}))
        src_db.commit()

        d = TransformationDirectives()
        d.retype_field(doc_t.representation(), "n", T.INT32, policy="saturate")
        rewriter, _ = DefinitionsRewriter.from_directives(src_db.definitions(), d)

        info = migrate_database.dry_run(src_db, rewriter)
        self.assertEqual(3, info["documents"])             # nothing dropped, all kept
        diag = info["diagnostics"]
        site = only(diag)
        self.assertEqual("Demo::Doc.n", site["site"])
        self.assertEqual(2, site["count"])                 # exactly the two out-of-range docs
        self.assertEqual("saturate", site["policy"])


if __name__ == "__main__":
    unittest.main()
