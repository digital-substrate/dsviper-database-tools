"""CommitDatabase migration — faithful structural replay.

Re-issue every commit in topological order, remapping ids (old->new) and translating
each opcode through the rewrite engine. History is **preserved** (unlike a flatten/carry
that collapses it). Because `state()` is a structural DFS linearization in which the
`CommitId` is an *identity* key (dedup of re-convergent arcs) and never an ordering key,
migration preserves the DAG topology and therefore **commutes** with evaluation:

    state(target, remap[C]) == migrate(state(source, C))   -- every commit C, merges included.

A `Merge` commit merges no value; it only seeds the linearization with its second
branch, so it is re-issued as `merge_commit(remap[parent], remap[merged])`.

This module is silo 3 in full: the replay loop (`migrate`), its round-trip self-check
(`verify`), and the `run` entry point. The shared
`VerificationError` is imported from the Database silo, whose verifier this one mirrors.
"""

from collections import namedtuple

import dsviper as V

from .blobs import copy_blob
from .rewrite import DefinitionsRewriter, Unrepresentable, DiagnosticSink
from .migrate_database import (
    VerificationError, _remove_db_file, _refuse_unacknowledged_attachment_drops)


# A progress snapshot handed to a `migrate(..., on_progress=)` callback. The dominant cost is
# blob BYTES (a multi-GB history is GBs of blobs, not opcode count): `bytes_copied` climbs per
# streamed chunk against `bytes_total` (the source's total blob bytes — an upper bound, exact
# when nothing is stranded). `commits`/`commit_count` give a cheap structural position (silo 3's
# natural secondary unit — a commit, not an attachment); `blobs` is a plain tally.
CommitMigrationProgress = namedtuple(
    "CommitMigrationProgress",
    ["commits", "commit_count", "blobs", "bytes_copied", "bytes_total"])


class _Progress:
    """Accumulates progress and fires `on_progress(CommitMigrationProgress)` on each change. A
    `None` callback accumulates silently. Bytes advance per streamed chunk, so the bar moves even
    through one multi-gigabyte blob."""
    __slots__ = ("_cb", "commits", "commit_count", "blobs", "bytes_copied", "bytes_total")

    def __init__(self, on_progress, commit_count, bytes_total):
        self._cb = on_progress
        self.commit_count = commit_count
        self.bytes_total = bytes_total
        self.commits = self.blobs = self.bytes_copied = 0

    def _fire(self):
        if self._cb is not None:
            self._cb(CommitMigrationProgress(
                self.commits, self.commit_count, self.blobs, self.bytes_copied, self.bytes_total))

    def add_bytes(self, n):                            # per streamed chunk — drives the byte bar
        self.bytes_copied += n
        self._fire()

    def blob_done(self):
        self.blobs += 1

    def commit_done(self):
        self.commits += 1
        self._fire()


# -- path remapper: rebuild a PathConst source -> target, renaming each Field (via the
#    struct's field_renames at that level) and transforming Key/Entry values; walk
#    path + type in lockstep. Returns (target PathConst, terminal (struct, field) | None).
def translate_path(rewriter, source_doc_type, source_path):
    cur = source_doc_type
    out = V.Path()
    terminal = None
    for comp in source_path.components():
        kind = comp.type()
        if kind == "Field":
            fname = comp.value(encoded=True)
            st = V.TypeStructure.cast(cur)
            new = rewriter.d.field_renames.get(st.representation(), {}).get(fname, fname)
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


def _update_value(rewriter, op, tgt_att, path, terminal):
    """The value for a Document_Update, converted to the target type at the path:
    routed through the terminal field's retype policy (Class B), else to the path type."""
    retype = terminal and rewriter.d.retyped_fields.get(terminal[0], {}).get(terminal[1])
    if retype:
        new_type, policy = retype
        return rewriter._retype(op.value(), new_type, policy)
    return rewriter.value(op.value(), path.check_type(tgt_att.document_type()))


