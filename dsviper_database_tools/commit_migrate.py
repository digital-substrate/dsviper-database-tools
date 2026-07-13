"""CommitDatabase migration — faithful structural replay.

Re-issue every commit in topological order, remapping ids (old->new) and translating
each opcode through the rewrite engine. History is **preserved** (unlike a flatten/carry
that collapses it). Because `state()` is a structural DFS linearization in which the
`CommitId` is an *identity* key (dedup of re-convergent arcs) and never an ordering key,
migration preserves the DAG topology and therefore **commutes** with evaluation:

    state(target, remap[C]) == migrate(state(source, C))   -- every commit C, merges included.

A `Merge` commit merges no value; it only seeds the linearization with its second
branch, so it is re-issued as `merge_commit(remap[parent], remap[merged])`.
"""

import dsviper as V

from .blobs import copy_blobs
from .rewrite import DefinitionsTransformer


# -- path remapper: rebuild a PathConst source -> target, renaming each Field (via the
#    struct's field_renames at that level) and transforming Key/Entry values; walk
#    path + type in lockstep. Returns (target PathConst, terminal (struct, field) | None).
def translate_path(tr, source_doc_type, source_path):
    cur = source_doc_type
    out = V.Path()
    terminal = None
    for comp in source_path.components():
        kind = comp.type()
        if kind == "Field":
            fname = comp.value(encoded=True)
            st = V.TypeStructure.cast(cur)
            new = tr.d.field_renames.get(st.representation(), {}).get(fname, fname)
            out = out.field(new)
            terminal = (st.representation(), fname)
            cur = st.check(fname).type()
        elif kind == "Index":
            out = out.index(comp.value(encoded=True))
            cur = V.TypeVector.cast(cur).element_type()
            terminal = None
        elif kind == "Key":
            out = out.key(comp.value(encoded=False))            # domain-free keys for now
            cur = V.TypeMap.cast(cur).element_type()
            terminal = None
        elif kind == "Unwrap":
            out = out.unwrap()
            cur = V.TypeOptional.cast(cur).element_type()
            terminal = None
        elif kind == "Position":
            out = out.position(comp.value(encoded=False))
            cur = V.TypeXArray.cast(cur).element_type()
            terminal = None
        else:
            raise NotImplementedError(f"path component {kind!r} not handled")
    return out.const(), terminal


def _update_value(tr, op, tgt_att, path, terminal):
    """The value for a Document_Update, converted to the target type at the path:
    routed through the terminal field's retype policy (Class B), else to the path type."""
    retype = terminal and tr.d.retyped_fields.get(terminal[0], {}).get(terminal[1])
    if retype:
        new_type, policy = retype
        return tr._retype(op.value(), new_type, policy)
    return tr.value(op.value(), path.check_type(tgt_att.document_type()))


def translate_opcode(op, am, tr, source_defs):
    """Re-issue one opcode (all verbs except XArray_Insert, which is paired — see
    `_replay_opcodes`) onto the target AttachmentMutating, transformed."""
    args = op.arguments(source_defs)
    att, key = args[0], args[1]
    tgt_att, tgt_key = tr.attachment(att), tr.value(key)
    kind = op.type()

    if kind == "Document_Set":
        am.set(tgt_att, tgt_key, tr.value(op.value()))
        return
    path, terminal = translate_path(tr, att.document_type(), op.path())

    if kind == "Document_Update":
        am.update(tgt_att, tgt_key, path, _update_value(tr, op, tgt_att, path, terminal))
    elif kind == "Set_Union":
        am.union_in_set(tgt_att, tgt_key, path, tr.value(op.value()))
    elif kind == "Set_Subtract":
        am.subtract_in_set(tgt_att, tgt_key, path, tr.value(op.value()))
    elif kind == "Map_Union":
        am.union_in_map(tgt_att, tgt_key, path, tr.value(op.value()))
    elif kind == "Map_Update":
        am.update_in_map(tgt_att, tgt_key, path, tr.value(op.value()))
    elif kind == "Map_Subtract":
        am.subtract_in_map(tgt_att, tgt_key, path, tr.value(op.value()))   # value = set of keys
    elif kind == "XArray_Update":
        am.update_in_xarray(tgt_att, tgt_key, path, op.position(), tr.value(op.value()))
    elif kind == "XArray_Remove":
        am.remove_in_xarray(tgt_att, tgt_key, path, op.position())
    else:
        raise NotImplementedError(f"opcode {kind!r} not handled")


