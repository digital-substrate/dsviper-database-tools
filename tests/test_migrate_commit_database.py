"""CommitDatabase faithful-replay tests — history preserved + commutation per commit
(merges included), across the opcode verbs, path rename, and retype-at-path."""

import os
import tempfile
import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsRewriter, Unrepresentable,
    VerificationError, migrate_commit_database)

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


def snapshot(db, commit_id, rewriter=None):
    """Materialise a state as {(attachment, instance): dumps(document)} — migrating each
    document through `rewriter` first when given (the RHS of the commutation law)."""
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
            if rewriter is not None:
                try:
                    val = rewriter.value(val)
                except Unrepresentable:
                    continue
                att_local = rewriter.attachment(att).identifier().split(".")[-1]
                inst = rewriter.value(key).instance_id().representation()
            else:
                att_local = att.identifier().split(".")[-1]
                inst = key.instance_id().representation()
            snap[(att_local, inst)] = V.Value.dumps(val)
    return snap


class CommitReplayCase(unittest.TestCase):
    def _prove(self, src, order, directives, commits):
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), directives)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        info = migrate_commit_database.migrate(src, rewriter, tgt)
        # history preserved
        self.assertEqual(len(src.commit_ids()), len(tgt.commit_ids()))
        self.assertEqual(len(src.commit_ids()), info["commits"])
        # commutation per commit
        for c in commits:
            lhs = snapshot(tgt, info["remap"][c.representation()])
            rhs = snapshot(src, c, rewriter=rewriter)
            self.assertEqual(rhs, lhs, f"commutation failed at {c.representation()[:8]}")
        return rewriter, tgt, info


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


def order_db_on_disk(path):
    src = V.CommitDatabase.create(path)
    defs = V.Definitions()
    customer = defs.create_concept(NS, "Customer")
    order = struct(defs, "Order", [("qty", T.INT32)])
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

        rewriter, target_defs = DefinitionsRewriter.from_directives(
            src.definitions(), rename_qty(order))
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        info = migrate_commit_database.migrate(src, rewriter, tgt)
        self.assertIn(blob, tgt.blob_ids())
        self.assertEqual(b"receipt bytes", bytes(tgt.blob(blob)))
        self.assertEqual(snapshot(src, c1, rewriter),
                         snapshot(tgt, info["remap"][c1.representation()]))


