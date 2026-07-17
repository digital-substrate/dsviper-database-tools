"""Round-trip verifier for a CommitDatabase — it must PASS on a faithful DAG replay
(linear, merge, commit_id remap, carried blob) and FAIL loudly when the rebuilt history
diverges (a commit not re-issued, a mis-threaded remap that lands the wrong state)."""

import unittest

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsRewriter,
    migrate_commit_database, VerificationError)

T = V.Type
NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def struct(defs, name, fields):
    d = V.TypeStructureDescriptor(name)
    for fname, ftype in fields:
        d.add_field(fname, ftype)
    return defs.create_structure(NS, d)


def order_db():
    src = V.CommitDatabase.create_in_memory()
    defs = V.Definitions()
    customer = defs.create_concept(NS, "Customer")
    order = struct(defs, "Order", [("qty", T.INT32), ("label", T.STRING)])
    defs.create_attachment(NS, "Orders", customer, order)
    src.extend_definitions(defs.const())
    return src, order


def rename_qty(order):
    d = TransformationDirectives()
    d.rename_field(order.representation(), "qty", "count")
    return d


def migrate(src, directives):
    rewriter, target_defs = DefinitionsRewriter.from_directives(src.definitions(), directives)
    tgt = V.CommitDatabase.create_in_memory()
    tgt.extend_definitions(target_defs.const())
    info = migrate_commit_database.migrate(src, rewriter, tgt)
    return rewriter, tgt, info


