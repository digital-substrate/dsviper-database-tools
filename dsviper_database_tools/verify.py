"""Round-trip verification — a migration tool must prove its own correctness.

`verify_migration(source, transformer, target)` re-derives the expected target from
the (read-only) source through the *same* transformer and asserts the target matches:

  * every kept document equals `transformer.value(source_doc)` (value equality is
    content-based, so a storage round-trip that altered a value is caught);
  * every `drop-record` document is **absent** from the target;
  * the target holds **no spurious** document beyond the kept set;
  * every blob a target document references is **present** (no dangling reference).

Raises `VerificationError` on the first divergence; returns a summary otherwise.
"""

import dsviper as V

from .rewrite import DropRecord


class VerificationError(Exception):
    """A migrated target diverges from the faithful transformation of its source."""


def verify_migration(source, transformer, target):
    checked = dropped = 0
    for att in source.definitions().attachments():
        tgt_att = transformer.attachment(att)
        keys = source.keys(att)
        for i in range(keys.size()):
            key = keys.at(i, encoded=False)
            sdoc = source.get(att, key)
            if sdoc.is_nil():
                continue
            tgt_key = transformer.value(key)
            try:
                expected = transformer.value(sdoc.unwrap(encoded=False))
            except DropRecord:
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

    # no spurious document beyond the kept set
    target_docs = sum(target.keys(a).size() for a in target.definitions().attachments())
    if target_docs != checked:
        raise VerificationError(f"target holds {target_docs} documents, expected {checked}")

    # referential integrity: every referenced blob is present (no dangling reference)
    referenced = set()
    for att in target.definitions().attachments():
        keys = target.keys(att)
        for i in range(keys.size()):
            doc = target.get(att, keys.at(i, encoded=False))
            if not doc.is_nil():
                referenced |= V.Value.collect_blob_ids(doc.unwrap(encoded=False))
    dangling = referenced - target.blob_ids()
    if dangling:
        raise VerificationError(f"{len(dangling)} referenced blob(s) absent from target")

    return {"checked": checked, "dropped": dropped, "referenced_blobs": len(referenced)}