class TestBlobCopyOnReference(unittest.TestCase):
    """Blobs are streamed **on reference** during the replay: exactly the blobs the rebuilt
    history references are copied — a blob stranded by a dropped/retyped field is never copied
    (the CommitDatabase has no blob-delete verb to sweep an orphan), and a shared blob is
    copied once. `verify` asserts the target holds no leftover orphan either."""

    def _seed_two_blob_fields(self):
        src = V.CommitDatabase.create_in_memory()
        blobA = src.create_blob_from_buffer(b"referenced")
        blobB = src.create_blob_from_buffer(b"stranded")
        defs = V.Definitions()
        c = defs.create_concept(NS, "C")
        doc = struct(defs, "Doc", [("keep", T.BLOB_ID), ("drop", T.BLOB_ID)])
        defs.create_attachment(NS, "Docs", c, doc)
        src.extend_definitions(defs.const())
        a = src.definitions().attachments()[0]
        cms = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms.attachment_mutating().set(
            a, a.create_key(V.ValueUUId("33333333-3333-3333-3333-333333333333")),
            V.ValueStructure(doc, {"keep": blobA, "drop": blobB}))
        src.commit_mutations("set", cms)
        return src, doc, blobA, blobB

    def test_stranded_blob_never_copied(self):
        src, doc, blobA, blobB = self._seed_two_blob_fields()
        d = TransformationDirectives()
        d.drop_field(doc.representation(), "drop")          # strands blobB
        rw, tdefs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(tdefs.const())
        info = migrate_commit_database.migrate(src, rw, tgt)
        present = {b.representation() for b in tgt.blob_ids()}
        self.assertEqual(1, info["blobs"])                  # only the referenced blob copied
        self.assertIn(blobA.representation(), present)
        self.assertNotIn(blobB.representation(), present)   # the stranded blob is never copied
        migrate_commit_database.verify(src, rw, tgt, info["remap"])   # no orphan → passes

    def test_shared_blob_copied_once(self):
        src = V.CommitDatabase.create_in_memory()
        sb = src.create_blob_from_buffer(b"shared")
        defs = V.Definitions()
        c = defs.create_concept(NS, "C")
        doc = struct(defs, "Doc", [("r", T.BLOB_ID)])
        defs.create_attachment(NS, "Docs", c, doc)
        src.extend_definitions(defs.const())
        a = src.definitions().attachments()[0]
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(
            a, a.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111")),
            V.ValueStructure(doc, {"r": sb}))
        c1 = src.commit_mutations("d1", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        cms1.attachment_mutating().set(
            a, a.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222")),
            V.ValueStructure(doc, {"r": sb}))                # same blob, second commit
        src.commit_mutations("d2", cms1)
        d = TransformationDirectives()
        d.rename_field(doc.representation(), "r", "ref")
        rw, tdefs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(tdefs.const())
        info = migrate_commit_database.migrate(src, rw, tgt)
        self.assertEqual(1, info["blobs"])                  # copied once, deduped across commits

    def test_verify_catches_leftover_orphan(self):
        src, doc, _blobA, _blobB = self._seed_two_blob_fields()
        d = TransformationDirectives()
        d.drop_field(doc.representation(), "drop")
        rw, tdefs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(tdefs.const())
        info = migrate_commit_database.migrate(src, rw, tgt)
        tgt.create_blob_from_buffer(b"injected orphan")     # a blob no commit references
        with self.assertRaises(VerificationError) as cm:
            migrate_commit_database.verify(src, rw, tgt, info["remap"])
        self.assertIn("orphan", str(cm.exception))


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
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        remap = migrate_commit_database.migrate(src, rewriter, tgt)["remap"]

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

            info = migrate_commit_database.run(src_path, build_directives, tgt_path)
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


class TestDropRecordRefused(unittest.TestCase):
    """`drop-record` is record-scoped; a CommitDatabase rewrites opcode-carried values
    (no document to drop), so the replay refuses it up front, before touching data."""

    def test_commitdb_migration_refuses_drop_record(self):
        src = V.CommitDatabase.create_in_memory()
        defs = V.Definitions()
        customer = defs.create_concept(NS, "Customer")
        order = struct(defs, "Order", [("qty", V.TypeOptional(T.INT32))])
        defs.create_attachment(NS, "Orders", customer, order)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        ot = V.TypeOptional(T.INT32)
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": V.ValueOptional(ot, 5)}))
        src.commit_mutations("set", cms0)

        d = TransformationDirectives()
        d.retype_field(order.representation(), "qty", T.INT32, policy="drop-record")
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        with self.assertRaises(ValueError) as cm:
            migrate_commit_database.migrate(src, rewriter, tgt)
        self.assertIn("drop-record", str(cm.exception))

    def test_hook_dropping_a_value_is_refused_clearly_at_runtime(self):
        # The runtime twin of the up-front drop-record refusal: a Class-C hook that returns
        # Unrepresentable (on a Database this DROPS the document) has no opcode-level meaning
        # on a CommitDatabase — dropping one mutation corrupts the trajectory — so the replay
        # refuses the whole migration with a clear ValueError and rolls the transaction back,
        # rather than aborting on an opaque Unrepresentable.
        src, order = order_db()
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5, "label": "a"}))
        src.commit_mutations("set", cms0)

        def drop_hook(source_struct, field_name, target_type, ctx=None):
            raise Unrepresentable("this value has no faithful image")

        d = TransformationDirectives()
        d.add_field(order.representation(), "note", T.STRING, derive=drop_hook)
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        with self.assertRaises(ValueError) as cm:
            migrate_commit_database.migrate(src, rewriter, tgt)
        self.assertIn("no faithful target image", str(cm.exception))
        self.assertFalse(tgt.commit_databasing().in_transaction())   # rolled back
        self.assertEqual(0, len(tgt.commit_ids()))