def translate_opcode(op, am, rewriter, source_defs, ensure_blobs):
    """Re-issue one opcode (all verbs except XArray_Insert, which is paired — see
    `_replay_opcodes`) onto the target AttachmentMutating, transformed. Each verb that ADDS
    a value (set/update/union) streams its referenced blobs into the target first
    (`ensure_blobs`) — a document cannot be persisted referencing an absent blob, so
    blob-before-its-opcode is required; the subtract/remove verbs add no reference and need
    none (their value only identifies elements/keys to delete)."""
    args = op.arguments(source_defs)
    att, key = args[0], args[1]
    rewriter._self_key = key                     # record identity for aggregate hooks
    tgt_att, tgt_key = rewriter.attachment(att), rewriter.value(key)
    kind = op.type()

    if kind == "Document_Set":
        v = rewriter.value(op.value()); ensure_blobs(v)
        am.set(tgt_att, tgt_key, v)
        return
    path, terminal = translate_path(rewriter, att.document_type(), op.path())

    if kind == "Document_Update":
        v = _update_value(rewriter, op, tgt_att, path, terminal); ensure_blobs(v)
        am.update(tgt_att, tgt_key, path, v)
    elif kind == "Set_Union":
        v = rewriter.value(op.value()); ensure_blobs(v)
        am.union_in_set(tgt_att, tgt_key, path, v)
    elif kind == "Set_Subtract":
        am.subtract_in_set(tgt_att, tgt_key, path, rewriter.value(op.value()))   # removal, no blob added
    elif kind == "Map_Union":
        v = rewriter.value(op.value()); ensure_blobs(v)
        am.union_in_map(tgt_att, tgt_key, path, v)
    elif kind == "Map_Update":
        v = rewriter.value(op.value()); ensure_blobs(v)
        am.update_in_map(tgt_att, tgt_key, path, v)
    elif kind == "Map_Subtract":
        am.subtract_in_map(tgt_att, tgt_key, path, rewriter.value(op.value()))   # value = set of keys
    elif kind == "XArray_Update":
        v = rewriter.value(op.value()); ensure_blobs(v)
        am.update_in_xarray(tgt_att, tgt_key, path, op.position(), v)
    elif kind == "XArray_Remove":
        am.remove_in_xarray(tgt_att, tgt_key, path, op.position())
    else:
        raise NotImplementedError(f"opcode {kind!r} not handled")


def _addresses_dropped_attachment(op, source_defs, dropped):
    """True if `op` addresses a dropped attachment — its documents are absent from the target,
    so the opcode is not re-issued (skipped). The CommitDatabase parity of silo 2 skipping a
    dropped attachment's documents: uniform over the whole partition (no partial state), and
    keys are not foreign keys (nothing dangles)."""
    return bool(dropped) and op.arguments(source_defs)[0].identifier().split(".")[-1] in dropped


def _replay_opcodes(ops, am, rewriter, source_defs, ensure_blobs):
    """An `insert_in_xarray(...value)` is stored as an XArray_Insert (empty position) +
    an XArray_Update (the value) — always adjacent. Re-fuse the pair into one insert. Opcodes
    addressing a dropped attachment are skipped (the insert PAIR together)."""
    dropped = rewriter.d.dropped_attachments
    i = 0
    while i < len(ops):
        op = ops[i]
        is_insert = op.type() == "XArray_Insert"
        if _addresses_dropped_attachment(op, source_defs, dropped):
            i += 2 if is_insert else 1                     # skip the paired update too
            continue
        if is_insert:
            nxt = ops[i + 1]                                   # the paired XArray_Update
            args = op.arguments(source_defs)
            att, key = args[0], args[1]
            rewriter._self_key = key                       # record identity for aggregate hooks
            path, _ = translate_path(rewriter, att.document_type(), op.path())
            v = rewriter.value(nxt.value()); ensure_blobs(v)
            am.insert_in_xarray(rewriter.attachment(att), rewriter.value(key), path,
                                op.before_position(), op.position(), v)
            i += 2
        else:
            translate_opcode(op, am, rewriter, source_defs, ensure_blobs)
            i += 1


