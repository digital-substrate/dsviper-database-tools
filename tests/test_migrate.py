"""Migration-loop integration test — a real read-old / write-new over in-memory
databases, including blob byte-copy and orphan mark-sweep."""

import os
import tempfile
import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsTransformer, migrate_database, run_migration)

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


def migrate(src_db, directives):
    transformer, target_defs = DefinitionsTransformer.from_directives(
        src_db.definitions(), directives)
    tgt_db = V.Database.create_in_memory()
    tgt_db.extend_definitions(target_defs.const())
    info = migrate_database(src_db, transformer, tgt_db)     # owns its transaction
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

    def test_blob_bytes_copied_and_orphan_swept(self):
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

        self.assertEqual({"documents": 1, "dropped": 0, "blobs": 2, "orphans_swept": 1}, info)

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

            info = run_migration(src_path, build, tgt_path, verify=True)
            self.assertEqual(1, info["documents"])
            self.assertEqual(1, info["orphans_swept"])
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


if __name__ == "__main__":
    unittest.main()