class TestDryRun(unittest.TestCase):
    """`dry_run` — the inform step: exercise the rewriter over every opcode with no target/write,
    previewing Class-B policy bites (diagnostics) and the would-abort record-scoped losses."""

    def _linear(self, qty_type=T.INT32, first=5, second=7):
        src, order = order_db(qty_type)
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": first, "label": "a"}))
        c1 = src.commit_mutations("set", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        cms1.attachment_mutating().update(att, k1, V.Path.from_field("qty").const(), second)
        src.commit_mutations("update", cms1)
        return src, order

    def _dry(self, src, directives):
        rewriter, _tdefs = DefinitionsRewriter.from_directives(src.definitions(), directives)
        return migrate_commit_database.dry_run(src, rewriter)

    def test_clean_migration_previews_no_loss(self):
        src, order = self._linear()
        r = self._dry(src, rename_qty(order))
        self.assertEqual(2, r["opcodes"])                        # a Set + an Update
        self.assertEqual([], r["unrepresentable"])
        self.assertEqual(0, r["stranded_blobs"])

    def test_value_closed_policy_shows_in_diagnostics(self):
        src, order = self._linear(qty_type=T.INT64, first=5, second=2**40)
        d = TransformationDirectives()
        d.retype_field(order.representation(), "qty", T.INT32, policy="saturate")
        r = self._dry(src, d)
        self.assertEqual([], r["unrepresentable"])              # value-closed → not a would-abort
        self.assertEqual(1, r["diagnostics"]["summary"]["findings"])   # the saturate bit is recorded

    def test_drop_record_policy_is_a_would_abort_site(self):
        src = V.CommitDatabase.create_in_memory()
        defs = V.Definitions()
        customer = defs.create_concept(NS, "Customer")
        order = struct(defs, "Order", [("qty", V.TypeOptional(T.INT32))])
        defs.create_attachment(NS, "Orders", customer, order)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        ot = V.TypeOptional(T.INT32)
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(
            att, att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111")),
            V.ValueStructure(order, {"qty": V.ValueOptional(ot, 5)}))
        src.commit_mutations("set", cms0)
        d = TransformationDirectives()
        d.retype_field(order.representation(), "qty", T.INT32, policy="drop-record")
        r = self._dry(src, d)                                   # dry_run informs, does NOT raise
        self.assertEqual(1, len(r["unrepresentable"]))
        self.assertIn("drop-record", r["unrepresentable"][0])

    def test_hook_drop_is_a_dynamic_would_abort_site(self):
        src, order = self._linear()
        def drop_hook(source_struct, field_name, target_type, ctx=None):
            raise Unrepresentable("no image")
        d = TransformationDirectives()
        d.add_field(order.representation(), "note", T.STRING, derive=drop_hook)
        r = self._dry(src, d)                                   # found only by running
        self.assertTrue(r["unrepresentable"])
        self.assertIn("Document_Set", r["unrepresentable"][0])

    def test_stranded_blob_previewed(self):
        src = V.CommitDatabase.create_in_memory()
        blob = src.create_blob_from_buffer(b"receipt")
        defs = V.Definitions()
        customer = defs.create_concept(NS, "Customer")
        order = struct(defs, "Order", [("qty", T.INT32), ("receipt", T.BLOB_ID)])
        defs.create_attachment(NS, "Orders", customer, order)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(
            att, att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111")),
            V.ValueStructure(order, {"qty": 5, "receipt": blob}))
        src.commit_mutations("set", cms0)
        d = TransformationDirectives()
        d.drop_field(order.representation(), "receipt")
        r = self._dry(src, d)
        self.assertEqual(0, r["referenced_blobs"])              # nothing references it after the drop
        self.assertEqual(1, r["stranded_blobs"])                # the blob would not be copied


class TestEnableDisable(CommitReplayCase):
    """`Enable`/`Disable` commits (feature-flag history) are re-issued structurally — no opcodes,
    parent + target ids remapped. A disable→enable pair round-trips and the commutation holds at
    every commit (the real `.rapmc` sync histories are full of these)."""

    def test_disable_then_enable_replays_and_verifies(self):
        src, order = order_db()
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5, "label": "a"}))
        c0 = src.commit_mutations("base", cms0)
        cmsA = V.CommitMutableState(V.CommitStateBuilder.state(src, c0))
        cmsA.attachment_mutating().update(att, k1, V.Path.from_field("qty").const(), 7)
        cA = src.commit_mutations("A", cmsA)
        src.disable_commit("disable A", src.last_commit_id(), cA)     # revert cA's effect
        src.enable_commit("enable A", src.last_commit_id(), cA)       # restore it
        rewriter, tgt, info = self._prove(src, order, rename_qty(order), [c0, cA])
        self.assertEqual(4, len(tgt.commit_ids()))                   # base, A, disable, enable
        result = migrate_commit_database.verify(src, rewriter, tgt, info["remap"])
        self.assertEqual(4, result["commits"])