def migrate(source, rewriter, target, on_progress=None):
    """Faithful structural replay of `source` into `target` under the transformed schema.

    Assumes `target` has been extended with the rewriter's target definitions. Owns one
    exclusive transaction (blobs + the whole replay, all-or-nothing — rolled back on any
    failure): re-issues every commit in topological order, threading an old->new id map, and
    streams each blob an opcode references **on reference** (before the opcode that carries it,
    content-addressed, deduped) — so a blob stranded by a dropped/retyped field is never
    copied and the target holds no orphan. Returns
    `{"commits": n, "blobs": n, "remap": {src repr -> new id}}`.

    `on_progress`, if given, is called with a `CommitMigrationProgress` as work advances —
    bytes per streamed chunk (against the source's total blob bytes) and commit position — for
    a progress bar over the dominant cost (blob I/O) and the history's length.
    """
    # `drop-record` is record-scoped: it elides an enclosing *document*. A CommitDatabase
    # stores opcodes, not documents — the engine rewrites the value a Document_Set/Update
    # carries, which has no document to drop — so refuse it up front (before any data is
    # touched) with a clear message rather than aborting the transaction mid-replay.
    sites = rewriter.d.drop_record_sites()
    if sites:
        raise ValueError(
            f"[unsupported] drop-record policy at {', '.join(sites)}: a CommitDatabase "
            f"migration rewrites opcode-carried values, which have no document 'record' to "
            f"drop. drop-record is a Database-level policy — use a value-level policy "
            f"(default / map-case) here, or migrate via a Database.")

    # `drop_attachment` deletes a whole persistence partition. Unlike `drop-record` (a per-value
    # trigger whose "record" is a trajectory across commits — materialisation-dependent, refused),
    # dropping an attachment is *static and uniform*: skip **every** opcode that addresses it, and
    # the partition is simply absent from the target (keys are not foreign keys, so nothing
    # dangles). It is therefore materialisation-independent and admissible here exactly as on a
    # Database — under the same shared acknowledgement gate (deleting a whole partition's data is a
    # deliberate, signed-off act).
    _refuse_unacknowledged_attachment_drops(rewriter.d)   # shared with silo 2 (a definition-level gate)

    driver = source.commit_databasing()
    instancing = source.stream_codec_instancing()
    target_driver = target.commit_databasing()
    remap = {}

    # `remap` grows in topological order, so when a commit's values are transformed
    # every commit_id they reference is already known; the engine remaps intra-DAG
    # references at commit_id leaves (external ids fall through, kept verbatim).
    rewriter._commit_id_remap = remap
    copied = set()                                                  # blob-id reprs copied this run
    progress = _Progress(on_progress, len(source.commit_ids()),
                         source.blob_statistics().total_size())

    def ensure_blobs(value):
        """Stream each blob `value` references that the target lacks — copy-on-reference. A
        commit cannot be persisted referencing an absent blob, so this runs before the opcode
        that carries the value. Copies EXACTLY the blobs the rebuilt history references (a
        dropped/retyped blob-field strands its blob → never copied), so the target holds no
        orphan — the CommitDatabase has no blob-delete verb to sweep one anyway."""
        for blob_id in V.Value.collect_blob_ids(value):
            r = blob_id.representation()
            if r not in copied and copy_blob(source, target_driver, blob_id,
                                             on_bytes=progress.add_bytes):
                copied.add(r); progress.blob_done()                # streamed once; shared blobs deduped

    target_driver.begin_transaction(V.Databasing.TRANSACTION_EXCLUSIVE)
    try:
        # the replay is inside the try: any failure (I/O, a raising hook) must roll the
        # transaction back, not leave the exclusive lock dangling (one atomic act).
        for cd in V.CommitData.sort(driver.commit_datas()):
            h = cd.header()
            ctype = h.commit_type()
            parent = h.parent_commit_id()

            if ctype == "Mutations":
                base = (V.CommitStateBuilder.state(target, remap[parent.representation()])
                        if parent.is_valid() else V.CommitStateBuilder.initial_state(target))
                cms = V.CommitMutableState(base)
                # source view for a non-local Class-C hook: the source state at *this* commit
                # — CommitState@C == Database@C, the fully-materialised snapshot the opcodes of
                # C produce. `verify` re-derives every expected value under this same @C view,
                # so wiring it here (not the parent) makes migrate and verify agree by
                # construction; a same-commit reference resolves; the source is immutable so
                # @C is stable and cycle-free (the intra-DAG cycle hazard is on the *target*).
                sview = V.CommitStateBuilder.state(source, h.commit_id())
                rewriter._source_view = sview.attachment_getting()
                try:
                    _replay_opcodes(cd.opcodes(instancing, source.definitions()),
                                    cms.attachment_mutating(), rewriter, source.definitions(),
                                    ensure_blobs)
                except Unrepresentable as e:
                    # An opcode operand with no faithful target image — a `drop-record` policy
                    # (refused up front) or a Class-C hook dropping the value. On a Database that
                    # elides the *document*; here there is no document to elide — an opcode is a
                    # *mutation* in a trace, and dropping one corrupts the document's trajectory
                    # (a dropped update leaves a stale field; a dropped set dangles later opcodes).
                    # Record-scoped loss has no opcode-level meaning: refuse the whole migration
                    # (CD-F1 rolls the transaction back), don't silently skip the opcode.
                    raise ValueError(
                        f"[unsupported] commit {h.commit_id().representation()[:8]}: an opcode's "
                        f"value has no faithful target image (dropped by a Class-C hook). A "
                        f"CommitDatabase migration rewrites a trace of mutations and cannot elide "
                        f"one without corrupting the document's trajectory — record-scoped loss is "
                        f"a Database-level act. Return a representable value (or use a value-closed "
                        f"policy), or migrate via a Database.") from e
                new_id = target.commit_mutations(h.label(), cms)
            elif ctype in ("Merge", "Enable", "Disable"):
                reissue = {"Merge": target.merge_commit, "Enable": target.enable_commit,
                           "Disable": target.disable_commit}[ctype]
                new_id = reissue(h.label(), remap[parent.representation()],
                                 remap[h.target_commit_id().representation()])
            else:
                raise NotImplementedError(f"commit type {ctype!r} not handled")

            remap[h.commit_id().representation()] = new_id
            progress.commit_done()

        target_driver.commit()
    except BaseException:
        # a mid-replay failure (a raising hook, an I/O error, an interrupt) must not leave
        # the exclusive transaction dangling: abort it, so the target is untouched. The DAG
        # replay is one atomic act — all commits re-issued or none.
        if target_driver.in_transaction():
            target_driver.rollback()
        raise
    finally:
        rewriter._commit_id_remap = None
        rewriter._source_view = None
        rewriter._self_key = None

    return {"commits": len(remap), "blobs": len(copied), "remap": remap}


