"""CommitDatabase faithful-replay tests — history preserved + commutation per commit
(merges included), across the opcode verbs, path rename, and retype-at-path."""

import os
import tempfile
import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsTransformer, DropRecord,
    migrate_commit_database, run_commit_migration)

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


def snapshot(db, commit_id, transformer=None):
    """Materialise a state as {(attachment, instance): dumps(document)} — migrating each
    document through `transformer` first when given (the RHS of the commutation law)."""
    state = V.CommitStateBuilder.state(db, commit_id)
    ag = state.attachment_getting()
    snap = {}
    for att in state.definitions().attachments():
        keys = ag.keys(att)
        for i in range(keys.size()):
            key = keys.at(i, encoded=False)
            doc = ag.get(att, key)
            if doc.is_nil():
                continue
            val = doc.unwrap(encoded=False)
            if transformer is not None:
                try:
                    val = transformer.value(val)
                except DropRecord:
                    continue
                att_local = transformer.attachment(att).identifier().split(".")[-1]
                inst = transformer.value(key).instance_id().representation()
            else:
                att_local = att.identifier().split(".")[-1]
                inst = key.instance_id().representation()
            snap[(att_local, inst)] = V.Value.dumps(val)
    return snap


class CommitReplayCase(unittest.TestCase):
    def _prove(self, src, order, directives, commits):
        transformer, target_defs = DefinitionsTransformer.from_directives(src.definitions(), directives)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        info = migrate_commit_database(src, transformer, tgt)
        # history preserved
        self.assertEqual(len(src.commit_ids()), len(tgt.commit_ids()))
        self.assertEqual(len(src.commit_ids()), info["commits"])
        # commutation per commit
        for c in commits:
            lhs = snapshot(tgt, info["remap"][c.representation()])
            rhs = snapshot(src, c, transformer=transformer)
            self.assertEqual(rhs, lhs, f"commutation failed at {c.representation()[:8]}")
        return transformer, tgt, info


def order_db(qty_type=T.INT32):
    src = V.CommitDatabase.create_in_memory()
    defs = V.Definitions()
    customer = defs.create_concept(NS, "Customer")
    order = struct(defs, "Order", [("qty", qty_type), ("label", T.STRING),
                                   ("tags", V.TypeSet(T.STRING)),
                                   ("attrs", V.TypeMap(T.STRING, T.INT32)),
                                   ("lines", V.TypeXArray(T.INT32))])
    defs.create_attachment(NS, "Orders", customer, order)
    src.extend_definitions(defs.const())
    return src, order


def rename_qty(order):
    d = TransformationDirectives()
    d.rename_field(order.representation(), "qty", "count")
    return d


class TestLinearAndMerge(CommitReplayCase):
    def test_linear_set_update_rename(self):
        src, order = order_db()
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5, "label": "a"}))
        c1 = src.commit_mutations("set", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        cms1.attachment_mutating().update(att, k1, V.Path.from_field("qty").const(), 7)
        c2 = src.commit_mutations("update .qty", cms1)
        self._prove(src, order, rename_qty(order), [c1, c2])

    def test_merge_two_divergent_heads(self):
        src, order = order_db()
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        k2 = att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5, "label": "a"}))
        c0 = src.commit_mutations("base", cms0)
        cmsA = V.CommitMutableState(V.CommitStateBuilder.state(src, c0))
        cmsA.attachment_mutating().update(att, k1, V.Path.from_field("qty").const(), 7)
        cA = src.commit_mutations("A", cmsA)
        cmsB = V.CommitMutableState(V.CommitStateBuilder.state(src, c0))
        cmsB.attachment_mutating().set(att, k2, V.ValueStructure(order, {"qty": 9, "label": "b"}))
        cB = src.commit_mutations("B", cmsB)
        cM = src.merge_commit("merge A,B", cA, cB)
        self._prove(src, order, rename_qty(order), [c0, cA, cB, cM])