class TestProgress(unittest.TestCase):
    """`migrate(on_progress=)` reports a `CommitMigrationProgress` as work advances: a byte bar
    (per streamed chunk, so it moves even through one multi-chunk blob) and a commit counter."""

    def test_byte_bar_and_commit_counter(self):
        src = V.CommitDatabase.create_in_memory()
        big = src.create_blob_from_buffer(b"x" * (150 * 1024 * 1024))   # 150 MB -> 3 chunks of 64 MB
        defs = V.Definitions()
        customer = defs.create_concept(NS, "Customer")
        order = struct(defs, "Order", [("qty", T.INT32), ("receipt", T.BLOB_ID)])
        defs.create_attachment(NS, "Orders", customer, order)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5, "receipt": big}))
        c1 = src.commit_mutations("set", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        cms1.attachment_mutating().update(att, k1, V.Path.from_field("qty").const(), 7)
        src.commit_mutations("update", cms1)

        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), rename_qty(order))
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        events = []
        migrate_commit_database.migrate(src, rewriter, tgt, on_progress=events.append)

        last = events[-1]
        self.assertEqual(2, last.commit_count)
        self.assertEqual(2, last.commits)                          # counter reached the total
        self.assertEqual(1, last.blobs)
        self.assertEqual(last.bytes_total, last.bytes_copied)      # byte bar reached the total
        # the bar advanced per 64 MB chunk through the single 150 MB blob (not one jump)
        climbs = sorted({e.bytes_copied for e in events if e.bytes_copied})
        self.assertGreater(len(climbs), 1)
        self.assertEqual(sorted(climbs), climbs)                   # monotonic