def _rewritten_opcode_value(op, rewriter, tgt_att, path, terminal):
    """The rewritten operand of one opcode — the same rule `translate_opcode` applies, so
    the two agree by construction. `XArray_Insert`/`XArray_Remove` carry no operand (an
    insert's value rides its paired `XArray_Update`); a `Document_Update`'s value is routed
    through the terminal field's retype policy; every other verb is a plain engine rewrite."""
    kind = op.type()
    if kind in ("XArray_Insert", "XArray_Remove"):
        return None
    if kind == "Document_Update":
        return _update_value(rewriter, op, tgt_att, path, terminal)
    return rewriter.value(op.value())


def _link_preserved(src_id, tgt_id, remap):
    """A source→target commit link (a parent or a merge/enable/disable target) is preserved iff
    the target id is the remapped source id — or both are invalid (a root has no parent)."""
    if not src_id.is_valid():
        return not tgt_id.is_valid()
    mapped = remap.get(src_id.representation())
    return mapped is not None and tgt_id.representation() == mapped.representation()


def verify(source, rewriter, target, remap):
    """Prove every opcode was **correctly rewritten** — the per-opcode twin of the Database
    `verify` (a `Document_Set` opcode *is* a Database set).

    Each opcode is a little document — `(attachment, key, semantics, path, value)` — a step of the
    computation trace the evaluator later replays. `migrate` rewrites each opcode through the
    engine (remap the attachment + key, translate the path, rewrite the operand) and re-issues it.
    So `verify` re-derives each source opcode's rewrite **independently** and checks the stored
    target commit carries exactly that. It deliberately does **not** compare materialised
    `CommitState`s: a `CommitState` is a *best-effort, semantically-unreliable* reconstruction — a
    **blind** (`catch`-ing) LWW linearisation of the opcode trace — so materialised-state equality
    is the wrong oracle. The **reliable artefact is the opcode trace**, and that is what `verify`
    checks. (This is also why an aggregate / history-varying non-local hook no longer false-fails:
    a derived field is checked only at the opcode that wrote it, under that commit's own view,
    never re-derived at a later commit that never touched it. The one use of materialisation — the
    `@C` source view a non-local hook reads — is inherent and *symmetric*: `migrate` and `verify`
    read the identical blind reconstruction, so they agree; `verify` does not second-guess it.)

    Two things `migrate` **adds** are therefore what `verify` proves: (1) each opcode correctly
    rewritten, and (2) the **DAG topology preserved** — every commit's parent link, and a
    `Merge`/`Enable`/`Disable`'s target link, is the remapped source id (a root maps invalid →
    invalid). Topology matters because the runtime later *interprets* the target DAG (part 2,
    `CommitStateBuilder`): an isomorphic DAG is interpreted identically, so materialisation
    commutes by construction — but only if the links are intact, which the opcode check alone is
    blind to. `Merge`/`Enable`/`Disable` carry no opcodes — their links are their whole content.
    Then, once: history is preserved (commit count matches, every source commit re-issued) and the
    target holds exactly the referenced blobs (none dangling, none orphaned).

    `remap` (`{source commit repr -> target ValueCommitId}`, from `migrate`) is installed on
    the rewriter so an intra-DAG `commit_id` **leaf** inside an operand is remapped the same
    way the replay remapped it; restored in `finally`. `checked` counts opcodes verified.
    Raises `VerificationError` on the first divergence; returns a summary otherwise.
    """
    src_ids = source.commit_ids()

    # history preserved: same commit count, every source commit re-issued
    if len(target.commit_ids()) != len(src_ids):
        raise VerificationError(
            f"target holds {len(target.commit_ids())} commits, expected {len(src_ids)}")
    for c in src_ids:
        if c.representation() not in remap:
            raise VerificationError(f"source commit {c.representation()[:8]} was not re-issued")

    instancing = source.stream_codec_instancing()
    src_defs, tgt_defs = source.definitions(), target.definitions()
    # index the target commits by id, to fetch the image of each source commit
    tgt_by_id = {cd.header().commit_id().representation(): cd
                 for cd in target.commit_databasing().commit_datas()}

    checked = 0
    referenced = set()
    prev_remap = rewriter._commit_id_remap
    rewriter._commit_id_remap = remap             # remap intra-DAG commit_id leaves
    try:
        for cd in V.CommitData.sort(source.commit_databasing().commit_datas()):
            h = cd.header()
            ctype = h.commit_type()
            crepr = h.commit_id().representation()
            tgt_cd = tgt_by_id[remap[crepr].representation()]
            tgt_h = tgt_cd.header()
            if tgt_h.commit_type() != ctype:
                raise VerificationError(
                    f"commit {crepr[:8]}: type mismatch — {tgt_h.commit_type()} != {ctype}")

            # topology preserved (part 1 — the DAG the runtime later interprets): every commit's
            # parent link — and a Merge/Enable/Disable's target link — must be the remapped source
            # id (or invalid → invalid at a root). Checking the opcode rewrite alone is blind to a
            # mis-threaded parent when two commits carry the same opcodes.
            if not _link_preserved(h.parent_commit_id(), tgt_h.parent_commit_id(), remap):
                raise VerificationError(f"commit {crepr[:8]}: parent link not preserved")
            if ctype != "Mutations":
                # Merge/Enable/Disable carry no opcodes — the target link is their whole content.
                if not _link_preserved(h.target_commit_id(), tgt_h.target_commit_id(), remap):
                    raise VerificationError(
                        f"commit {crepr[:8]}: {ctype} target link not preserved")
                continue

            # the commit's own materialised source state — a non-local hook must re-derive
            # under the same @C snapshot migrate used, or the check diverges.
            rewriter._source_view = (
                V.CommitStateBuilder.state(source, h.commit_id()).attachment_getting())
            # opcodes addressing a dropped attachment were not re-issued — filter them from the
            # source side so the two opcode streams align (silo 2 skips its dropped documents).
            src_ops = [o for o in cd.opcodes(instancing, src_defs)
                       if not _addresses_dropped_attachment(o, src_defs, rewriter.d.dropped_attachments)]
            tgt_ops = tgt_cd.opcodes(instancing, tgt_defs)
            if len(src_ops) != len(tgt_ops):
                raise VerificationError(
                    f"commit {crepr[:8]}: {len(tgt_ops)} opcodes, expected {len(src_ops)}")

            for so, to in zip(src_ops, tgt_ops):
                kind = so.type()
                if to.type() != kind:
                    raise VerificationError(
                        f"commit {crepr[:8]}: opcode type mismatch — {to.type()} != {kind}")
                s_att, s_key = so.arguments(src_defs)[:2]
                t_att, t_key = to.arguments(tgt_defs)[:2]
                rewriter._self_key = s_key                # record identity for aggregate hooks
                if rewriter.attachment(s_att).identifier() != t_att.identifier():
                    raise VerificationError(
                        f"commit {crepr[:8]}: {kind} attachment mismatch")
                if rewriter.value(s_key) != t_key:
                    raise VerificationError(
                        f"commit {crepr[:8]}: {kind} key mismatch")

                if kind == "Document_Set":
                    path, terminal = None, None
                else:
                    path, terminal = translate_path(rewriter, s_att.document_type(), so.path())
                    if to.path() != path:
                        raise VerificationError(
                            f"commit {crepr[:8]}: {kind} path mismatch")

                exp_val = _rewritten_opcode_value(so, rewriter, t_att, path, terminal)
                if exp_val is not None:
                    if to.value() != exp_val:
                        raise VerificationError(
                            f"commit {crepr[:8]}: {kind} value mismatch")
                    referenced |= V.Value.collect_blob_ids(exp_val)
                checked += 1
    finally:
        rewriter._commit_id_remap = prev_remap
        rewriter._source_view = None
        rewriter._self_key = None

    # blob integrity: the target holds EXACTLY the blobs its opcodes reference — none dangling
    # (a referenced blob absent), none leftover (an orphan copy-on-reference should never make).
    present = target.blob_ids()
    dangling = referenced - present
    if dangling:
        raise VerificationError(f"{len(dangling)} referenced blob(s) absent from target")
    leftover = present - referenced
    if leftover:
        raise VerificationError(f"{len(leftover)} orphan blob(s) present in target")

    return {"commits": len(src_ids), "checked": checked, "referenced_blobs": len(referenced)}


