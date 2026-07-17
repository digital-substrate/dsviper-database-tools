"""Migration-loop integration test — a real read-old / write-new over in-memory
databases, including blob byte-copy and orphan mark-sweep."""

import os
import tempfile
import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsRewriter, migrate_database)

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


def migrate(src_db, directives):
    rewriter, target_defs = DefinitionsRewriter.from_directives(
        src_db.definitions(), directives)
    tgt_db = V.Database.create_in_memory()
    tgt_db.extend_definitions(target_defs.const())
    info = migrate_database.migrate(src_db, rewriter, tgt_db)     # owns its transaction
    return tgt_db, info


class TestMigrateDatabase(unittest.TestCase):
    def test_rename_field_migration_in_memory(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        device = defs.create_concept(NS, "Customer")
        reading = struct(defs, "Order", [("qty", T.INT32)])
        defs.create_attachment(NS, "Orders", device, reading)
        src_db.extend_definitions(defs.const())

        src_att = src_db.definitions().attachments()[0]
        key = src_att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        src_db.begin_transaction()
        src_db.set(src_att, key, V.ValueStructure(reading, {"qty": 5}))
        src_db.commit()

        directives = TransformationDirectives()
        directives.rename_field(reading.representation(), "qty", "count")
        tgt_db, info = migrate(src_db, directives)

        self.assertEqual(1, info["documents"])
        tgt_att = tgt_db.definitions().attachments()[0]
        tgt_keys = tgt_db.keys(tgt_att)
        self.assertEqual(1, tgt_keys.size())
        doc = tgt_db.get(tgt_att, tgt_keys.at(0, encoded=False))
        self.assertFalse(doc.is_nil())
        self.assertEqual(5, V.ValueStructure.cast(doc.unwrap(encoded=False)).at("count", encoded=False))

    def test_referenced_blob_copied_orphan_never_copied(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        item = defs.create_concept(NS, "Item")
        doc_t = struct(defs, "Doc", [("name", T.STRING),
                                     ("thumb", T.BLOB_ID), ("old", T.BLOB_ID)])
        defs.create_attachment(NS, "Items", item, doc_t)
        src_db.extend_definitions(defs.const())

        layout = V.BlobLayout("uchar", 1)
        src_db.begin_transaction()
        kept = src_db.create_blob(layout, V.ValueBlob(bytes([10, 20, 30, 40])))
        orphan = src_db.create_blob(layout, V.ValueBlob(bytes(range(50, 82))))
        att = src_db.definitions().attachments()[0]
        key = att.create_key(V.ValueUUId("33333333-3333-3333-3333-333333333333"))
        src_db.set(att, key, V.ValueStructure(doc_t, {"name": "x", "thumb": kept, "old": orphan}))
        src_db.commit()

        # migrate: rename name->title, DROP 'old' — orphans the second blob
        directives = TransformationDirectives()
        directives.rename_field(doc_t.representation(), "name", "title")
        directives.drop_field(doc_t.representation(), "old")
        tgt_db, info = migrate(src_db, directives)

        # copy-on-reference: only the referenced blob is copied — the orphan is never touched
        self.assertEqual({"documents": 1, "dropped": 0, "blobs": 1}, info)

        # the referenced blob's BYTES landed, id preserved (content-addressed)
        tgt_att = tgt_db.definitions().attachments()[0]
        tdoc = V.ValueStructure.cast(
            tgt_db.get(tgt_att, tgt_db.keys(tgt_att).at(0, encoded=False)).unwrap(encoded=False))
        self.assertEqual("x", tdoc.at("title", encoded=False))
        thumb = tdoc.at("thumb", encoded=False)
        self.assertEqual(kept.representation(), thumb.representation())
        self.assertEqual(bytes([10, 20, 30, 40]), bytes(tgt_db.blob(thumb)))

        # the orphan is gone; only the referenced blob survives
        surviving = {b.representation() for b in tgt_db.blob_ids()}
        self.assertEqual({kept.representation()}, surviving)
        self.assertNotIn(orphan.representation(), surviving)

    def test_shared_blob_copied_once(self):
        # copy-on-reference dedups: two documents referencing the SAME blob copy it once.
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        item = defs.create_concept(NS, "Item")
        doc_t = struct(defs, "Doc", [("thumb", T.BLOB_ID)])
        defs.create_attachment(NS, "Items", item, doc_t)
        src_db.extend_definitions(defs.const())
        att = src_db.definitions().attachments()[0]
        src_db.begin_transaction()
        shared = src_db.create_blob(V.BlobLayout("uchar", 1), V.ValueBlob(bytes([1, 2, 3])))
        for u in ("11111111-1111-1111-1111-111111111111",
                  "22222222-2222-2222-2222-222222222222"):
            src_db.set(att, att.create_key(V.ValueUUId(u)), V.ValueStructure(doc_t, {"thumb": shared}))
        src_db.commit()

        tgt_db, info = migrate(src_db, TransformationDirectives())
        self.assertEqual(2, info["documents"])
        self.assertEqual(1, info["blobs"])                 # the shared blob streamed once
        self.assertEqual(1, len(tgt_db.blob_ids()))

    def test_drop_record_skips_document(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        concept = defs.create_concept(NS, "R")
        doc_t = struct(defs, "Rec", [("x", V.TypeOptional(T.INT32))])
        defs.create_attachment(NS, "Recs", concept, doc_t)
        src_db.extend_definitions(defs.const())

        att = src_db.definitions().attachments()[0]
        src_db.begin_transaction()
        k1 = att.create_key(V.ValueUUId("44444444-4444-4444-4444-444444444444"))
        k2 = att.create_key(V.ValueUUId("55555555-5555-5555-5555-555555555555"))
        ot = V.TypeOptional(T.INT32)
        src_db.set(att, k1, V.ValueStructure(doc_t, {"x": V.ValueOptional(ot, 9)}))
        src_db.set(att, k2, V.ValueStructure(doc_t, {"x": V.ValueOptional(ot)}))   # nil -> dropped
        src_db.commit()

        directives = TransformationDirectives()
        directives.retype_field(doc_t.representation(), "x", T.INT32, policy="drop-record")
        directives.accept_document_drops()             # explicit sign-off: drops may delete docs
        tgt_db, info = migrate(src_db, directives)

        self.assertEqual(1, info["documents"])
        self.assertEqual(1, info["dropped"])
        tgt_att = tgt_db.definitions().attachments()[0]
        self.assertEqual(1, tgt_db.keys(tgt_att).size())


class TestRunMigrationOnDisk(unittest.TestCase):
    def test_real_file_migration_self_verified(self):
        tmp = tempfile.mkdtemp()
        src_path, tgt_path = os.path.join(tmp, "src.db"), os.path.join(tmp, "tgt.db")
        try:
            src = V.Database.create(src_path)
            defs = V.Definitions()
            item = defs.create_concept(NS, "Item")
            doc_t = struct(defs, "Doc", [("name", T.STRING),
                                         ("thumb", T.BLOB_ID), ("old", T.BLOB_ID)])
            defs.create_attachment(NS, "Items", item, doc_t)
            src.extend_definitions(defs.const())
            layout = V.BlobLayout("uchar", 1)
            src.begin_transaction()
            kept = src.create_blob(layout, V.ValueBlob(bytes([7, 7, 7, 7])))
            orphan = src.create_blob(layout, V.ValueBlob(bytes(range(20))))
            att = src.definitions().attachments()[0]
            key = att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
            src.set(att, key, V.ValueStructure(doc_t, {"name": "hi", "thumb": kept, "old": orphan}))
            src.commit()
            src.close()

            def build(defs):
                st = defs.structures()[0].representation()
                d = TransformationDirectives()
                d.rename_field(st, "name", "title")
                d.drop_field(st, "old")
                return d

            info = migrate_database.run(src_path, build, tgt_path, verify=True)
            self.assertEqual(1, info["documents"])
            self.assertEqual(1, info["blobs"])             # only the referenced blob copied
            self.assertEqual({"checked": 1, "dropped": 0, "referenced_blobs": 1},
                             info["verification"])

            tgt = V.Database.open(tgt_path, readonly=True)
            tatt = tgt.definitions().attachments()[0]
            doc = V.ValueStructure.cast(
                tgt.get(tatt, tgt.keys(tatt).at(0, encoded=False)).unwrap(encoded=False))
            self.assertEqual("hi", doc.at("title", encoded=False))
            self.assertEqual(bytes([7, 7, 7, 7]), bytes(tgt.blob(doc.at("thumb", encoded=False))))
            self.assertEqual(1, len(tgt.blob_ids()))
            tgt.close()
        finally:
            for p in (src_path, tgt_path):
                if os.path.exists(p):
                    os.remove(p)
            os.rmdir(tmp)


class TestDropRecordPosition(unittest.TestCase):
    """`drop-record` is record-scoped: on a Database its record is the document. It is
    admissible at document scope (a field reached through structs/optionals — multiplicity
    1), but UNDER a container (vector/set/map/xarray) 'drop the record' is ambiguous (one
    element among many), so the Database refuses it up front — symmetric to the
    CommitDatabase's blanket refusal."""

    def _enum(self, defs):
        ed = V.TypeEnumerationDescriptor("Mode")
        ed.add_case("Old"); ed.add_case("New")
        return defs.create_enumeration(NS, ed)

    def test_refuses_removed_case_drop_record_inside_vector(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        c = defs.create_concept(NS, "C")
        e = self._enum(defs)
        doc_t = struct(defs, "Doc", [("modes", V.TypeVector(e))])   # enum under a container
        defs.create_attachment(NS, "Docs", c, doc_t)
        src_db.extend_definitions(defs.const())

        d = TransformationDirectives()
        d.remove_case(e.representation(), "Old", "drop-record")
        rewriter, _ = DefinitionsRewriter.from_directives(src_db.definitions(), d)
        with self.assertRaises(ValueError) as cm:
            migrate_database.migrate(src_db, rewriter, V.Database.create_in_memory())
        self.assertIn("drop-record", str(cm.exception))
        self.assertIn("ambiguous", str(cm.exception))

    def test_refuses_retype_drop_record_when_struct_nested_in_map(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        c = defs.create_concept(NS, "C")
        inner = struct(defs, "Inner", [("x", V.TypeOptional(T.INT32))])
        doc_t = struct(defs, "Doc", [("m", V.TypeMap(T.STRING, inner))])   # struct under a container
        defs.create_attachment(NS, "Docs", c, doc_t)
        src_db.extend_definitions(defs.const())

        d = TransformationDirectives()
        d.retype_field(inner.representation(), "x", T.INT32, policy="drop-record")
        rewriter, _ = DefinitionsRewriter.from_directives(src_db.definitions(), d)
        with self.assertRaises(ValueError) as cm:
            migrate_database.dry_run(src_db, rewriter)     # dry_run enforces the same scope
        self.assertIn("Inner.x", str(cm.exception))

    def test_admits_drop_record_through_nested_struct_multiplicity_one(self):
        # Doc -> Inner -> x, all multiplicity 1: dropping the one document is unambiguous
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        c = defs.create_concept(NS, "C")
        inner = struct(defs, "Inner", [("x", V.TypeOptional(T.INT32))])
        doc_t = struct(defs, "Doc", [("inner", inner)])                    # nested struct, not a container
        defs.create_attachment(NS, "Docs", c, doc_t)
        src_db.extend_definitions(defs.const())
        att = src_db.definitions().attachments()[0]
        ot = V.TypeOptional(T.INT32)
        src_db.begin_transaction()
        src_db.set(att, att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111")),
                   V.ValueStructure(doc_t, {"inner": V.ValueStructure(inner, {"x": V.ValueOptional(ot, 9)})}))
        src_db.set(att, att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222")),
                   V.ValueStructure(doc_t, {"inner": V.ValueStructure(inner, {"x": V.ValueOptional(ot)})}))
        src_db.commit()

        d = TransformationDirectives()
        d.retype_field(inner.representation(), "x", T.INT32, policy="drop-record")
        rewriter, _ = DefinitionsRewriter.from_directives(src_db.definitions(), d)
        info = migrate_database.dry_run(src_db, rewriter)                  # NOT refused
        self.assertEqual(1, info["documents"])
        self.assertEqual(1, info["dropped"])                              # the nil-inner doc drops


class TestDocumentDropAcknowledgment(unittest.TestCase):
    """Dropping a whole document is record-scoped loss — categorically graver than a
    value-closed policy — so `migrate` refuses `drop-record` until the migration explicitly
    signs off (`accept_document_drops()`). `dry_run` never requires it: it is the tool that
    informs that decision."""

    def _src_with_droppable_doc(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        concept = defs.create_concept(NS, "R")
        doc_t = struct(defs, "Rec", [("x", V.TypeOptional(T.INT32))])   # direct field: coherent
        defs.create_attachment(NS, "Recs", concept, doc_t)
        src_db.extend_definitions(defs.const())
        att = src_db.definitions().attachments()[0]
        ot = V.TypeOptional(T.INT32)
        src_db.begin_transaction()
        src_db.set(att, att.create_key(V.ValueUUId("44444444-4444-4444-4444-444444444444")),
                   V.ValueStructure(doc_t, {"x": V.ValueOptional(ot, 9)}))
        src_db.set(att, att.create_key(V.ValueUUId("55555555-5555-5555-5555-555555555555")),
                   V.ValueStructure(doc_t, {"x": V.ValueOptional(ot)}))          # nil -> would drop
        src_db.commit()
        return src_db, doc_t

    def test_migrate_refuses_unacknowledged_drop_record(self):
        src_db, doc_t = self._src_with_droppable_doc()
        d = TransformationDirectives()
        d.retype_field(doc_t.representation(), "x", T.INT32, policy="drop-record")   # NO sign-off
        rewriter, _ = DefinitionsRewriter.from_directives(src_db.definitions(), d)
        with self.assertRaises(ValueError) as cm:
            migrate_database.migrate(src_db, rewriter, V.Database.create_in_memory())
        self.assertIn("unacknowledged", str(cm.exception))
        self.assertIn("accept_document_drops", str(cm.exception))

    def test_dry_run_does_not_require_acknowledgment(self):
        # the informing step: dry_run runs and reports the drop WITHOUT a sign-off
        src_db, doc_t = self._src_with_droppable_doc()
        d = TransformationDirectives()
        d.retype_field(doc_t.representation(), "x", T.INT32, policy="drop-record")   # NO sign-off
        rewriter, _ = DefinitionsRewriter.from_directives(src_db.definitions(), d)
        info = migrate_database.dry_run(src_db, rewriter)                            # not refused
        self.assertEqual(1, info["dropped"])

    def test_migrate_proceeds_once_acknowledged(self):
        src_db, doc_t = self._src_with_droppable_doc()
        d = TransformationDirectives()
        d.retype_field(doc_t.representation(), "x", T.INT32, policy="drop-record")
        d.accept_document_drops()                                                   # the explicit act
        rewriter, target_defs = DefinitionsRewriter.from_directives(src_db.definitions(), d)
        tgt = V.Database.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        info = migrate_database.migrate(src_db, rewriter, tgt)
        self.assertEqual(1, info["documents"])
        self.assertEqual(1, info["dropped"])


class TestDryRun(unittest.TestCase):
    """Exercise the rewriter over the documents WITHOUT the write-side machinery — the
    dividend of the I/O-free engine: a preview at the cost of one read-only pass."""

    def test_previews_documents_and_orphans_without_writing(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        item = defs.create_concept(NS, "Item")
        doc_t = struct(defs, "Doc", [("name", T.STRING), ("thumb", T.BLOB_ID), ("old", T.BLOB_ID)])
        defs.create_attachment(NS, "Items", item, doc_t)
        src_db.extend_definitions(defs.const())
        layout = V.BlobLayout("uchar", 1)
        src_db.begin_transaction()
        kept = src_db.create_blob(layout, V.ValueBlob(bytes([1, 2, 3])))
        orphan = src_db.create_blob(layout, V.ValueBlob(bytes([9, 9])))
        att = src_db.definitions().attachments()[0]
        key = att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        src_db.set(att, key, V.ValueStructure(doc_t, {"name": "x", "thumb": kept, "old": orphan}))
        src_db.commit()

        directives = TransformationDirectives()
        directives.rename_field(doc_t.representation(), "name", "title")
        directives.drop_field(doc_t.representation(), "old")            # orphans the 2nd blob
        rewriter, _ = DefinitionsRewriter.from_directives(src_db.definitions(), directives)

        info = migrate_database.dry_run(src_db, rewriter)
        self.assertEqual({"documents": 1, "dropped": 0, "referenced_blobs": 1, "orphans": 1},
                         {k: info[k] for k in ("documents", "dropped", "referenced_blobs", "orphans")})
        self.assertEqual([], info["diagnostics"]["sites"])   # no Class-B policy fired
        # nothing written: the source still holds BOTH blobs, untouched
        self.assertEqual(2, len(src_db.blob_ids()))

    def test_previews_dropped_records(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        concept = defs.create_concept(NS, "R")
        doc_t = struct(defs, "Rec", [("x", V.TypeOptional(T.INT32))])
        defs.create_attachment(NS, "Recs", concept, doc_t)
        src_db.extend_definitions(defs.const())
        att = src_db.definitions().attachments()[0]
        ot = V.TypeOptional(T.INT32)
        src_db.begin_transaction()
        src_db.set(att, att.create_key(V.ValueUUId("44444444-4444-4444-4444-444444444444")),
                   V.ValueStructure(doc_t, {"x": V.ValueOptional(ot, 9)}))
        src_db.set(att, att.create_key(V.ValueUUId("55555555-5555-5555-5555-555555555555")),
                   V.ValueStructure(doc_t, {"x": V.ValueOptional(ot)}))   # nil -> would drop
        src_db.commit()

        directives = TransformationDirectives()
        directives.retype_field(doc_t.representation(), "x", T.INT32, policy="drop-record")
        rewriter, _ = DefinitionsRewriter.from_directives(src_db.definitions(), directives)

        info = migrate_database.dry_run(src_db, rewriter)
        self.assertEqual(1, info["documents"])
        self.assertEqual(1, info["dropped"])


class TestNonLocalHook(unittest.TestCase):
    """A Class-C derive hook that reads *another* source document (single reference).

    Orders carry a `custRef : key<Customer>`; the migration adds `Order.customerName`,
    derived by dereferencing that key in the Customers attachment through the source view
    the store loop wires (`ctx.attachment_getting`). The view is over `Base(A)` — the
    immutable source — so the read is well-defined regardless of migration order."""

    def _schema(self):
        defs = V.Definitions()
        customer = defs.create_concept(NS, "Customer")
        cust_doc = struct(defs, "CustomerDoc", [("name", T.STRING)])
        custs_att = defs.create_attachment(NS, "Customers", customer, cust_doc)
        order = defs.create_concept(NS, "Order")
        order_doc = struct(defs, "OrderDoc",
                           [("custRef", custs_att.type_key()), ("qty", T.INT32)])
        orders_att = defs.create_attachment(NS, "Orders", order, order_doc)
        return defs, cust_doc, custs_att, order_doc, orders_att

    def _directives(self, order_doc, custs_att):
        def derive_name(source_struct, field_name, target_type, ctx):
            key = source_struct.at("custRef", encoded=False)
            cust = ctx.attachment_getting.get(custs_att, key)         # ValueOptional
            name = V.ValueStructure.cast(cust.unwrap(encoded=False)).at("name", encoded=False)
            return V.ValueString(name)

        d = TransformationDirectives()
        d.add_field(order_doc.representation(), "customerName", T.STRING, derive=derive_name)
        return d

    def test_derive_dereferences_via_source_view(self):
        src_db = V.Database.create_in_memory()
        defs, cust_doc, custs_att, order_doc, orders_att = self._schema()
        src_db.extend_definitions(defs.const())

        ck = custs_att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        ok = orders_att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        src_db.begin_transaction()
        src_db.set(custs_att, ck, V.ValueStructure(cust_doc, {"name": "Ada"}))
        src_db.set(orders_att, ok, V.ValueStructure(order_doc, {"custRef": ck, "qty": 7}))
        src_db.commit()

        tgt_db, info = migrate(src_db, self._directives(order_doc, custs_att))
        self.assertEqual(2, info["documents"])

        tgt_orders = next(a for a in tgt_db.definitions().attachments()
                          if a.representation().endswith("Orders"))
        keys = tgt_db.keys(tgt_orders)
        doc = V.ValueStructure.cast(
            tgt_db.get(tgt_orders, keys.at(0, encoded=False)).unwrap(encoded=False))
        self.assertEqual("Ada", doc.at("customerName", encoded=False))
        self.assertEqual(7, doc.at("qty", encoded=False))

    def test_no_source_view_wired_raises_clearly(self):
        # Run the engine directly (no store loop) so no source view is wired: a non-local
        # hook must fail closed with a clear, actionable message — never silently.
        defs, cust_doc, custs_att, order_doc, orders_att = self._schema()
        rewriter, _ = DefinitionsRewriter.from_directives(
            defs, self._directives(order_doc, custs_att))
        ck = custs_att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        src_order = V.ValueStructure(order_doc, {"custRef": ck, "qty": 7})
        with self.assertRaises(ValueError) as cm:
            rewriter.value(src_order)
        self.assertIn("no source view", str(cm.exception))

    def test_ctx_reports_view_and_reentrant_rewrite(self):
        # A hook sees `ctx.has_source_view` True under the store loop and can re-enter the
        # engine on a fetched value via `ctx.rewrite(...)`. With follow_refs=False the
        # nested call runs with the source view blanked (cycle-breaking): a hook firing
        # inside it would see no view — asserted here by round-tripping a leaf.
        seen = {}
        src_db = V.Database.create_in_memory()
        defs, cust_doc, custs_att, order_doc, orders_att = self._schema()
        src_db.extend_definitions(defs.const())
        ck = custs_att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        ok = orders_att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        src_db.begin_transaction()
        src_db.set(custs_att, ck, V.ValueStructure(cust_doc, {"name": "Ada"}))
        src_db.set(orders_att, ok, V.ValueStructure(order_doc, {"custRef": ck, "qty": 7}))
        src_db.commit()

        def derive_name(source_struct, field_name, target_type, ctx):
            seen["view"] = ctx.has_source_view
            key = source_struct.at("custRef", encoded=False)
            cust = ctx.attachment_getting.get(custs_att, key)
            name = V.ValueStructure.cast(cust.unwrap(encoded=False)).at("name", encoded=False)
            # re-enter the engine on the leaf; follow_refs=False blanks the view inside
            return ctx.rewrite(V.ValueString(name), T.STRING, follow_refs=False)

        d = TransformationDirectives()
        d.add_field(order_doc.representation(), "customerName", T.STRING, derive=derive_name)
        rewriter, tgt_defs = DefinitionsRewriter.from_directives(src_db.definitions(), d)
        tgt_db = V.Database.create_in_memory()
        tgt_db.extend_definitions(tgt_defs.const())
        migrate_database.migrate(src_db, rewriter, tgt_db)

        self.assertTrue(seen["view"])
        tgt_orders = next(a for a in tgt_db.definitions().attachments()
                          if a.representation().endswith("Orders"))
        doc = V.ValueStructure.cast(tgt_db.get(
            tgt_orders, tgt_db.keys(tgt_orders).at(0, encoded=False)).unwrap(encoded=False))
        self.assertEqual("Ada", doc.at("customerName", encoded=False))


class TestAggregateHook(unittest.TestCase):
    """Aggregate Class-C — a value folded over a *collection* of other source documents,
    selected by an *incoming* reference. `Customer.totalSpent = sum(order.amount where
    order.custRef == me)`. Two pieces the store loop supplies: `ctx.attachment_getting`
    (scan/fold the whole Orders attachment over the immutable Base(A)) and `ctx.self_key`
    (the record's own identity — the `me` an incoming-reference fold needs, which the
    customer document value does not carry). The hook memoises the index in a closure so
    the scan runs once, not once per customer."""

    def _schema(self, defs):
        customer = defs.create_concept(NS, "Customer")
        cust_doc = struct(defs, "CustomerDoc", [("name", T.STRING)])
        custs_att = defs.create_attachment(NS, "Customers", customer, cust_doc)
        order = defs.create_concept(NS, "Order")
        order_doc = struct(defs, "OrderDoc",
                           [("custRef", custs_att.type_key()), ("amount", T.INT32)])
        orders_att = defs.create_attachment(NS, "Orders", order, order_doc)
        return cust_doc, custs_att, order_doc, orders_att

    def test_incoming_reference_fold_over_the_source(self):
        src_db = V.Database.create_in_memory()
        defs = V.Definitions()
        cust_doc, custs_att, order_doc, orders_att = self._schema(defs)
        src_db.extend_definitions(defs.const())

        ada = custs_att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        bob = custs_att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        src_db.begin_transaction()
        src_db.set(custs_att, ada, V.ValueStructure(cust_doc, {"name": "Ada"}))
        src_db.set(custs_att, bob, V.ValueStructure(cust_doc, {"name": "Bob"}))
        for i, (who, amt) in enumerate([(ada, 10), (ada, 20), (ada, 30), (bob, 99)]):
            k = orders_att.create_key(V.ValueUUId(f"aaaa0000-0000-0000-0000-00000000000{i}"))
            src_db.set(orders_att, k, V.ValueStructure(order_doc, {"custRef": who, "amount": amt}))
        src_db.commit()

        index = {}
        scans = []

        def total_spent(source_struct, field_name, target_type, ctx):
            if not index:
                ag = ctx.attachment_getting
                ks = ag.keys(orders_att)
                for i in range(ks.size()):
                    o = V.ValueStructure.cast(
                        ag.get(orders_att, ks.at(i, encoded=False)).unwrap(encoded=False))
                    ref = V.Value.dumps(o.at("custRef", encoded=False))
                    index[ref] = index.get(ref, 0) + V.Value.dumps(o.at("amount", encoded=False))
                scans.append(1)
            return V.ValueInt32(index.get(V.Value.dumps(ctx.self_key), 0))

        d = TransformationDirectives()
        d.add_field(cust_doc.representation(), "totalSpent", T.INT32, derive=total_spent)
        tgt_db, _ = migrate(src_db, d)

        self.assertEqual(1, len(scans))                    # scanned once, memoised
        tgt_custs = next(a for a in tgt_db.definitions().attachments()
                         if a.representation().endswith("Customers"))
        totals = {}
        keys = tgt_db.keys(tgt_custs)
        for i in range(keys.size()):
            doc = V.ValueStructure.cast(
                tgt_db.get(tgt_custs, keys.at(i, encoded=False)).unwrap(encoded=False))
            totals[V.Value.dumps(doc.at("name", encoded=False))] = \
                V.Value.dumps(doc.at("totalSpent", encoded=False))
        self.assertEqual({"Ada": 60, "Bob": 99}, totals)

    def test_self_key_absent_outside_store_loop_raises(self):
        # Run the engine directly: no store loop → no self key. A hook that reads it must
        # fail closed with a clear message (guardable via ctx.has_self_key).
        defs = V.Definitions()
        cust_doc, custs_att, order_doc, orders_att = self._schema(defs)

        def needs_self(source_struct, field_name, target_type, ctx):
            self.assertFalse(ctx.has_self_key)
            return V.ValueInt32(ctx.self_key.instance_id().representation() and 0)

        d = TransformationDirectives()
        d.add_field(cust_doc.representation(), "totalSpent", T.INT32, derive=needs_self)
        rewriter, _ = DefinitionsRewriter.from_directives(defs, d)
        with self.assertRaises(ValueError) as cm:
            rewriter.value(V.ValueStructure(cust_doc, {"name": "Ada"}))
        self.assertIn("no self key", str(cm.exception))


class TestDropAttachment(unittest.TestCase):
    """drop_attachment removes an attachment and DELETES its documents — a persistence-layer
    mass deletion, so it demands `accept_attachment_drops()` (like drop-record's sign-off).
    A surviving attachment is untouched; nothing references an attachment, so no dangling."""

    def _seed(self):
        src = V.Database.create_in_memory()
        defs = V.Definitions()
        cust = defs.create_concept(NS, "Customer")
        keep = struct(defs, "Order", [("qty", T.INT32)])
        legacy = struct(defs, "Audit", [("note", T.STRING)])
        defs.create_attachment(NS, "Orders", cust, keep)
        defs.create_attachment(NS, "Audits", cust, legacy)
        src.extend_definitions(defs.const())
        atts = {a.identifier().split(".")[-1]: a for a in src.definitions().attachments()}
        src.begin_transaction()
        src.set(atts["Orders"], atts["Orders"].create_key(
            V.ValueUUId("11111111-1111-1111-1111-111111111111")),
            V.ValueStructure(keep, {"qty": 5}))
        src.set(atts["Audits"], atts["Audits"].create_key(
            V.ValueUUId("22222222-2222-2222-2222-222222222222")),
            V.ValueStructure(legacy, {"note": "x"}))
        src.commit()
        return src, keep

    def test_refused_without_acknowledgement(self):
        src, _ = self._seed()
        d = TransformationDirectives(); d.drop_attachment("Audits")
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.Database.create_in_memory(); tgt.extend_definitions(target_defs.const())
        with self.assertRaises(ValueError) as cm:
            migrate_database.migrate(src, rewriter, tgt)
        self.assertIn("unacknowledged", str(cm.exception))

    def test_acknowledged_drops_attachment_and_its_documents(self):
        src, _ = self._seed()
        d = TransformationDirectives()
        d.drop_attachment("Audits")
        d.accept_attachment_drops()
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.Database.create_in_memory(); tgt.extend_definitions(target_defs.const())
        info = migrate_database.migrate(src, rewriter, tgt)

        locals_ = {a.identifier().split(".")[-1] for a in tgt.definitions().attachments()}
        self.assertEqual({"Orders"}, locals_)              # Audits gone, Orders kept
        self.assertEqual(1, info["documents"])             # only the surviving Order copied

    def test_dry_run_informs_without_acknowledgement(self):
        src, _ = self._seed()
        d = TransformationDirectives(); d.drop_attachment("Audits")   # no accept_*
        rewriter, _ = DefinitionsRewriter.from_directives(src.definitions(), d)
        info = migrate_database.dry_run(src, rewriter)     # must NOT require the sign-off
        self.assertEqual(1, info["documents"])             # the Audit doc is not carried


class TestProgress(unittest.TestCase):
    """`on_progress` reports a `MigrationProgress` as work advances — the byte bar (the
    dominant cost) climbing per streamed chunk against the source's total blob bytes, the
    document tally, and the attachment position."""

    def test_on_progress_reports_bytes_documents_and_position(self):
        src = V.Database.create_in_memory()
        defs = V.Definitions()
        item = defs.create_concept(NS, "Item")
        doc_t = struct(defs, "Doc", [("name", T.STRING), ("thumb", T.BLOB_ID)])
        defs.create_attachment(NS, "Items", item, doc_t)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        src.begin_transaction()
        total = 0
        for i, u in enumerate(("11111111-1111-1111-1111-111111111111",
                               "22222222-2222-2222-2222-222222222222")):
            payload = bytes(range((i + 1) * 40))
            total += len(payload)
            b = src.create_blob(V.BlobLayout("uchar", 1), V.ValueBlob(payload))
            src.set(att, att.create_key(V.ValueUUId(u)), V.ValueStructure(doc_t, {"name": "x", "thumb": b}))
        src.commit()

        events = []
        d = TransformationDirectives(); d.rename_field(doc_t.representation(), "name", "title")
        rw, tdefs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.Database.create_in_memory(); tgt.extend_definitions(tdefs.const())
        migrate_database.migrate(src, rw, tgt, on_progress=events.append)

        self.assertTrue(events)
        last = events[-1]
        self.assertEqual(total, last.bytes_total)                  # denominator = source total blob bytes
        self.assertEqual(total, last.bytes_copied)                 # bar reaches the total
        self.assertEqual(2, last.documents)
        self.assertEqual(2, last.blobs)
        self.assertEqual((0, 1), (last.attachment_index, last.attachment_count))
        self.assertEqual("Items", last.attachment)
        # bytes_copied is monotonic non-decreasing (per-chunk climb)
        seq = [e.bytes_copied for e in events]
        self.assertEqual(seq, sorted(seq))

    def test_no_callback_is_a_noop(self):
        src = V.Database.create_in_memory()
        defs = V.Definitions()
        c = defs.create_concept(NS, "C")
        doc_t = struct(defs, "Doc", [("n", T.INT32)])
        defs.create_attachment(NS, "Docs", c, doc_t)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        src.begin_transaction()
        src.set(att, att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111")),
                V.ValueStructure(doc_t, {"n": 1}))
        src.commit()
        tgt, info = migrate(src, TransformationDirectives())       # no on_progress
        self.assertEqual(1, info["documents"])


class TestSourceSnapshot(unittest.TestCase):
    """The source is opened read-only but is not immutable (a concurrent process may
    del/del_blob), so migrate/dry_run hold ONE read transaction over the source for the whole
    pass — a consistent snapshot. Proven here by the lifecycle: the source is in a transaction
    *during* the pass (observed from inside a hook) and released *after*."""

    def _seed(self):
        src = V.Database.create_in_memory()
        defs = V.Definitions()
        c = defs.create_concept(NS, "C")
        doc = struct(defs, "Doc", [("n", T.INT32)])
        defs.create_attachment(NS, "Docs", c, doc)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        src.begin_transaction()
        src.set(att, att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111")),
                V.ValueStructure(doc, {"n": 1}))
        src.commit()
        return src

    def test_source_snapshot_held_during_pass_released_after(self):
        src = self._seed()
        seen = {}

        def probe(source_struct, field_name, target_type, ctx):
            seen["during"] = src.in_transaction()      # snapshot held while the pass reads
            return V.ValueInt32(0)

        d = TransformationDirectives(); d.add_field("Demo::Doc", "tag", T.INT32, derive=probe)
        rw, tdefs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.Database.create_in_memory(); tgt.extend_definitions(tdefs.const())
        migrate_database.migrate(src, rw, tgt)
        self.assertTrue(seen["during"])                # in a read transaction during the migration
        self.assertFalse(src.in_transaction())         # released afterward (nothing was written)

    def test_snapshot_released_even_on_failure(self):
        src = self._seed()

        def boom(source_struct, field_name, target_type, ctx):
            raise RuntimeError("boom")

        d = TransformationDirectives(); d.add_field("Demo::Doc", "tag", T.INT32, derive=boom)
        rw, tdefs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.Database.create_in_memory(); tgt.extend_definitions(tdefs.const())
        with self.assertRaises(RuntimeError):
            migrate_database.migrate(src, rw, tgt)
        self.assertFalse(src.in_transaction())         # source snapshot released on failure too


class TestMigrationFailureSafety(unittest.TestCase):
    """A migration is all-or-nothing. A mid-migration failure (here: a hook that raises on
    the 3rd document) must abort the exclusive transaction — no half-written documents left —
    and `run` must discard the partial target file, never leave a corrupt artefact behind."""

    _UUIDS = ("11111111-1111-1111-1111-111111111111",
              "22222222-2222-2222-2222-222222222222",
              "33333333-3333-3333-3333-333333333333")

    def _seed(self, db):
        defs = V.Definitions()
        c = defs.create_concept(NS, "C")
        doc = struct(defs, "Doc", [("n", T.INT32)])
        defs.create_attachment(NS, "Docs", c, doc)
        db.extend_definitions(defs.const())
        att = db.definitions().attachments()[0]
        db.begin_transaction()
        for u in self._UUIDS:
            db.set(att, att.create_key(V.ValueUUId(u)), V.ValueStructure(doc, {"n": 1}))
        db.commit()

    @staticmethod
    def _boom_directives(source_defs):
        calls = [0]
        def boom(source_struct, field_name, target_type, ctx):
            calls[0] += 1
            if calls[0] == 3:
                raise RuntimeError("boom mid-migration")
            return V.ValueInt32(calls[0])
        d = TransformationDirectives()
        d.add_field("Demo::Doc", "tag", T.INT32, derive=boom)
        return d

    def test_migrate_rolls_back_on_failure(self):
        src = V.Database.create_in_memory(); self._seed(src)
        rw, tdefs = DefinitionsRewriter.from_directives(
            src.definitions(), self._boom_directives(src.definitions()))
        tgt = V.Database.create_in_memory(); tgt.extend_definitions(tdefs.const())
        with self.assertRaises(RuntimeError):
            migrate_database.migrate(src, rw, tgt)
        self.assertFalse(tgt.in_transaction())             # exclusive transaction aborted
        ta = tgt.definitions().attachments()[0]
        self.assertEqual(0, tgt.keys(ta).size())           # no half-written documents survive

    def test_run_discards_partial_target_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp, tp = os.path.join(tmp, "src.db"), os.path.join(tmp, "tgt.db")
            src = V.Database.create(sp); self._seed(src); src.close()
            with self.assertRaises(RuntimeError):
                migrate_database.run(sp, self._boom_directives, tp)
            self.assertFalse(os.path.exists(tp))           # partial target discarded


if __name__ == "__main__":
    unittest.main()
