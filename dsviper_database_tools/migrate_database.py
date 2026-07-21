"""Database migration — the applied read-old / write-new loop.

The finality: rebuild `Base(A)` (read-only) into a fresh `Base(B)` under the target
registry. Never an in-place ALTER (a type's runtimeId is its storage key); the old
artefact stays as rollback.

The loop mirrors the runtime's schema-invariant `DatabaseCopier`, with two twists:
documents are **transformed** (not copied verbatim), and — because a schema change can
strand a blob (a dropped `blob_id` field, a dropped record) — a **mark-sweep** reclaims
the now-unreferenced blobs at the end. Blobs are content-addressed (their id is a hash
of the bytes), so a blob's id survives the copy verbatim.

This module is silo 2 in full: the loop (`migrate`), its round-trip self-check
(`verify` / `VerificationError`), and the `run` entry point.
"""

import contextlib
import os
from collections import namedtuple

import dsviper as V

from .blobs import copy_blob
from .rewrite import DefinitionsRewriter, Unrepresentable, DiagnosticSink
from .rewrite.engine import _att_hit


class VerificationError(Exception):
    """A migrated target diverges from the faithful transformation of its source."""


# A progress snapshot handed to a `migrate(..., on_progress=)` callback. The unit that
# matters is BYTES (migration time ∝ blob bytes, not counts): `bytes_copied` climbs against
# `bytes_total` (the source's total blob bytes — an upper bound, exact when nothing is
# orphaned). `attachment_index`/`attachment_count` give a cheap structural position; the
# document count is a plain tally.
MigrationProgress = namedtuple(
    "MigrationProgress",
    ["documents", "blobs", "bytes_copied", "bytes_total",
     "attachment", "attachment_index", "attachment_count"])


class _Progress:
    """Accumulates progress and fires `on_progress(MigrationProgress)` on each change. A
    `None` callback makes every method accumulate silently (no fire). Bytes advance per
    streamed chunk — so the bar moves even through one multi-gigabyte blob."""
    __slots__ = ("_cb", "documents", "blobs", "bytes_copied", "bytes_total",
                 "attachment", "attachment_index", "attachment_count")

    def __init__(self, on_progress, bytes_total, attachment_count):
        self._cb = on_progress
        self.bytes_total = bytes_total
        self.attachment_count = attachment_count
        self.documents = self.blobs = self.bytes_copied = 0
        self.attachment = None
        self.attachment_index = 0

    def _fire(self):
        if self._cb is not None:
            self._cb(MigrationProgress(
                self.documents, self.blobs, self.bytes_copied, self.bytes_total,
                self.attachment, self.attachment_index, self.attachment_count))

    def enter_attachment(self, name, index):
        self.attachment, self.attachment_index = name, index
        self._fire()

    def add_bytes(self, n):                            # per streamed chunk — drives the byte bar
        self.bytes_copied += n
        self._fire()

    def blob_done(self):
        self.blobs += 1

    def document_done(self):
        self.documents += 1
        self._fire()


def _carriers_under_container(document_types):
    """Reprs of the struct/enum types reachable — in some document type graph — through a
    CONTAINER element (vector/set/map/xarray), i.e. at a position of element *multiplicity*.
    Struct fields, `Optional`, `Tuple` and `Variant` are multiplicity-1 and preserve the
    'this is the record' scope; a container element breaks it (one among many)."""
    under = set()
    seen = set()                                       # (struct repr, nested) — breaks ref cycles

    def walk(t, nested):
        tc = t.type_code()
        if tc == "struct":
            s = V.TypeStructure.cast(t)
            r = s.representation()
            if nested:
                under.add(r)
            if (r, nested) in seen:
                return
            seen.add((r, nested))
            for f in s.fields():
                walk(f.type(), nested)
        elif tc == "enum":
            if nested:
                under.add(t.representation())          # enums carry no further named types
        elif tc == "optional":
            walk(V.TypeOptional.cast(t).element_type(), nested)      # multiplicity 1
        elif tc == "tuple":
            for x in V.TypeTuple.cast(t).types():
                walk(x, nested)                                      # fixed product
        elif tc == "variant":
            for x in V.TypeVariant.cast(t).types():
                walk(x, nested)                                      # one arm
        elif tc == "vector":
            walk(V.TypeVector.cast(t).element_type(), True)          # container → multiplicity many
        elif tc == "set":
            walk(V.TypeSet.cast(t).element_type(), True)
        elif tc == "xarray":
            walk(V.TypeXArray.cast(t).element_type(), True)
        elif tc == "map":
            m = V.TypeMap.cast(t)
            walk(m.key_type(), True)
            walk(m.element_type(), True)
        # key / any / concept / club / primitives: no statically-visible drop-record carrier
        #   (a key's element is a concept leaf; an `Any`'s content is dynamic, invisible here)

    for dt in document_types:
        walk(dt, False)
    return under