def _replay_opcodes(ops, am, tr, source_defs):
    """An `insert_in_xarray(...value)` is stored as an XArray_Insert (empty position) +
    an XArray_Update (the value) — always adjacent. Re-fuse the pair into one insert."""
    i = 0
    while i < len(ops):
        op = ops[i]
        if op.type() == "XArray_Insert":
            nxt = ops[i + 1]                                   # the paired XArray_Update
            args = op.arguments(source_defs)
            att, key = args[0], args[1]
            path, _ = translate_path(tr, att.document_type(), op.path())
            am.insert_in_xarray(tr.attachment(att), tr.value(key), path,
                                op.before_position(), op.position(), tr.value(nxt.value()))
            i += 2
        else:
            translate_opcode(op, am, tr, source_defs)
            i += 1


def migrate_commit_database(source, transformer, target):
    """Faithful structural replay of `source` into `target` under the transformed schema.

    Assumes `target` has been extended with the transformer's target definitions. Owns
    one exclusive transaction (blobs + the whole replay, all-or-nothing): copies the
    blobs the target lacks — streamed, content-addressed, before any commit references
    them; a CommitDatabase is immutable so the rebuild keeps them all — then re-issues
    every commit in topological order, threading an old->new id map. Returns
    `{"commits": n, "blobs": n, "remap": {src repr -> new id}}`.
    """
    driver = source.commit_databasing()
    instancing = source.stream_codec_instancing()
    target_driver = target.commit_databasing()
    remap = {}

    target_driver.begin_transaction(V.Databasing.TRANSACTION_EXCLUSIVE)
    missing = target_driver.unknown_blob_ids(source.blob_ids())
    blobs = copy_blobs(source, target_driver, missing)             # blobs first (streamed)

    # `remap` grows in topological order, so when a commit's values are transformed
    # every commit_id they reference is already known; the engine remaps intra-DAG
    # references at commit_id leaves (external ids fall through, kept verbatim).
    transformer._commit_id_remap = remap
    try:
        for cd in V.CommitData.sort(driver.commit_datas()):
            h = cd.header()
            ctype = h.commit_type()
            parent = h.parent_commit_id()

            if ctype == "Mutations":
                base = (V.CommitStateBuilder.state(target, remap[parent.representation()])
                        if parent.is_valid() else V.CommitStateBuilder.initial_state(target))
                cms = V.CommitMutableState(base)
                _replay_opcodes(cd.opcodes(instancing, source.definitions()),
                                cms.attachment_mutating(), transformer, source.definitions())
                new_id = target.commit_mutations(h.label(), cms)
            elif ctype in ("Merge", "Enable", "Disable"):
                reissue = {"Merge": target.merge_commit, "Enable": target.enable_commit,
                           "Disable": target.disable_commit}[ctype]
                new_id = reissue(h.label(), remap[parent.representation()],
                                 remap[h.target_commit_id().representation()])
            else:
                raise NotImplementedError(f"commit type {ctype!r} not handled")

            remap[h.commit_id().representation()] = new_id

        target_driver.commit()
    finally:
        transformer._commit_id_remap = None

    return {"commits": len(remap), "blobs": blobs, "remap": remap}


def run_commit_migration(source_path, build_directives, target_path):
    """Open the source `CommitDatabase` read-only, build the directives against its live
    schema, and replay it into a fresh target `CommitDatabase`. The source is never
    modified. Mirrors `run_migration` (the plain-`Database` entry point)."""
    source = V.CommitDatabase.open(source_path, readonly=True)
    try:
        directives = build_directives(source.definitions())
        transformer, target_defs = DefinitionsTransformer.from_directives(
            source.definitions(), directives)
        target = V.CommitDatabase.create(target_path)
        try:
            target.extend_definitions(target_defs.const())   # manages its own transaction
            info = migrate_commit_database(source, transformer, target)
            return {"commits": info["commits"], "blobs": info["blobs"]}   # operator summary
        finally:
            target.close()
    finally:
        source.close()
