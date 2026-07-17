"""Round-trip verifier — it must PASS on a faithful migration and FAIL loudly on
any divergence (value drift, dangling blob, a dropped record left behind)."""

import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsRewriter, migrate_database,
    VerificationError)

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


def _source_with_blob():
    src = V.Database.create_in_memory()
    defs = V.Definitions()
    item = defs.create_concept(NS, "Item")
    doc_t = struct(defs, "Doc", [("name", T.STRING), ("thumb", T.BLOB_ID), ("old", T.BLOB_ID)])
    defs.create_attachment(NS, "Items", item, doc_t)
    src.extend_definitions(defs.const())
    layout = V.BlobLayout("uchar", 1)
    src.begin_transaction()
    kept = src.create_blob(layout, V.ValueBlob(bytes([1, 2, 3, 4])))
    orphan = src.create_blob(layout, V.ValueBlob(bytes([9, 9, 9])))
    att = src.definitions().attachments()[0]
    key = att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
    src.set(att, key, V.ValueStructure(doc_t, {"name": "x", "thumb": kept, "old": orphan}))
    src.commit()
    return src, doc_t


class TestVerifyMigration(unittest.TestCase):
    def _migrate(self, src, directives):
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), directives)
        tgt = V.Database.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        migrate_database.migrate(src, rewriter, tgt)
        return rewriter, tgt

    def test_passes_on_faithful_rename_and_blob(self):
        src, doc_t = _source_with_blob()
        d = TransformationDirectives()
        d.rename_field(doc_t.representation(), "name", "title")
        d.drop_field(doc_t.representation(), "old")
        rewriter, tgt = self._migrate(src, d)
        info = migrate_database.verify(src, rewriter, tgt)
        self.assertEqual({"checked": 1, "dropped": 0, "referenced_blobs": 1}, info)

    def test_passes_on_drop_record(self):
        src = V.Database.create_in_memory()
        defs = V.Definitions()
        concept = defs.create_concept(NS, "R")
        doc_t = struct(defs, "Rec", [("x", V.TypeOptional(T.INT32))])
        defs.create_attachment(NS, "Recs", concept, doc_t)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        ot = V.TypeOptional(T.INT32)
        src.begin_transaction()
        src.set(att, att.create_key(V.ValueUUId("44444444-4444-4444-4444-444444444444")),
                V.ValueStructure(doc_t, {"x": V.ValueOptional(ot, 9)}))
        src.set(att, att.create_key(V.ValueUUId("55555555-5555-5555-5555-555555555555")),
                V.ValueStructure(doc_t, {"x": V.ValueOptional(ot)}))
        src.commit()
        d = TransformationDirectives()
        d.retype_field(doc_t.representation(), "x", T.INT32, policy="drop-record")
        d.accept_document_drops()                      # explicit sign-off: drops may delete docs
        rewriter, tgt = self._migrate(src, d)
        info = migrate_database.verify(src, rewriter, tgt)
        self.assertEqual(1, info["checked"])
        self.assertEqual(1, info["dropped"])

    def test_fails_on_value_drift(self):
        src, doc_t = _source_with_blob()
        d = TransformationDirectives()
        d.rename_field(doc_t.representation(), "name", "title")
        d.drop_field(doc_t.representation(), "old")
        rewriter, tgt = self._migrate(src, d)
        # tamper with the target document after a faithful migration
        tgt_att = tgt.definitions().attachments()[0]
        tk = tgt.keys(tgt_att).at(0, encoded=False)
        tdoc = V.ValueStructure.cast(tgt.get(tgt_att, tk).unwrap(encoded=False))
        tampered = V.ValueStructure(tdoc.type_structure(),
                                    {"title": "DRIFTED", "thumb": tdoc.at("thumb", encoded=False)})
        tgt.begin_transaction(); tgt.set(tgt_att, tk, tampered); tgt.commit()
        with self.assertRaises(VerificationError) as cm:
            migrate_database.verify(src, rewriter, tgt)
        self.assertIn("value mismatch", str(cm.exception))

    def test_fails_on_dangling_blob(self):
        src, doc_t = _source_with_blob()
        d = TransformationDirectives()
        d.rename_field(doc_t.representation(), "name", "title")
        d.drop_field(doc_t.representation(), "old")
        rewriter, tgt = self._migrate(src, d)
        # delete the referenced blob out from under the document
        tgt_att = tgt.definitions().attachments()[0]
        thumb = V.ValueStructure.cast(
            tgt.get(tgt_att, tgt.keys(tgt_att).at(0, encoded=False)).unwrap(encoded=False)
        ).at("thumb", encoded=False)
        tgt.begin_transaction(); tgt.del_blob(thumb); tgt.commit()
        with self.assertRaises(VerificationError) as cm:
            migrate_database.verify(src, rewriter, tgt)
        self.assertIn("blob", str(cm.exception))

    def test_fails_on_spurious_document(self):
        src, doc_t = _source_with_blob()
        d = TransformationDirectives()
        d.rename_field(doc_t.representation(), "name", "title")
        d.drop_field(doc_t.representation(), "old")
        rewriter, tgt = self._migrate(src, d)
        tgt_att = tgt.definitions().attachments()[0]
        extra = tgt_att.create_key(V.ValueUUId("66666666-6666-6666-6666-666666666666"))
        existing = V.ValueStructure.cast(
            tgt.get(tgt_att, tgt.keys(tgt_att).at(0, encoded=False)).unwrap(encoded=False))
        tgt.begin_transaction(); tgt.set(tgt_att, extra, existing); tgt.commit()
        with self.assertRaises(VerificationError) as cm:
            migrate_database.verify(src, rewriter, tgt)
        self.assertIn("documents", str(cm.exception))