class TestVerifyCommitMigration(unittest.TestCase):
    def _linear(self):
        src, order = order_db()
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5, "label": "a"}))
        c1 = src.commit_mutations("set", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        cms1.attachment_mutating().update(att, k1, V.Path.from_field("qty").const(), 7)
        c2 = src.commit_mutations("update .qty", cms1)
        return src, order, (c1, c2)

    def test_passes_on_faithful_linear(self):
        src, order, _ = self._linear()
        rewriter, tgt, info = migrate(src, rename_qty(order))
        result = migrate_commit_database.verify(src, rewriter, tgt, info["remap"])
        self.assertEqual(2, result["commits"])
        # c1 carries one Document_Set, c2 one Document_Update -> two opcodes verified
        self.assertEqual(2, result["checked"])

    def test_passes_on_faithful_merge(self):
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
        src.merge_commit("merge A,B", cA, cB)                  # merges the two divergent heads
        rewriter, tgt, info = migrate(src, rename_qty(order))
        result = migrate_commit_database.verify(src, rewriter, tgt, info["remap"])
        self.assertEqual(len(src.commit_ids()), result["commits"])
        self.assertEqual(4, result["commits"])                # base, A, B, merge

    def test_passes_with_commit_id_remap(self):
        # a document carries an intra-DAG commit_id leaf; the verifier must remap it the
        # same way the replay did, or the re-derived expected value would not match.
        src = V.CommitDatabase.create_in_memory()
        defs = V.Definitions()
        concept = defs.create_concept(NS, "C")
        ref = struct(defs, "Ref", [("note", T.STRING), ("prev", T.COMMIT_ID)])
        defs.create_attachment(NS, "Refs", concept, ref)
        src.extend_definitions(defs.const())
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        k2 = att.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        external = V.ValueCommitId.try_parse("a" * 40)
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(ref, {"note": "a", "prev": external}))
        c1 = src.commit_mutations("c1", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        cms1.attachment_mutating().set(att, k2, V.ValueStructure(ref, {"note": "b", "prev": c1}))
        src.commit_mutations("c2", cms1)

        d = TransformationDirectives()
        d.rename_field(ref.representation(), "note", "memo")
        rewriter, tgt, info = migrate(src, d)
        result = migrate_commit_database.verify(src, rewriter, tgt, info["remap"])
        self.assertEqual(2, result["commits"])
        # verifier left the rewriter's remap hook cleared
        self.assertIsNone(rewriter._commit_id_remap)

    def test_passes_with_carried_blob(self):
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
        src.commit_mutations("set", cms0)
        rewriter, tgt, info = migrate(src, rename_qty(order))
        result = migrate_commit_database.verify(src, rewriter, tgt, info["remap"])
        self.assertEqual(1, result["referenced_blobs"])

    def test_fails_when_a_commit_was_not_reissued(self):
        src, order, (c1, _c2) = self._linear()
        rewriter, tgt, info = migrate(src, rename_qty(order))
        broken = dict(info["remap"])
        broken.pop(c1.representation())                       # pretend c1 was lost
        with self.assertRaises(VerificationError) as cm:
            migrate_commit_database.verify(src, rewriter, tgt, broken)
        self.assertIn("re-issued", str(cm.exception))

    def test_fails_on_wrong_remap_lands_wrong_state(self):
        # swap the two commits' target ids: source@c1 (a root Set) now maps to the image of
        # c2 (a child Update) -> the broken parent link is caught (the topology check fires
        # before the opcode check; either way a wrong remap can't slip through).
        src, order, (c1, c2) = self._linear()
        rewriter, tgt, info = migrate(src, rename_qty(order))
        swapped = dict(info["remap"])
        swapped[c1.representation()], swapped[c2.representation()] = (
            info["remap"][c2.representation()], info["remap"][c1.representation()])
        with self.assertRaises(VerificationError) as cm:
            migrate_commit_database.verify(src, rewriter, tgt, swapped)
        self.assertIn("parent link", str(cm.exception))

    def test_fails_on_opcode_value_drift(self):
        # verify against a DIFFERENT rewriter than migrate used (renames to another field) —
        # the topology is intact, so the opcode-value check is what must catch the drift.
        src, order, _ = self._linear()
        rewriter, tgt, info = migrate(src, rename_qty(order))       # migrated qty -> count
        other = TransformationDirectives()
        other.rename_field(order.representation(), "qty", "tally")  # a divergent rewrite
        wrong, _tdefs = DefinitionsRewriter.from_directives(src.definitions(), other)
        with self.assertRaises(VerificationError) as cm:
            migrate_commit_database.verify(src, wrong, tgt, info["remap"])
        self.assertIn("mismatch", str(cm.exception))

    def test_fails_on_broken_topology_with_identical_opcodes(self):
        # two Mutations commits with IDENTICAL opcodes (same key + value) but different parents
        # (one a root, one its child). Swapping their remap keeps every opcode matching -> only
        # the parent link differs; verify must catch the broken topology, not just the opcodes.
        src, order = order_db()
        att = src.definitions().attachments()[0]
        k1 = att.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src))
        cms0.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5, "label": "a"}))
        c1 = src.commit_mutations("set", cms0)                         # root
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c1))
        cms1.attachment_mutating().set(att, k1, V.ValueStructure(order, {"qty": 5, "label": "a"}))
        c2 = src.commit_mutations("set again", cms1)                   # child of c1, identical opcode
        rewriter, tgt, info = migrate(src, rename_qty(order))
        swapped = dict(info["remap"])
        swapped[c1.representation()], swapped[c2.representation()] = (
            info["remap"][c2.representation()], info["remap"][c1.representation()])
        with self.assertRaises(VerificationError) as cm:
            migrate_commit_database.verify(src, rewriter, tgt, swapped)
        self.assertIn("parent link", str(cm.exception))