def dry_run(source, rewriter, *, max_samples=5):
    """Exercise the rewriter over every opcode of `source` **without writing** — no target, no
    blob copy, no transaction. The CommitDatabase parity of the Database `dry_run`, and the
    *inform* step of the loss model.

    Each opcode is rewritten under its commit's own `@C` source view (so a non-local hook runs
    exactly as in `migrate`), with a `DiagnosticSink` attached: it records every Class-B policy
    that actually bites and a bounded `before → after` sample. The CommitDatabase-specific part:
    a **record-scoped** loss — a `drop-record` policy, or a Class-C hook that drops a value — has
    no opcode-level meaning here (dropping one mutation corrupts the document's trajectory), so
    `migrate` refuses it; `dry_run` **collects** those would-abort sites up front instead of the
    operator meeting them mid-replay. It never writes and never raises on such a site.

    Returns `{commits, opcodes, referenced_blobs, stranded_blobs, unrepresentable, diagnostics}`:
    `referenced_blobs` would be copied, `stranded_blobs` are source blobs no rewritten opcode
    references (dropped by the migration), `unrepresentable` lists the would-abort sites (static
    `drop-record` policy sites + opcodes a hook dropped), `diagnostics` is the sink's report
    (render with `format_report`). No commit_id remap exists yet (nothing is issued), so
    intra-DAG `commit_id` leaves are previewed verbatim."""
    sink = DiagnosticSink(max_samples=max_samples)
    instancing = source.stream_codec_instancing()
    src_defs = source.definitions()
    commits = opcodes = 0
    referenced = set()
    # statically-known record-scoped losses would abort migrate too — surface them alongside the
    # dynamically-discovered hook drops, in one would-abort list.
    unrepresentable = [f"{s} (drop-record policy)" for s in sorted(rewriter.d.drop_record_sites())]

    rewriter._sink = sink
    rewriter._commit_id_remap = {}                     # no target ids yet → commit_ids kept verbatim
    try:
        for cd in V.CommitData.sort(source.commit_databasing().commit_datas()):
            h = cd.header()
            if h.commit_type() != "Mutations":         # Merge/Enable/Disable carry no opcodes
                continue
            commits += 1
            rewriter._source_view = (
                V.CommitStateBuilder.state(source, h.commit_id()).attachment_getting())
            for op in cd.opcodes(instancing, src_defs):
                if _addresses_dropped_attachment(op, src_defs, rewriter.d.dropped_attachments):
                    continue                           # dropped attachment — not re-issued
                opcodes += 1
                kind = op.type()
                att, key = op.arguments(src_defs)[:2]
                rewriter._self_key = key
                tgt_att = rewriter.attachment(att)
                try:
                    if kind == "Document_Set":
                        v = rewriter.value(op.value())
                    else:
                        path, terminal = translate_path(rewriter, att.document_type(), op.path())
                        v = _rewritten_opcode_value(op, rewriter, tgt_att, path, terminal)
                    if v is not None:
                        referenced |= V.Value.collect_blob_ids(v)
                except Unrepresentable:
                    unrepresentable.append(
                        f"commit {h.commit_id().representation()[:8]} {kind} "
                        f"{key.representation()}")
    finally:
        rewriter._sink = None
        rewriter._commit_id_remap = None
        rewriter._source_view = None
        rewriter._self_key = None

    stranded = source.blob_ids() - referenced
    return {"commits": commits, "opcodes": opcodes,
            "referenced_blobs": len(referenced), "stranded_blobs": len(stranded),
            "unrepresentable": unrepresentable, "diagnostics": sink.report()}