def _refuse_ambiguous_drop_record(source, directives):
    """Refuse — up front, before any data is touched — a `drop-record` policy whose bite
    site sits UNDER a container in some document, where 'drop the enclosing record' is
    ambiguous (the value is one element among many, not the record itself).

    `drop-record` is record-scoped (see `REWRITE.md` §3–4): on a `Database` the record is the
    document, so the policy is admissible only at *document scope* — a document field, or
    one reached through nested structs / optionals / tuples / variants (multiplicity 1). A
    value of container multiplicity has no unambiguous record to elide. This is the
    Database's own admissible-scope check, symmetric to the `CommitDatabase`'s blanket
    refusal (which has no document record at all)."""
    retype_carriers = {}                               # struct repr -> [field]
    for s, fields in directives.retyped_fields.items():
        for f, (_t, p) in fields.items():
            if p == "drop-record":
                retype_carriers.setdefault(s, []).append(f)
    enum_carriers = {}                                 # enum repr -> [case]
    for e, cases in directives.removed_cases.items():
        for c, p in cases.items():
            if p == "drop-record":
                enum_carriers.setdefault(e, []).append(c)
    if not retype_carriers and not enum_carriers:
        return

    under = _carriers_under_container(
        [a.document_type() for a in source.definitions().attachments()])
    bad = [f"{s}.{f}" for s, fs in retype_carriers.items() if s in under for f in fs]
    bad += [f"{e}::{c}" for e, cs in enum_carriers.items() if e in under for c in cs]
    if bad:
        raise ValueError(
            f"[unsupported] drop-record at {', '.join(sorted(bad))}: the value sits under a "
            f"container (vector/set/map/xarray) in a document — 'drop the record' is ambiguous "
            f"there (one element among many). drop-record is admissible only at document scope "
            f"(reached through structs/optionals, multiplicity 1); use a value-closed policy "
            f"(default / map-case) for a nested value, or restructure the schema.")


def _refuse_unacknowledged_drops(directives):
    """Refuse any `drop-record` policy unless the migration has explicitly signed off that
    it may **delete whole documents** (`directives.accept_document_drops()`). Dropping a
    document is record-scoped loss — categorically graver than any value-closed policy — so
    it demands a deliberate, separate act, not a field policy that reads like `saturate`.

    `dry_run` intentionally does NOT call this: it is the tool that *informs* the decision
    (how many / which documents would drop), which the operator weighs before signing off
    and running `migrate`. This closes the loop: identify (plan) → inform (dry-run) →
    acknowledge (here) → decide."""
    if directives.drop_record_sites() and not directives.document_drops_accepted:
        raise ValueError(
            f"[unacknowledged] this migration decrees drop-record at "
            f"{', '.join(sorted(directives.drop_record_sites()))} — it may DELETE whole "
            f"documents (a record has no faithful image → the enclosing document is elided). "
            f"Run migrate_database.dry_run to see how many/which would drop, then call "
            f"directives.accept_document_drops() to authorize it explicitly.")


