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

# a function pool declares no persistence — it sits *outside* the namespace, at top level,
# and its signatures reference named types (qualified, `Shop::Order`). The engine's digest
# ignores it, but its type references must still follow a rename / namespace-rename so the
# patched tree keeps resolving.
TOOLS = '''"""Order tools."""
function_pool Tools {8d5b40a5-f9a3-4d0e-83dd-90dd282d3cbe} {
  """summarise an order"""
  Shop::Order summarise(Shop::Order o);
  float total(Shop::Order o, float rate);
};
'''


def _namespace(source_defs, name):
    for defn in (*source_defs.concepts(), *source_defs.structures(),
                 *source_defs.enumerations()):
        ns = defn.type_name().name_space()
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

    def test_document_field_authors_at_the_field_indent(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.document_field("Shop::Order", "quantity", "how many")
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        # the docstring sits on its own line at the field's indent, not doubled
        self.assertIn('    """how many"""\n    uint32 quantity = 1;', out["shop.dsm"])

    def test_document_case_authors_and_overrides(self):
        catalog = ('namespace Shop {11111111-1111-1111-1111-111111111111} {\n\n'
                   'enum Status {\n'
                   '    pending,\n'
                   '    """being shipped"""\n'
                   '    shipped,\n'
                   '    cancelled\n'
                   '};\n\n};\n')

        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.document_case("Shop::Status", "pending", "not yet shipped")     # bare -> authored
            d.document_case("Shop::Status", "shipped", "handed to carrier")   # override
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": catalog}, fn)
        self.assertIn('    """not yet shipped"""\n    pending,', out["catalog.dsm"])
        self.assertIn("handed to carrier", out["catalog.dsm"])
        self.assertNotIn("being shipped", out["catalog.dsm"])

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

    # -- function pools: type references in signatures follow the type edits ------------

    def test_type_rename_propagates_into_pool_signature(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.rename_type("Shop::Order", "Shop::PurchaseOrder")
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG, "tools.dsm": TOOLS}, fn)
        # qualification preserved: Shop::Order -> Shop::PurchaseOrder inside the pool
        self.assertIn("Shop::PurchaseOrder summarise(Shop::PurchaseOrder o);", out["tools.dsm"])
        self.assertNotIn("Shop::Order", out["tools.dsm"])

    def test_namespace_rename_propagates_into_qualified_pool_references(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.rename_namespace(_namespace(defs, "Shop"), "Store")
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG, "tools.dsm": TOOLS}, fn)
        self.assertIn("Store::Order summarise(Store::Order o);", out["tools.dsm"])
        self.assertNotIn("Shop::Order", out["tools.dsm"])

    # -- move_type: relocate a declaration to another namespace (n:m algebra) ------------

    def test_move_type_split_into_new_namespace(self):
        model = ('namespace N {22222222-2222-2222-2222-222222222222} {\n\n'
                 '"""A catalogued item."""\n'
                 'struct Item { uint32 id; };\n\n'
                 'struct Basket { Item first; };\n\n'
                 '};\n')
        tools = ('function_pool Tools {8d5b40a5-f9a3-4d0e-83dd-90dd282d3cbe} {\n'
                 '  N::Item pick(N::Item x);\n'
                 '};\n')

        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            cat = V.NameSpace(V.ValueUUId("33333333-3333-3333-3333-333333333333"), "Cat")
            d.move_type("N::Item", cat)
            return d
        out = self._run({"model.dsm": model, "tools.dsm": tools}, fn)
        # the declaration (with its docstring) now lives under a fresh namespace Cat
        self.assertIn("namespace Cat {33333333-3333-3333-3333-333333333333}", out["model.dsm"])
        self.assertIn('"""A catalogued item."""', out["model.dsm"])
        # every reference is fully re-qualified — the bare sibling field and the pool signature
        self.assertIn("Cat::Item first;", out["model.dsm"])
        self.assertIn("Cat::Item pick(Cat::Item x);", out["tools.dsm"])

    def test_move_type_merge_with_rename_and_retype(self):
        core = ('namespace M {44444444-4444-4444-4444-444444444444} {\n\n'
                'struct Anchor { uint32 tag; };\n\n'
                '};\n')
        model = ('namespace N {22222222-2222-2222-2222-222222222222} {\n\n'
                 '"""A catalogued item."""\n'
                 'struct Item { uint32 id; uint16 code; };\n\n'
                 'struct Basket { Item first; };\n\n'
                 '};\n')

        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            m = _namespace(defs, "M")
            d = TransformationDirectives()
            d.move_type("N::Item", m)                          # move into an existing namespace
            d.rename_type("N::Item", "N::Widget")              # rename — baked into the carried text
            d.retype_field("N::Item", "code", V.Type.UINT32)   # retype a field of the moved type too
            return d
        out = self._run({"core.dsm": core, "model.dsm": model}, fn)
        # merged INTO the live M block (not a second adjacent one), carrying its own edits
        self.assertEqual(out["core.dsm"].count("namespace M {"), 1)
        self.assertRegex(out["core.dsm"], r"struct Anchor[\s\S]*struct Widget \{ uint32 id; uint32 code; \}")
        self.assertIn("M::Widget first;", out["model.dsm"])    # reference: moved + renamed -> qualified
        self.assertNotIn("struct Item", out["model.dsm"])

    def test_move_type_merge_is_brace_safe(self):
        # the target block holds a docstring and a string default that both contain braces —
        # the merge must find the block's real closing brace, not one inside a literal
        core = ('namespace M {44444444-4444-4444-4444-444444444444} {\n\n'
                '"""tricky doc with a { and a } brace"""\n'
                'struct Anchor { string tag = "a } brace { here"; };\n\n'
                '};\n')
        model = ('namespace N {22222222-2222-2222-2222-222222222222} {\n'
                 'struct Item { uint32 id; };\n};\n')

        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.move_type("N::Item", _namespace(defs, "M"))
            return d
        out = self._run({"core.dsm": core, "model.dsm": model}, fn)
        self.assertEqual(out["core.dsm"].count("namespace M {"), 1)
        self.assertRegex(out["core.dsm"], r"struct Anchor[\s\S]*struct Item")   # merged inside, in order

    # -- reorder: rewrite the member region in the target order (a permutation) ----------

    def test_reorder_fields_with_rename_retype_and_add(self):
        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.rename_field("Shop::Order", "quantity", "qty")
            d.retype_field("Shop::Order", "legacy_code", V.Type.UINT32)
            d.add_field("Shop::Order", "priority", V.Value.create(V.Type.UINT8, 5))
            d.reorder_fields("Shop::Order",
                             ["buyer", "qty", "priority", "legacy_code", "position", "basis"])
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": CATALOG}, fn)
        body = out["shop.dsm"]
        # target order, each member carrying its own edit (rename keeps default, retype, add)
        order = [body.index(m) for m in ("key<Customer> buyer;", "uint32 qty = 1;",
                                         "uint8 priority = 5;", "uint32 legacy_code;")]
        self.assertEqual(order, sorted(order))

    def test_reorder_cases_moves_a_documented_case(self):
        catalog = ('namespace Shop {11111111-1111-1111-1111-111111111111} {\n\n'
                   '"""Order lifecycle status."""\n'
                   'enum Status {\n'
                   '    pending,\n'
                   '    """being shipped"""\n'
                   '    shipped,\n'
                   '    cancelled\n'
                   '};\n\n};\n')

        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.rename_case("Shop::Status", "cancelled", "voided")
            d.reorder_cases("Shop::Status", ["voided", "pending", "shipped"])
            return d
        out = self._run({"shop.dsm": SHOP, "catalog.dsm": catalog}, fn)
        body = out["catalog.dsm"]
        self.assertLess(body.index("voided"), body.index("pending"))     # target order
        self.assertLess(body.index("pending"), body.index("shipped"))
        self.assertIn('"""being shipped"""', body)                       # the case's doc travelled with it

    # -- attachments: declarations too, so the same machinery patches them ---------------

    def test_attachment_operations(self):
        model = ('namespace N {22222222-2222-2222-2222-222222222222} {\n\n'
                 '"""A person."""\n'
                 'concept Person;\n\n'
                 '"""orders placed"""\n'
                 'attachment<Person, uint32> orders;\n\n'
                 'attachment<Person, bool> flags;\n\n'
                 '};\n')

        def rename(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.rename_attachment("orders", "purchaseOrders")    # keyed by LOCAL name
            return d

        def drop(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            d.drop_attachment("flags")
            d.accept_attachment_drops()
            return d

        def move(defs):
            from dsviper_database_tools import TransformationDirectives
            d = TransformationDirectives()
            cat = V.NameSpace(V.ValueUUId("33333333-3333-3333-3333-333333333333"), "Cat")
            d.move_attachment("orders", cat)
            return d

        out = self._run({"model.dsm": model}, rename)
        self.assertIn("attachment<Person, uint32> purchaseOrders;", out["model.dsm"])

        out = self._run({"model.dsm": model}, drop)
        self.assertNotIn("flags", out["model.dsm"])

        out = self._run({"model.dsm": model}, move)
        self.assertIn("namespace Cat {33333333-3333-3333-3333-333333333333}", out["model.dsm"])
        # the moved attachment's key concept, staying in N, is re-qualified so it still resolves
        self.assertIn("attachment<N::Person, uint32> orders;", out["model.dsm"])

    # -- transform_type: a global type substitution at every occurrence, nested included -------

    def test_transform_type_primitive_named_and_composite(self):
        model = ('namespace N {22222222-2222-2222-2222-222222222222} {\n\n'
                 'struct A { uint32 x; };\n'
                 'struct B { uint32 x; float y; };\n'
                 'struct S {\n'
                 '    uint16 count = 3;\n'                        # primitive, with a default
                 '    map<uint16, vector<int32>> grid;\n'         # nested primitive + nested composite
                 '    A a;\n'                                     # named
                 '    vector<A> many;\n'
                 '};\n\n'
                 '};\n')

        def fn(defs):
            from dsviper_database_tools import TransformationDirectives
            a = next(s for s in defs.structures() if s.representation() == "N::A")
            b = next(s for s in defs.structures() if s.representation() == "N::B")
            d = TransformationDirectives()
            d.transform_type(V.Type.UINT16, V.Type.UINT32, lambda v, t: v)          # primitive, everywhere
            d.transform_type(V.TypeVector(V.Type.INT32), V.TypeSet(V.Type.INT32), lambda v, t: v)  # composite
            d.transform_type(a, b, lambda v, t: v)                                   # named A -> B
            return d
        out = self._run({"model.dsm": model}, fn)
        body = out["model.dsm"]
        self.assertIn("uint32 count = 3;", body)                 # primitive incl. its default's type
        self.assertIn("map<uint32, set<int32>>", body)           # nested primitive AND nested composite
        self.assertIn("N::B a;", body)                           # named -> fully qualified
        self.assertIn("vector<N::B> many;", body)
        self.assertNotIn("struct A {", body)                     # the engine drops the transformed decl


if __name__ == "__main__":
    unittest.main()