class TestVerifyMirrorsMigrate(unittest.TestCase):
    """`verify` must re-derive the expected target through the SAME engine wiring `migrate`
    uses. Before this, `verify` wired neither the source view (non-local hooks) nor the
    record key (aggregate hooks), so a valid such migration self-verified with a raw
    `ValueError`. It must now pass; and a dropped attachment must be skipped, not crash."""

    def _order_shop(self):
        src = V.Database.create_in_memory()
        defs = V.Definitions()
        cust = defs.create_concept(NS, "Customer")
        cust_doc = struct(defs, "CustomerDoc", [("name", T.STRING)])
        custs = defs.create_attachment(NS, "Customers", cust, cust_doc)
        order = defs.create_concept(NS, "Order")
        order_doc = struct(defs, "OrderDoc",
                           [("custRef", custs.type_key()), ("amount", T.INT32)])
        orders = defs.create_attachment(NS, "Orders", order, order_doc)
        src.extend_definitions(defs.const())
        atts = {a.identifier().split(".")[-1]: a for a in src.definitions().attachments()}
        ck = atts["Customers"].create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        ok = atts["Orders"].create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        src.begin_transaction()
        src.set(atts["Customers"], ck, V.ValueStructure(cust_doc, {"name": "Ada"}))
        src.set(atts["Orders"], ok, V.ValueStructure(order_doc, {"custRef": ck, "amount": 7}))
        src.commit()
        return src, custs, cust_doc, order_doc

    def _migrate(self, src, d):
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.Database.create_in_memory(); tgt.extend_definitions(target_defs.const())
        migrate_database.migrate(src, rewriter, tgt)
        return rewriter, tgt

    def test_verify_passes_with_nonlocal_hook(self):
        src, custs, cust_doc, order_doc = self._order_shop()

        def derive_name(source_struct, field_name, target_type, ctx):
            c = ctx.attachment_getting.get(custs, source_struct.at("custRef", encoded=False))
            return V.ValueString(V.ValueStructure.cast(c.unwrap(encoded=False)).at("name", encoded=False))

        d = TransformationDirectives()
        d.add_field(order_doc.representation(), "customerName", T.STRING, derive=derive_name)
        rewriter, tgt = self._migrate(src, d)
        info = migrate_database.verify(src, rewriter, tgt)          # must NOT raise
        self.assertEqual(2, info["checked"])

    def test_verify_passes_with_aggregate_hook(self):
        src, custs, cust_doc, order_doc = self._order_shop()
        seen = []

        def needs_self_key(source_struct, field_name, target_type, ctx):
            seen.append(ctx.self_key.instance_id().representation())   # reads self_key
            return V.ValueInt32(0)

        d = TransformationDirectives()
        d.add_field(cust_doc.representation(), "tag", T.INT32, derive=needs_self_key)
        rewriter, tgt = self._migrate(src, d)
        seen.clear()
        info = migrate_database.verify(src, rewriter, tgt)          # must NOT raise (self_key wired)
        self.assertEqual(2, info["checked"])
        self.assertTrue(seen)                                       # the hook ran under verify

    def test_verify_skips_dropped_attachment(self):
        src, custs, cust_doc, order_doc = self._order_shop()
        d = TransformationDirectives()
        d.drop_attachment("Orders")
        d.accept_attachment_drops()
        rewriter, tgt = self._migrate(src, d)
        info = migrate_database.verify(src, rewriter, tgt)          # must NOT crash on the dropped att
        self.assertEqual(1, info["checked"])                       # only the Customer survives


if __name__ == "__main__":
    unittest.main()