class TestNonLocalHookInReplay(unittest.TestCase):
    """A non-local Class-C derive hook under DAG replay. The customer is committed first,
    the order (referencing it) in a later commit; the migration adds `Order.customerName`
    by dereferencing `custRef` through the source view. `migrate` wires the *parent* commit
    state as the view (the pre-image); `verify` wires each commit's own state — the two
    agree because the referenced customer is stable, so the commutation law holds."""

    def test_derive_across_commits_verifies(self):
        src = V.CommitDatabase.create_in_memory()
        defs = V.Definitions()
        customer = defs.create_concept(NS, "Customer")
        cust_doc = struct(defs, "CustomerDoc", [("name", T.STRING)])
        custs_att = defs.create_attachment(NS, "Customers", customer, cust_doc)
        order = defs.create_concept(NS, "Order")
        order_doc = struct(defs, "OrderDoc",
                           [("custRef", custs_att.type_key()), ("qty", T.INT32)])
        orders_att = defs.create_attachment(NS, "Orders", order, order_doc)
        src.extend_definitions(defs.const())

        ck = custs_att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        ok = orders_att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(custs_att, ck, V.ValueStructure(cust_doc, {"name": "Ada"}))
        c0 = src.commit_mutations("add customer", cms0)                # parent commit
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c0))
        cms1.attachment_mutating().set(
            orders_att, ok, V.ValueStructure(order_doc, {"custRef": ck, "qty": 7}))
        c1 = src.commit_mutations("add order", cms1)

        def derive_name(source_struct, field_name, target_type, ctx):
            key = source_struct.at("custRef", encoded=False)
            cust = ctx.attachment_getting.get(custs_att, key)
            name = V.ValueStructure.cast(cust.unwrap(encoded=False)).at("name", encoded=False)
            return V.ValueString(name)

        d = TransformationDirectives()
        d.add_field(order_doc.representation(), "customerName", T.STRING, derive=derive_name)

        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        info = migrate_commit_database.migrate(src, rewriter, tgt)

        # the head order carries the dereferenced name
        head = info["remap"][c1.representation()]
        state = V.CommitStateBuilder.state(tgt, head)
        ag = state.attachment_getting()
        tgt_orders = next(a for a in state.definitions().attachments()
                          if a.representation().endswith("Orders"))
        tkeys = ag.keys(tgt_orders)
        doc = V.ValueStructure.cast(ag.get(tgt_orders, tkeys.at(0, encoded=False)).unwrap(encoded=False))
        self.assertEqual("Ada", doc.at("customerName", encoded=False))

        # and the whole-DAG commutation law holds with the hook (verify wires the view)
        summary = migrate_commit_database.verify(src, rewriter, tgt, info["remap"])
        self.assertEqual(2, summary["commits"])


class TestDropAttachment(unittest.TestCase):
    """`drop_attachment` is materialisation-independent (a static, uniform whole-partition drop —
    every opcode addressing it is skipped, exactly as silo 2 skips the attachment's documents; keys
    are not foreign keys, so nothing dangles). So it is admissible on a CommitDatabase too, under
    the **shared** acknowledgement gate (unlike `drop-record`, whose per-value trigger over a
    trajectory is genuinely materialisation-dependent, and stays refused)."""

    def _two_attachments(self):
        src = V.CommitDatabase.create_in_memory()
        defs = V.Definitions()
        cust = defs.create_concept(NS, "Customer")
        order = struct(defs, "Order", [("qty", T.INT32)])
        defs.create_attachment(NS, "Orders", cust, order)
        note = struct(defs, "Note", [("text", T.STRING), ("tags", V.TypeXArray(T.STRING))])
        defs.create_attachment(NS, "Notes", cust, note)
        src.extend_definitions(defs.const())
        oatt, natt = src.definitions().attachments()
        ok = oatt.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        nk = natt.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src)); am = cms0.attachment_mutating()
        am.set(oatt, ok, V.ValueStructure(order, {"qty": 5}))
        am.set(natt, nk, V.ValueStructure(note, {"text": "hi"}))
        c0 = src.commit_mutations("c0", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c0)); am = cms1.attachment_mutating()
        am.update(oatt, ok, V.Path.from_field("qty").const(), 7)
        am.insert_in_xarray(natt, nk, V.Path.from_field("tags").const(), V.ValueXArray.END,
                            V.ValueUUId("aaaaaaaa-0000-0000-0000-000000000001"), "x")  # a PAIR to skip
        src.commit_mutations("c1", cms1)
        return src, order

    def test_refused_without_acknowledgement(self):
        src, _order = self._two_attachments()
        d = TransformationDirectives()
        d.drop_attachment("Notes")                             # no accept_attachment_drops()
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        with self.assertRaises(ValueError) as cm:
            migrate_commit_database.migrate(src, rewriter, tgt)
        self.assertIn("unacknowledged", str(cm.exception).lower())

    def test_acknowledged_drop_skips_the_partition_and_verifies(self):
        src, _order = self._two_attachments()
        d = TransformationDirectives()
        d.drop_attachment("Notes")
        d.accept_attachment_drops()
        rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), d)
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(target_defs.const())
        info = migrate_commit_database.migrate(src, rewriter, tgt)
        self.assertEqual(2, info["commits"])
        tatts = [a.representation() for a in tgt.definitions().attachments()]
        self.assertTrue(any(r.endswith("Orders") for r in tatts))
        self.assertFalse(any(r.endswith("Notes") for r in tatts))   # partition gone
        # Orders survives with its update; the Notes opcodes (incl. the xarray insert pair) skipped
        head = V.CommitStateBuilder.state(tgt, tgt.last_commit_id())
        toa = next(a for a in head.definitions().attachments() if a.representation().endswith("Orders"))
        k = head.attachment_getting().keys(toa).at(0, encoded=False)
        doc = V.ValueStructure.cast(head.attachment_getting().get(toa, k).unwrap(encoded=False))
        self.assertEqual(7, doc.at("qty", encoded=True))
        # verify aligns the opcode streams (dropped-attachment opcodes filtered on both sides)
        result = migrate_commit_database.verify(src, rewriter, tgt, info["remap"])
        self.assertEqual(2, result["commits"])


