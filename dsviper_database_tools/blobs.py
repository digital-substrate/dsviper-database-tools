"""Shared blob byte-copy — stream a blob source → target in 64 MB chunks, preserving
its content-addressed id (stable across databases). Used by both the `Database` and
`CommitDatabase` migration loops.

`source` is a `Database` or `CommitDatabase` (both expose `blob_info` + `read_blob`);
`target_databasing` is `target.databasing()` or `target.commit_databasing()` (both
expose `create_zero_blob` / `write_blob` / `freeze_blob`).
"""

_CHUNK = 64 * 1024 * 1024        # stream large blobs, never materialised whole


def copy_blob(source, target_databasing, blob_id, on_bytes=None):
    """Stream one blob's bytes source → target, preserving its id. Returns True if
    copied, False if the source lacks it (an incoherent reference — skipped). `on_bytes`,
    if given, is called with each chunk's byte count as it is written — so a caller can
    show byte-level progress even through a single multi-gigabyte blob."""
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
        if on_bytes is not None:
            on_bytes(chunk)
    target_databasing.freeze_blob(blob_id)
    return True
