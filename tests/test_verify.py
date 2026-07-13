"""Round-trip verifier — it must PASS on a faithful migration and FAIL loudly on
any divergence (value drift, dangling blob, a dropped record left behind)."""

import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsTransformer, migrate_database,
    verify_migration, VerificationError)

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
        transformer, target_defs = DefinitionsTransformer.from_directives(src.definitions(), directives)
        tgt = V.Database.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        migrate_database(src, transformer, tgt)
        return transformer, tgt

    def test_passes_on_faithful_rename_and_blob(self):
        src, doc_t = _source_with_blob()
        d = TransformationDirectives()
        d.rename_field(doc_t.representation(), "name", "title")
        d.drop_field(doc_t.representation(), "old")
        transformer, tgt = self._migrate(src, d)
        info = verify_migration(src, transformer, tgt)
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
        transformer, tgt = self._migrate(src, d)
        info = verify_migration(src, transformer, tgt)
        self.assertEqual(1, info["checked"])
        self.assertEqual(1, info["dropped"])

    def test_fails_on_value_drift(self):
        src, doc_t = _source_with_blob()
        d = TransformationDirectives()
        d.rename_field(doc_t.representation(), "name", "title")
        d.drop_field(doc_t.representation(), "old")
        transformer, tgt = self._migrate(src, d)
        # tamper with the target document after a faithful migration
        tgt_att = tgt.definitions().attachments()[0]
        tk = tgt.keys(tgt_att).at(0, encoded=False)
        tdoc = V.ValueStructure.cast(tgt.get(tgt_att, tk).unwrap(encoded=False))
        tampered = V.ValueStructure(tdoc.type_structure(),
                                    {"title": "DRIFTED", "thumb": tdoc.at("thumb", encoded=False)})
        tgt.begin_transaction(); tgt.set(tgt_att, tk, tampered); tgt.commit()
        with self.assertRaises(VerificationError) as cm:
            verify_migration(src, transformer, tgt)
        self.assertIn("value mismatch", str(cm.exception))

    def test_fails_on_dangling_blob(self):
        src, doc_t = _source_with_blob()
        d = TransformationDirectives()
        d.rename_field(doc_t.representation(), "name", "title")
        d.drop_field(doc_t.representation(), "old")
        transformer, tgt = self._migrate(src, d)
        # delete the referenced blob out from under the document
        tgt_att = tgt.definitions().attachments()[0]
        thumb = V.ValueStructure.cast(
            tgt.get(tgt_att, tgt.keys(tgt_att).at(0, encoded=False)).unwrap(encoded=False)
        ).at("thumb", encoded=False)
        tgt.begin_transaction(); tgt.del_blob(thumb); tgt.commit()
        with self.assertRaises(VerificationError) as cm:
            verify_migration(src, transformer, tgt)
        self.assertIn("blob", str(cm.exception))

    def test_fails_on_spurious_document(self):
        src, doc_t = _source_with_blob()
        d = TransformationDirectives()
        d.rename_field(doc_t.representation(), "name", "title")
        d.drop_field(doc_t.representation(), "old")
        transformer, tgt = self._migrate(src, d)
        tgt_att = tgt.definitions().attachments()[0]
        extra = tgt_att.create_key(V.ValueUUId("66666666-6666-6666-6666-666666666666"))
        existing = V.ValueStructure.cast(
            tgt.get(tgt_att, tgt.keys(tgt_att).at(0, encoded=False)).unwrap(encoded=False))
        tgt.begin_transaction(); tgt.set(tgt_att, extra, existing); tgt.commit()
        with self.assertRaises(VerificationError) as cm:
            verify_migration(src, transformer, tgt)
        self.assertIn("documents", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