class TestCommitMigrationFailureSafety(unittest.TestCase):
    """A DAG replay is all-or-nothing. A mid-replay failure (here: a hook that raises while
    transforming a commit's opcodes) must abort the exclusive transaction — no dangling lock,
    no half-issued commits — and `run` must discard the partial target file, never leave a
    corrupt artefact behind."""

    @staticmethod
    def _boom_directives(source_defs):
        calls = [0]
        def boom(source_struct, field_name, target_type, ctx):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("boom mid-replay")
            return V.ValueInt32(calls[0])
        d = TransformationDirectives()
        d.add_field("Demo::Order", "tag", T.INT32, derive=boom)
        return d

    def _seed(self, src, order):
        att = src.definitions().attachments()[0]
        for i, u in enumerate(("11111111-1111-1111-1111-111111111111",
                               "22222222-2222-2222-2222-222222222222")):
            base = (V.CommitStateBuilder.initial_state(src) if i == 0
                    else V.CommitStateBuilder.state(src, src.last_commit_id()))
            cms = V.CommitMutableState(base)
            cms.attachment_mutating().set(
                att, att.create_key(V.ValueUUId(u)), V.ValueStructure(order, {"qty": i}))
            src.commit_mutations(f"c{i}", cms)

    def test_migrate_rolls_back_on_failure(self):
        src, order = order_db()
        self._seed(src, order)
        rw, tdefs = DefinitionsRewriter.from_directives(
            src.definitions(), self._boom_directives(src.definitions()))
        tgt = V.CommitDatabase.create_in_memory()
        tgt.extend_definitions(tdefs.const())
        with self.assertRaises(RuntimeError):
            migrate_commit_database.migrate(src, rw, tgt)
        # the exclusive transaction was aborted, not left dangling — the target is usable again
        self.assertFalse(tgt.commit_databasing().in_transaction())
        self.assertEqual(0, len(tgt.commit_ids()))         # no half-issued commits survive

    def test_run_discards_partial_target_on_failure(self):
        tmp = tempfile.mkdtemp()
        sp, tp = os.path.join(tmp, "src.cdb"), os.path.join(tmp, "tgt.cdb")
        try:
            src, order = order_db_on_disk(sp)
            self._seed(src, order)
            src.close()
            with self.assertRaises(RuntimeError):
                migrate_commit_database.run(sp, self._boom_directives, tp)
            self.assertFalse(os.path.exists(tp))           # partial target discarded
        finally:
            for p in (sp, tp, sp + "-wal", tp + "-wal", sp + "-shm", tp + "-shm",
                      sp + "-journal", tp + "-journal"):
                if os.path.exists(p):
                    os.remove(p)
            os.rmdir(tmp)


if __name__ == "__main__":
    unittest.main()