def _refuse_unacknowledged_attachment_drops(directives):
    """Refuse any `drop_attachment` unless the migration has signed off that it may **delete
    whole attachments** (`directives.accept_attachment_drops()`). Dropping an attachment
    deletes *every* document it holds — a deliberate, separate act, mirroring the drop-record
    acknowledgement. `dry_run` does NOT call this (it informs, before the sign-off)."""
    if directives.dropped_attachments and not directives.attachment_drops_accepted:
        raise ValueError(
            f"[unacknowledged] this migration drops the attachment(s) "
            f"{', '.join(sorted(directives.dropped_attachments))} — it will DELETE every "
            f"document they hold. Run migrate_database.dry_run to preview, then call "
            f"directives.accept_attachment_drops() to authorize it explicitly.")


@contextlib.contextmanager
def _source_snapshot(source):
    """Hold ONE read transaction on the source for the whole read, so every read — documents,
    keys, blobs, and a non-local hook's source view — sees a single consistent snapshot. The
    source is opened read-only but is **not immutable**: another process may `del` / `del_blob`
    concurrently, and without an enclosing transaction each read takes its own lock, so a
    referenced blob could vanish between reading a document and copying it. A DEFERRED (read)
    transaction takes a shared lock on first read; `rollback` releases it (nothing was written).
    A `CommitDatabase` needs none — its `CommitState` is immutable by construction."""
    source.begin_transaction()
    try:
        yield
    finally:
        if source.in_transaction():
            source.rollback()


def _transform_pass(source, rewriter, sink, diag=None, progress=None):
    """Read every source document, rewrite it, and hand each kept result to
    `sink(tgt_att, tgt_key, tgt_doc)`; a `drop-record` policy that fires skips the
    document. Returns `(documents, dropped, referenced_blobs)`. This read+rewrite core is
    shared by `migrate` (sink = `target.set`) and `dry_run` (sink = a no-op) — the payoff
    of the I/O-free engine: the same pass runs with or without a target to write to.

    `diag`, if given, is a diagnostic sink (`DiagnosticSink`) the engine notifies whenever
    a Class-B policy bites; it is attached only for the duration of the pass so a later
    `migrate`/`verify` on the same rewriter is not observed.

    The **source view** — `source.attachment_getting()`, a read-only handle over `Base(A)` —
    is wired for the duration too, so a non-local Class-C hook may fetch *another* source
    document (`ctx.attachment_getting.get(attachment, key)`). It reads `Base(A)` (immutable,
    fully materialised): no ordering dependency, no cycle hazard from the target being built."""
    documents = dropped = 0
    referenced = set()
    rewriter._sink = diag
    rewriter._source_view = source.attachment_getting()
    atts = [a for a in source.definitions().attachments()
            if not _att_hit(rewriter.d.dropped_attachments, a)]              # dropped: deleted
    try:
        for att_i, att in enumerate(atts):
            if progress is not None:
                progress.enter_attachment(att.identifier().split(".")[-1], att_i)
            tgt_att = rewriter.attachment(att)
            keys = source.keys(att)
            for i in range(keys.size()):
                key = keys.at(i, encoded=False)
                doc = source.get(att, key)                 # ValueOptional
                if doc.is_nil():
                    continue
                rewriter._self_key = key                   # record identity for aggregate hooks
                try:
                    tgt_doc = rewriter.value(doc.unwrap(encoded=False))
                except Unrepresentable:
                    dropped += 1                           # policy: skip this document
                    continue
                sink(tgt_att, rewriter.value(key), tgt_doc)
                referenced |= V.Value.collect_blob_ids(tgt_doc)
                documents += 1
                if progress is not None:
                    progress.document_done()
    finally:
        rewriter._sink = None
        rewriter._source_view = None
        rewriter._self_key = None
    return documents, dropped, referenced


