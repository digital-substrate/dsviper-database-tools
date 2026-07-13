"""Database migration — the applied read-old / write-new loop.

The finality: rebuild `Base(A)` (read-only) into a fresh `Base(B)` under the target
registry. Never an in-place ALTER (a type's runtimeId is its storage key); the old
artefact stays as rollback.

The loop mirrors the runtime's schema-invariant `DatabaseCopier`, with two twists:
documents are **transformed** (not copied verbatim), and — because a schema change can
strand a blob (a dropped `blob_id` field, a dropped record) — a **mark-sweep** reclaims
the now-unreferenced blobs at the end. Blobs are content-addressed (their id is a hash
of the bytes), so a blob's id survives the copy verbatim.
"""

import dsviper as V

from .blobs import copy_blobs
from .rewrite import DefinitionsTransformer, DropRecord
from .verify import verify_migration


def migrate_database(source, transformer, target):
    """Rewrite every document of `source` into `target` through `transformer`.

    Assumes `target` has already been extended with `transformer`'s target
    definitions. Owns its own exclusive transaction (blobs before documents, one
    unit). A `drop-record` policy that fires on a document skips it. Returns a
    transfer summary.
    """
    all_blobs = source.blob_ids()

    target.begin_transaction(V.Databasing.TRANSACTION_EXCLUSIVE)

    blobs = copy_blobs(source, target.databasing(), all_blobs)     # blobs first (streamed)

    documents = dropped = 0
    referenced = set()
    for att in source.definitions().attachments():
        tgt_att = transformer.attachment(att)
        keys = source.keys(att)
        for i in range(keys.size()):
            key = keys.at(i, encoded=False)
            doc = source.get(att, key)                 # ValueOptional
            if doc.is_nil():
                continue
            try:
                tgt_doc = transformer.value(doc.unwrap(encoded=False))
            except DropRecord:
                dropped += 1                           # policy: skip this document
                continue
            target.set(tgt_att, transformer.value(key), tgt_doc)
            referenced |= V.Value.collect_blob_ids(tgt_doc)
            documents += 1

    orphans = all_blobs - referenced                   # stranded by the schema change
    for blob_id in orphans:
        target.del_blob(blob_id)

    target.commit()
    return {"documents": documents, "dropped": dropped,
            "blobs": blobs, "orphans_swept": len(orphans)}


def run_migration(source_path, build_directives, target_path, verify=False):
    """Open the source read-only, build the directives against its live schema,
    transform, and write a fresh target database. The source is never modified.

    With `verify=True`, prove the target is a faithful image before closing it — the
    tool checks its own correctness in the same run (adds `"verification"` to the info)."""
    source = V.Database.open(source_path, readonly=True)
    try:
        directives = build_directives(source.definitions())
        transformer, target_defs = DefinitionsTransformer.from_directives(
            source.definitions(), directives)
        target = V.Database.create(target_path)
        try:
            target.extend_definitions(target_defs.const())   # manages its own transaction
            info = migrate_database(source, transformer, target)
            if verify:
                info["verification"] = verify_migration(source, transformer, target)
            return info
        finally:
            target.close()
    finally:
        source.close()
