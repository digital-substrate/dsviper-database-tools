"""`definitions_migrate.py` — the DSM-source twin of the data migration.

A schema change migrates two artefacts: the data in a base and the hand-authored `.dsm`
files that document that schema. `definitions_migrate` patches the `.dsm` source as a
structured codemod (span-precise edits, file split preserved), and self-checks against the
engine: re-parse the patched tree and compare its `Definitions` digest to the engine's
target. The `runtimeId` is a structure fingerprint (field defaults included), so an equal
digest proves the patch faithful — that comparison is the assertion these tests rest on.

The tool needs the parser's `DSMSourceMap` by-product (`dsviper >= 1.2.6`), which is newer
than the shipped floor. Every test therefore live-probes the installed binding and skips
cleanly where the source-map surface is absent, so the suite documents the contract without
breaking on an older peer.
"""

import os
import tempfile
import unittest

import dsviper as V


def _source_map_available():
    """True iff the installed binding exposes the parser source-map surface."""
    return hasattr(V, "DSMSourceMap") and hasattr(V, "DSMBuilder")


_SM = _source_map_available()

if _SM:
    import definitions_migrate as dm


class _Transformation:
    """A stand-in for a ``transformation.py`` module: any object exposing
    ``build_directives(source_defs) -> TransformationDirectives``."""

    def __init__(self, fn):
        self.build_directives = fn


SHOP = '''namespace Shop {11111111-1111-1111-1111-111111111111} {

"""A customer of the shop."""
concept Customer;

struct Order {
    key<Customer> buyer;
    uint32 quantity = 1;
    uint16 legacy_code;
    vec<float,3> position;
    mat<float,2,2> basis;
};

};
'''

CATALOG = '''namespace Shop {11111111-1111-1111-1111-111111111111} {

"""Order lifecycle status."""
enum Status {
    pending,
    shipped,
    cancelled
};

};
'''


def _namespace(source_defs, name):
    for c in source_defs.concepts():
        ns = c.type_name().name_space()
        if ns.name() == name:
            return ns
    raise KeyError(name)


@unittest.skipUnless(_SM, "binding has no DSMSourceMap (parser source-map surface)")
class DefinitionsMigrateTest(unittest.TestCase):
    """Each test builds a two-file `.dsm` tree, runs the codemod with `verify=True`
    (the digest oracle is the pass/fail), and asserts the patched text where it matters."""

    def _run(self, files, fn):
        src = tempfile.mkdtemp()
        out = tempfile.mkdtemp()
        for name, text in files.items():
            with open(os.path.join(src, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        dm.definitions_migrate(src, _Transformation(fn), out, verify=True)   # raises on mismatch
        patched = {}
        for name in files:
            with open(os.path.join(out, name), encoding="utf-8") as handle:
                patched[name] = handle.read()
        return patched

    # -- renames (family 1) -------------------------------------------------------------

    def test_rename_type_patches_declaration_and_references(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.rename_type("Shop::Order", "Shop::PurchaseOrder")
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertIn("struct PurchaseOrder", out["shop.dsm"])
        self.assertNotIn("struct Order", out["shop.dsm"])

    def test_rename_field_preserves_its_default(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.rename_field("Shop::Order", "quantity", "qty")
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertIn("uint32 qty = 1;", out["shop.dsm"])       # the default rides the rename

    def test_rename_case(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.rename_case("Shop::Status", "cancelled", "voided")
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertIn("voided", out["catalog.dsm"])
        self.assertNotIn("cancelled", out["catalog.dsm"])

    # -- type change: retype / resize / transpose via the engine target type ------------

    def test_retype_widens_field(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.retype_field("Shop::Order", "legacy_code", V.Type.UINT32)
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertIn("uint32 legacy_code;", out["shop.dsm"])

    def test_resize_vec_field(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.resize_vec_field("Shop::Order", "position", 4)
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertRegex(out["shop.dsm"], r"vec<float,\s*4>\s+position;")

    # -- add / drop (family 2) ----------------------------------------------------------

    def test_add_field_renders_default(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.add_field("Shop::Order", "priority", V.Value.create(V.Type.UINT8, 3))
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertIn("uint8 priority = 3;", out["shop.dsm"])

    def test_drop_field_leaves_no_dangling_terminator(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.drop_field("Shop::Order", "legacy_code")
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertNotIn("legacy_code", out["shop.dsm"])
        self.assertNotIn("\n    ;", out["shop.dsm"])            # no orphan ';'

    def test_add_case(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.add_case("Shop::Status", "returned")
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertIn("returned", out["catalog.dsm"])

    def test_remove_middle_case(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.remove_case("Shop::Status", "shipped", "fail")
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertNotIn("shipped", out["catalog.dsm"])
        self.assertIn("pending", out["catalog.dsm"])
        self.assertIn("cancelled", out["catalog.dsm"])

    # -- documentation (Class A) --------------------------------------------------------

    def test_document_type_authors_and_overrides(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.document_type("Shop::Order", "An order placed by a customer.")   # bare -> authored
            d.document_type("Shop::Status", "The lifecycle state of an order.")  # override
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        self.assertIn("An order placed by a customer.", out["shop.dsm"])
        self.assertIn("The lifecycle state of an order.", out["catalog.dsm"])
        self.assertNotIn("Order lifecycle status.", out["catalog.dsm"])

    # -- namespace: two orthogonal axes, patched across the file split ------------------

    def test_rename_and_remap_namespace_across_files(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            shop = _namespace(defs, "Shop")
            d.rename_namespace(shop, "Store")
            d.remap_namespace(shop, V.ValueUUId("99999999-9999-9999-9999-999999999999"))
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        for text in out.values():                              # every occurrence, both files
            self.assertIn("namespace Store {99999999-9999-9999-9999-999999999999}", text)
            self.assertNotIn("Shop {11111111", text)

    # -- fail closed on a directive the codemod does not yet patch -----------------------

    def test_unsupported_directive_is_refused(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.reorder_fields("Shop::Order",
                             ["quantity", "buyer", "legacy_code", "position", "basis"])
            return d
        with self.assertRaises(NotImplementedError):
            self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)


if __name__ == "__main__":
    unittest.main()