def migrate(source, rewriter, target, on_progress=None):
    """Rewrite every document of `source` into `target` through `rewriter`.

    Assumes `target` has already been extended with `rewriter`'s target definitions. Owns
    its own exclusive transaction (the SQLite auto-transaction-avoidance win). Blobs are
    copied **on reference**: just before a target document is written, every blob it
    references that the target lacks is streamed over — so exactly the referenced blobs are
    copied, never an orphan, and there is nothing to sweep. A document cannot be persisted
    referencing an absent blob, so blob-before-its-document is the required (and sufficient)
    order. A `drop-record` policy that fires skips the document. Returns a transfer summary.

    `on_progress`, if given, is called with a `MigrationProgress` as work advances — bytes
    per streamed chunk (against the source's total blob bytes), documents, and attachment
    position — for a progress bar over the dominant cost (blob I/O)."""
    _refuse_ambiguous_drop_record(source, rewriter.d)  # coherence: no ambiguous record scope
    _refuse_unacknowledged_drops(rewriter.d)           # authorization: document drops signed off
    _refuse_unacknowledged_attachment_drops(rewriter.d)  # both fail closed, before any data is touched
    copied = set()                                     # blob-id reprs copied this run (target starts fresh)
    with _source_snapshot(source):                     # one consistent view of the mutable source
        live_atts = [a for a in source.definitions().attachments()
                     if not _att_hit(rewriter.d.dropped_attachments, a)]
        progress = _Progress(on_progress, source.blob_statistics().total_size(), len(live_atts))
        target.begin_transaction(V.Databasing.TRANSACTION_EXCLUSIVE)

        def sink(tgt_att, tgt_key, tgt_doc):
            for blob_id in V.Value.collect_blob_ids(tgt_doc):
                r = blob_id.representation()
                if r not in copied and copy_blob(source, target.databasing(), blob_id,
                                                 on_bytes=progress.add_bytes):
                    copied.add(r); progress.blob_done()    # streamed once; shared blobs deduped
            target.set(tgt_att, tgt_key, tgt_doc)          # blob(s) present → the document is writable

        try:
            documents, dropped, _referenced = _transform_pass(source, rewriter, sink, progress=progress)
            target.commit()
        except BaseException:
            # a mid-migration failure (a raising hook, an I/O error, an interrupt) must not leave
            # the exclusive transaction dangling: abort it, so the target is untouched.
            if target.in_transaction():
                target.rollback()
            raise
    return {"documents": documents, "dropped": dropped, "blobs": len(copied)}


def dry_run(source, rewriter, *, max_samples=5):
    """Exercise the rewriter over every document of `source` **without writing anything**
    — no target, no blob copy, no transaction. The dividend of the layered design and the
    I/O-free engine: prove the rewrite holds, or preview exactly which documents a
    `drop-record` policy would skip and which blobs it would strand, at the cost of one
    read-only pass.

    Beyond the aggregate counts, a `DiagnosticSink` records — per site — every Class-B
    policy that actually bit and a bounded sample of the `before → after` values (the
    *inform* surface: real loss on real data, not just what the static plan flagged as
    possible). Returns `{documents, dropped, referenced_blobs, orphans, diagnostics}`;
    `diagnostics` is the sink's report (render it with `format_report`)."""
    _refuse_ambiguous_drop_record(source, rewriter.d)  # same admissible-scope check as migrate
    sink = DiagnosticSink(max_samples=max_samples)
    with _source_snapshot(source):                     # a consistent preview of the mutable source
        documents, dropped, referenced = _transform_pass(source, rewriter, lambda *a: None, diag=sink)
        orphans = source.blob_ids() - referenced
    return {"documents": documents, "dropped": dropped,
            "referenced_blobs": len(referenced), "orphans": len(orphans),
            "diagnostics": sink.report()}


