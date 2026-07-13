"""Shared blob byte-copy — stream a blob source → target in 64 MB chunks, preserving
its content-addressed id (stable across databases). Used by both the `Database` and
`CommitDatabase` migration loops.

`source` is a `Database` or `CommitDatabase` (both expose `blob_info` + `read_blob`);
`target_databasing` is `target.databasing()` or `target.commit_databasing()` (both
expose `create_zero_blob` / `write_blob` / `freeze_blob`).
"""

_CHUNK = 64 * 1024 * 1024        # stream large blobs, never materialised whole


def copy_blob(source, target_databasing, blob_id):
    """Stream one blob's bytes source → target, preserving its id. Returns True if
    copied, False if the source lacks it (an incoherent reference — skipped)."""
    info = source.blob_info(blob_id)
    if info is None:
        return False
    size = info.size()
    target_databasing.create_zero_blob(blob_id, info.blob_layout(), size)
    offset = 0
    while offset < size:
        chunk = min(_CHUNK, size - offset)
        target_databasing.write_blob(blob_id, source.read_blob(blob_id, chunk, offset), offset)
        offset += chunk
    target_databasing.freeze_blob(blob_id)
    return True


def copy_blobs(source, target_databasing, blob_ids):
    """Stream a set of blobs; returns the count actually copied."""
    return sum(copy_blob(source, target_databasing, b) for b in blob_ids)