class TestVerifyOpcodeFaithful(unittest.TestCase):
    """`verify` checks that each *opcode* was correctly rewritten, not that every materialised
    document re-derives from every commit's snapshot. So a non-local hook whose input **varies
    across the history** — an aggregate over a growing set, or a single reference to a document
    updated later — no longer false-fails: a document's derived field is checked only at the
    opcode that wrote it, under that commit's own view, never re-derived at a later commit that
    never touched it."""

    def _cust_order_schema(self):
        src = V.CommitDatabase.create_in_memory()
        defs = V.Definitions()
        customer = defs.create_concept(NS, "Customer")
        cust_doc = struct(defs, "CustomerDoc", [("name", T.STRING)])
        custs = defs.create_attachment(NS, "Customers", customer, cust_doc)
        order = defs.create_concept(NS, "Order")
        order_doc = struct(defs, "OrderDoc", [("custRef", custs.type_key()), ("qty", T.INT32)])
        orders = defs.create_attachment(NS, "Orders", order, order_doc)
        src.extend_definitions(defs.const())
        return src, cust_doc, order_doc, custs, orders

    def test_aggregate_over_growing_dag_verifies(self):
        # Customer.orderCount folds an INCOMING reference (orders referencing this customer);
        # the set grows commit to commit. migrate freezes it at each write; verify checks the
        # write opcode, not a re-fold at a later commit -> no false failure.
        src, cust_doc, _order_doc, custs, orders = self._cust_order_schema()
        ck = custs.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        o1 = orders.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        o2 = orders.create_key(V.ValueUUId("33333333-3333-3333-3333-333333333333"))
        odt = orders.document_type()
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src)); am = cms0.attachment_mutating()
        am.set(custs, ck, V.ValueStructure(cust_doc, {"name": "Ada"}))
        am.set(orders, o1, V.ValueStructure(odt, {"custRef": ck, "qty": 7}))
        c0 = src.commit_mutations("cust+order1", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c0))
        cms1.attachment_mutating().set(orders, o2, V.ValueStructure(odt, {"custRef": ck, "qty": 10}))
        src.commit_mutations("order2", cms1)

        def order_count(source_struct, field_name, target_type, ctx):
            n = 0
            keys = ctx.attachment_getting.keys(orders)
            for i in range(keys.size()):
                od = ctx.attachment_getting.get(orders, keys.at(i, encoded=False))
                if od.is_nil():
                    continue
                ref = V.ValueStructure.cast(od.unwrap(encoded=False)).at("custRef", encoded=False)
                if ref.representation() == ctx.self_key.representation():
                    n += 1
            return V.ValueInt32(n)

        d = TransformationDirectives()
        d.add_field(cust_doc.representation(), "orderCount", T.INT32, derive=order_count)
        rewriter, tgt, info = migrate(src, d)
        result = migrate_commit_database.verify(src, rewriter, tgt, info["remap"])
        self.assertEqual(2, result["commits"])          # faithful — no false VerificationError

    def test_single_ref_to_later_updated_doc_verifies(self):
        # order derives customerName from the customer; the customer is RENAMED in a later
        # commit while the order is never rewritten. The order's frozen customerName is
        # opcode-faithful; verify must not re-derive it under the renamed customer.
        src, cust_doc, order_doc, custs, orders = self._cust_order_schema()
        ck = custs.create_key(V.ValueUUId("11111111-1111-1111-1111-111111111111"))
        ok = orders.create_key(V.ValueUUId("22222222-2222-2222-2222-222222222222"))
        odt = orders.document_type()
        cms0 = V.CommitMutableState(V.CommitStateBuilder.initial_state(src)); am = cms0.attachment_mutating()
        am.set(custs, ck, V.ValueStructure(cust_doc, {"name": "Ada"}))
        am.set(orders, ok, V.ValueStructure(odt, {"custRef": ck, "qty": 7}))
        c0 = src.commit_mutations("c0", cms0)
        cms1 = V.CommitMutableState(V.CommitStateBuilder.state(src, c0))
        cms1.attachment_mutating().update(custs, ck, V.Path.from_field("name").const(), "Ada2")
        src.commit_mutations("rename cust", cms1)

        def derive_name(source_struct, field_name, target_type, ctx):
            cust = ctx.attachment_getting.get(custs, source_struct.at("custRef", encoded=False))
            return V.ValueString(V.ValueStructure.cast(cust.unwrap(encoded=False)).at("name", encoded=False))

        d = TransformationDirectives()
        d.add_field(order_doc.representation(), "customerName", T.STRING, derive=derive_name)
        rewriter, tgt, info = migrate(src, d)
        result = migrate_commit_database.verify(src, rewriter, tgt, info["remap"])
        self.assertEqual(2, result["commits"])          # faithful — no false VerificationError


if __name__ == "__main__":
    unittest.main()