def verify(source, rewriter, target):
    """Prove `target` is the faithful image of `source` under `rewriter`: every kept
    document equals `rewriter.value(source_doc)` (content-based equality, so a storage
    round-trip that drifted a value is caught), every dropped record is absent, no
    spurious document, no dangling blob. Raises `VerificationError` on divergence;
    returns a summary otherwise."""
    checked = dropped = 0
    # verify re-derives the expected target through the SAME engine wiring `migrate` uses, or
    # its self-check has blind spots exactly where the engine is most powerful: a non-local hook
    # needs the source view, an aggregate hook needs the record's own key. Mirror both.
    rewriter._source_view = source.attachment_getting()
    try:
        for att in source.definitions().attachments():
            if _att_hit(rewriter.d.dropped_attachments, att):
                continue                                   # dropped attachment: no target image
            tgt_att = rewriter.attachment(att)
            keys = source.keys(att)
            for i in range(keys.size()):
                key = keys.at(i, encoded=False)
                sdoc = source.get(att, key)
                if sdoc.is_nil():
                    continue
                rewriter._self_key = key                   # record identity for aggregate hooks
                tgt_key = rewriter.value(key)
                try:
                    expected = rewriter.value(sdoc.unwrap(encoded=False))
                except Unrepresentable:
                    if target.has(tgt_att, tgt_key):
                        raise VerificationError(
                            f"dropped record present in target: {tgt_key.representation()}")
                    dropped += 1
                    continue
                got = target.get(tgt_att, tgt_key)
                if got.is_nil():
                    raise VerificationError(f"missing target document: {tgt_key.representation()}")
                if got.unwrap(encoded=False) != expected:
                    raise VerificationError(f"value mismatch at {tgt_key.representation()}")
                checked += 1
    finally:
        rewriter._source_view = None
        rewriter._self_key = None

    # no spurious document beyond the kept set
    target_docs = sum(target.keys(a).size() for a in target.definitions().attachments())
    if target_docs != checked:
        raise VerificationError(f"target holds {target_docs} documents, expected {checked}")

    # blob integrity: the target holds EXACTLY the referenced blobs — none dangling (a
    # referenced blob absent), none leftover (an orphan the mark-sweep missed).
    referenced = set()
    for att in target.definitions().attachments():
        keys = target.keys(att)
        for i in range(keys.size()):
            doc = target.get(att, keys.at(i, encoded=False))
            if not doc.is_nil():
                referenced |= V.Value.collect_blob_ids(doc.unwrap(encoded=False))
    present = target.blob_ids()
    dangling = referenced - present
    if dangling:
        raise VerificationError(f"{len(dangling)} referenced blob(s) absent from target")
    leftover = present - referenced
    if leftover:
        raise VerificationError(f"{len(leftover)} orphan blob(s) not swept from target")

    return {"checked": checked, "dropped": dropped, "referenced_blobs": len(referenced)}


# a stable handle to verify() — run()'s `verify=` flag would otherwise shadow it in-body
_verify = verify


def run(source_path, build_directives, target_path, verify=False, on_progress=None):
    """Open the source read-only, build the directives against its live schema,
    transform, and write a fresh target database. The source is never modified.

    With `verify=True`, prove the target is a faithful image before closing it — the
    tool checks its own correctness in the same run (adds `"verification"` to the info).
    `on_progress` is forwarded to `migrate` (a `MigrationProgress` per step)."""
    source = V.Database.open(source_path, readonly=True)
    try:
        directives = build_directives(source.definitions())
        rewriter, target_defs = DefinitionsRewriter.from_directives(
            source.definitions(), directives)
        target = V.Database.create(target_path)
        ok = False
        try:
            target.extend_definitions(target_defs.const())   # manages its own transaction
            info = migrate(source, rewriter, target, on_progress=on_progress)
            if verify:
                info["verification"] = _verify(source, rewriter, target)
            ok = True
            return info
        finally:
            target.close()
            if not ok:                                       # discard the half-written target —
                _remove_db_file(target_path)                 # never leave a partial artefact behind
    finally:
        source.close()


def _remove_db_file(path):
    """Best-effort delete of a database file and its SQLite sidecars (`-wal` / `-shm` /
    `-journal`) — used to discard a target that a failed `run` left half-written."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass                                             # absent or unremovable — nothing to do
