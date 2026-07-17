"""Static plan report — classify a migration from directives + definitions alone, no data.
Identify (Class A/B/refused, lossy) + warnings (missing policy, float→int, forgotten rename)."""

import unittest

import dsviper as V

from dsviper_database_tools import TransformationDirectives, plan, format_plan

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Shop")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


class TestPlanReport(unittest.TestCase):
    def _defs(self):
        defs = V.Definitions()
        order = struct(defs, "Order", [
            ("qty", T.INT64), ("amount", T.DOUBLE), ("label", T.STRING),
            ("legacy", T.STRING), ("note", T.STRING)])
        return defs, order

    def _sites(self, report):
        return {c["site"]: c for c in report["changes"]}

    def test_classifies_a_b_and_lossy(self):
        defs, order = self._defs()
        d = TransformationDirectives()
        d.rename_field(order.representation(), "label", "title")          # A, no loss
        d.retype_field(order.representation(), "qty", T.INT32, policy="saturate")  # B narrowing, policied
        d.add_field(order.representation(), "email", V.ValueString(""))   # A, no loss
        report = plan(defs, d)
        by = self._sites(report)
        self.assertEqual(("A", False), (by["Shop::Order.label"]["class"], by["Shop::Order.label"]["loss"]))
        self.assertEqual(("B", True), (by["Shop::Order.qty"]["class"], by["Shop::Order.qty"]["loss"]))
        self.assertEqual("saturate", by["Shop::Order.qty"]["policy"])
        self.assertEqual(("A", False), (by["Shop::Order.email"]["class"], by["Shop::Order.email"]["loss"]))
        self.assertEqual([], report["warnings"])                          # everything decreed

    def test_widening_is_class_a(self):
        defs2 = V.Definitions()
        m = struct(defs2, "M", [("n", T.INT32)])
        d = TransformationDirectives()
        d.retype_field(m.representation(), "n", T.INT64)                 # int32->int64 widening (A)
        report = plan(defs2, d)
        self.assertEqual("A", self._sites(report)["Shop::M.n"]["class"])

    def test_drop_is_class_a_but_flagged_lossy(self):
        defs, order = self._defs()
        d = TransformationDirectives()
        d.drop_field(order.representation(), "note")
        c = self._sites(plan(defs, d))["Shop::Order.note"]
        self.assertEqual(("A", True), (c["class"], c["loss"]))           # engine-total, but DATA LOSS

    def test_missing_policy_warns(self):
        defs, order = self._defs()
        d = TransformationDirectives()
        d.retype_field(order.representation(), "qty", T.INT32)           # narrowing, NO policy
        report = plan(defs, d)
        self.assertTrue(any("missing policy" in w and "Shop::Order.qty" in w for w in report["warnings"]))

    def test_float_to_int_flagged(self):
        defs, order = self._defs()
        d = TransformationDirectives()
        d.retype_field(order.representation(), "amount", T.INT32, policy="saturate")
        c = self._sites(plan(defs, d))["Shop::Order.amount"]
        self.assertEqual("B", c["class"])
        self.assertIn("float→int", c["detail"])                         # RW-F3 surfaced

    def test_forgotten_rename_pair_warns(self):
        defs, order = self._defs()
        d = TransformationDirectives()
        d.drop_field(order.representation(), "legacy")
        d.add_field(order.representation(), "legacyId", V.ValueString(""))
        report = plan(defs, d)
        self.assertTrue(any("forgotten rename" in w for w in report["warnings"]))  # RW-T1

    def test_format_plan_renders(self):
        defs, order = self._defs()
        d = TransformationDirectives()
        d.retype_field(order.representation(), "qty", T.INT32)           # missing policy
        text = format_plan(plan(defs, d))
        self.assertIn("Migration plan", text)
        self.assertIn("policy=REQUIRED", text)
        self.assertIn("Warnings:", text)

    def test_non_injective_type_mapping_warns(self):
        # two types renamed to the same target -> the runtime would refuse at build; the plan
        # surfaces it early as a warning (advisory — the runtime remains the arbiter).
        defs = V.Definitions()
        a = struct(defs, "A", [("x", T.INT32)])
        b = struct(defs, "B", [("y", T.INT32)])
        d = TransformationDirectives()
        d.rename_type(a.representation(), "Shop::Merged")
        d.rename_type(b.representation(), "Shop::Merged")
        report = plan(defs, d)
        self.assertTrue(any("non-injective" in w and "Merged" in w for w in report["warnings"]))

    def test_injective_renames_do_not_warn(self):
        defs = V.Definitions()
        a = struct(defs, "A", [("x", T.INT32)])
        d = TransformationDirectives()
        d.rename_type(a.representation(), "Shop::Renamed")
        report = plan(defs, d)
        self.assertFalse(any("non-injective" in w for w in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