class TestRetypeAtPath(CommitReplayCase):
    def _run(self, new_type, values, policy, src_qty=T.INT32):
        src, order = order_db(src_qty)
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": values[0], "label": "a"}))
        c1 = src.commit_mutations("set", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        cms1.attachment_mutating().update(att, k1, V.Path.from_field("qty").const(), values[1])
        c2 = src.commit_mutations("update .qty", cms1)
        d = TransformationDirectives()
        d.retype_field(order.representation(), "qty", new_type, policy=policy)
        self._prove(src, order, d, [c1, c2])

    def test_widening_at_path(self):
        self._run(T.INT64, (5, 7), None)

    def test_narrowing_at_path_saturate_out_of_range(self):
        self._run(T.INT32, (100, 2**40), "saturate", src_qty=T.INT64)


class TestContainerVerbs(CommitReplayCase):
    def test_set_map_verbs_under_rename(self):
        src, order = order_db()
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        p = lambda f: V.Path.from_field(f).const()
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(
            order, {"qty": 1, "label": "a", "tags": ["a"], "attrs": {"x": 1}}))
        c1 = src.commit_mutations("set", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        am = cms1.attachment_mutating()
        am.union_in_set(att, k1, p("tags"), V.ValueSet(V.TypeSet(T.STRING), ["b", "c"]))
        am.subtract_in_set(att, k1, p("tags"), V.ValueSet(V.TypeSet(T.STRING), ["a"]))
        am.union_in_map(att, k1, p("attrs"), V.ValueMap(V.TypeMap(T.STRING, T.INT32), {"y": 2}))
        am.update_in_map(att, k1, p("attrs"), V.ValueMap(V.TypeMap(T.STRING, T.INT32), {"x": 9}))
        am.subtract_in_map(att, k1, p("attrs"), V.ValueSet(V.TypeSet(T.STRING), ["x"]))
        c2 = src.commit_mutations("set/map ops", cms1)
        self._prove(src, order, rename_qty(order), [c1, c2])

    def test_xarray_verbs_under_rename(self):
        src, order = order_db()
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        pl = V.Path.from_field("lines").const()
        p1 = V.ValueUUId("aaaaaaaa-0000-0000-0000-000000000001")
        p2 = V.ValueUUId("aaaaaaaa-0000-0000-0000-000000000002")
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 1, "label": "a"}))
        c1 = src.commit_mutations("set", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        am = cms1.attachment_mutating()
        am.insert_in_xarray(att, k1, pl, V.ValueXArray.END, p1, 10)
        am.insert_in_xarray(att, k1, pl, V.ValueXArray.END, p2, 20)
        am.update_in_xarray(att, k1, pl, p1, 11)
        am.remove_in_xarray(att, k1, pl, p2)
        c2 = src.commit_mutations("xarray ops", cms1)
        self._prove(src, order, rename_qty(order), [c1, c2])


class TestBlobsCarried(CommitReplayCase):
    def test_referenced_blob_carried(self):
        src = V.CommitDatabase.create_in_memory()
        blob = src.create_blob_from_buffer(b"receipt bytes")
        defs = V.Definitions()
        customer = defs.create_concept(NS, "Customer")
        order = struct(defs, "Order", [("qty", T.INT32), ("receipt", T.BLOB_ID)])
        defs.create_attachment(NS, "Orders", customer, order)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5, "receipt": blob}))
        c1 = src.commit_mutations("set", cms0)

        transformer, target_defs = DefinitionsTransformer.from_directives(
            src.definitions(), rename_qty(order))
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        info = migrate_commit_database(src, transformer, tgt)
        self.assertIn(blob, tgt.blob_ids())
        self.assertEqual(b"receipt bytes", bytes(tgt.blob(blob)))
        self.assertEqual(snapshot(src, c1, transformer),
                         snapshot(tgt, info["remap"][c1.representation()]))


class TestCommitIdRemap(unittest.TestCase):
    def test_intra_dag_remapped_external_kept(self):
        src = V.CommitDatabase.create_in_memory()
        defs = V.Definitions()
        concept = defs.create_concept(NS, "C")
        ref = struct(defs, "Ref", [("note", T.STRING), ("prev", T.COMMIT_ID)])
        defs.create_attachment(NS, "Refs", concept, ref)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        k2 = att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        external = V.ValueCommitId.try_parse("a" * 40)                # not a commit of this base

        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(ref, {"note": "a", "prev": external}))
        c1 = src.commit_mutations("c1", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        cms1.attachment_mutating().set(att, k2, V.ValueStructure(ref, {"note": "b", "prev": c1}))  # intra-DAG
        c2 = src.commit_mutations("c2", cms1)

        d = TransformationDirectives()
        d.rename_field(ref.representation(), "note", "memo")
        transformer, target_defs = DefinitionsTransformer.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        remap = migrate_commit_database(src, transformer, tgt)["remap"]

        ag = V.CommitStateBuilder.state(tgt, remap[c2.representation()]).attachment_getting()
        tatt = tgt.definitions().attachments()[0]
        d1 = V.ValueStructure.cast(ag.get(tatt, k1).unwrap(encoded=False))
        d2 = V.ValueStructure.cast(ag.get(tatt, k2).unwrap(encoded=False))
        # external kept verbatim; intra-DAG reference remapped to c1's re-issued id
        self.assertEqual(external.representation(), d1.at("prev", encoded=False).representation())
        self.assertEqual(remap[c1.representation()].representation(),
                         d2.at("prev", encoded=False).representation())
        # collect_commit_ids sees the new id, never the stale c1
        ids2 = {i.representation() for i in V.Value.collect_commit_ids(d2)}
        self.assertIn(remap[c1.representation()].representation(), ids2)
        self.assertNotIn(c1.representation(), ids2)


class TestRunCommitMigrationOnDisk(unittest.TestCase):
    def test_real_file_commit_migration(self):
        tmp = tempfile.mkdtemp()
        src_path, tgt_path = os.path.join(tmp, "src.cdb"), os.path.join(tmp, "tgt.cdb")
        try:
            src = V.CommitDatabase.create(src_path)
            defs = V.Definitions()
            customer = defs.create_concept(NS, "Customer")
            order = struct(defs, "Order", [("qty", T.INT32)])
            defs.create_attachment(NS, "Orders", customer, order)
            src.extend_definitions(defs.const())
            att = src.definitions().attachments()[0]
            k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
            cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
            cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5}))
            c1 = src.commit_mutations("set", cms0)
            cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
            cms1.attachment_mutating().update(att, k1, V.Path.from_field("qty").const(), 7)
            src.commit_mutations("update .qty", cms1)
            src.close()

            def build_directives(defs):
                d = TransformationDirectives()
                d.rename_field("Demo::Order", "qty", "count")
                return d

            info = run_commit_migration(src_path, build_directives, tgt_path)
            self.assertEqual({"commits": 2, "blobs": 0}, info)          # operator summary, history preserved

            tgt = V.CommitDatabase.open(tgt_path, readonly=True)
            state = V.CommitStateBuilder.state(tgt, tgt.last_commit_id())
            tatt = tgt.definitions().attachments()[0]
            key = state.attachment_getting().keys(tatt).at(0, encoded=False)
            doc = V.ValueStructure.cast(state.attachment_getting().get(tatt, key).unwrap(encoded=False))
            self.assertEqual(7, doc.at("count", encoded=True))          # update carried, field renamed
            tgt.close()
        finally:
            for p in (src_path, tgt_path):
                if os.path.exists(p):
                    os.remove(p)
            os.rmdir(tmp)


if __name__ == "__main__":
    unittest.main()