# a stable handle to verify() — run()'s `verify=` flag would otherwise shadow it in-body
_verify = verify


def run(source_path, build_directives, target_path, verify=False, on_progress=None):
    """Open the source `CommitDatabase` read-only, build the directives against its live
    schema, and replay it into a fresh target `CommitDatabase`. The source is never
    modified. Mirrors the Database `run`.

    With `verify=True`, prove the rebuild is a faithful image of the whole history
    before closing it — every opcode correctly rewritten + the topology preserved (`verify`)
    — and add `"verification"` to the returned summary. `on_progress` is forwarded to
    `migrate` (a `CommitMigrationProgress` per step)."""
    source = V.CommitDatabase.open(source_path, readonly=True)
    try:
        directives = build_directives(source.definitions())
        rewriter, target_defs = DefinitionsRewriter.from_directives(
            source.definitions(), directives)
        target = V.CommitDatabase.create(target_path)
        ok = False
        try:
            target.extend_definitions(target_defs.const())   # manages its own transaction
            info = migrate(source, rewriter, target, on_progress=on_progress)
            summary = {"commits": info["commits"], "blobs": info["blobs"]}   # operator summary
            if verify:
                summary["verification"] = _verify(
                    source, rewriter, target, info["remap"])
            ok = True
            return summary
        finally:
            target.close()
            if not ok:                                       # discard the half-written target —
                _remove_db_file(target_path)                 # never leave a partial artefact behind
    finally:
        source.close()
