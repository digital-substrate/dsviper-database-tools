#!/usr/bin/env python3
"""Real-scale soak — rebuild a real Database verbatim and prove the round-trip.

An **identity** migration (empty directives): read a real source read-only, rewrite every
document through the engine (identity), stream every referenced blob to a fresh target, and
verify the target is a faithful image. It exercises the whole loop — the value walk on real
data, copy-on-reference blob streaming, the source snapshot, and `verify` — at real scale,
where the cost is blob VOLUME (GB), not document count.

    python3 soak.py <source.db> [target.db]

`source` is opened read-only and never modified. `target` (a temp file if omitted) is written
fresh and deleted afterwards. Prints throughput and a byte-progress trace over the dominant cost.
"""
import os
import sys
import tempfile
import time

import dsviper as V

from dsviper_database_tools import TransformationDirectives, DefinitionsRewriter, migrate_database


def _remove(path):
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def soak(source_path, target_path=None):
    owns_target = target_path is None
    if owns_target:
        target_path = os.path.join(tempfile.gettempdir(), "soak_target.db")
    _remove(target_path)

    source = V.Database.open(source_path, readonly=True)
    try:
        st = source.blob_statistics()
        docs = sum(source.keys(a).size() for a in source.definitions().attachments())
        atts = len(source.definitions().attachments())
        print(f"source: {docs} docs, {atts} attachments, {st.count()} blobs, "
              f"{st.total_size() / 1e9:.2f} GB (max blob {st.max_size() / 1e6:.0f} MB)")

        rewriter, target_defs = DefinitionsRewriter.from_directives(
            source.definitions(), TransformationDirectives())          # identity
        target = V.Database.create(target_path)
        try:
            target.extend_definitions(target_defs.const())

            mark = [500e6]
            def on_progress(p):
                if p.bytes_copied >= mark[0]:
                    print(f"  ... {p.bytes_copied / 1e9:.2f}/{p.bytes_total / 1e9:.2f} GB "
                          f"({p.documents} docs, att {p.attachment_index + 1}/{p.attachment_count})")
                    mark[0] += 500e6

            t0 = time.time()
            info = migrate_database.migrate(source, rewriter, target, on_progress=on_progress)
            t1 = time.time()
            gb = st.total_size() / 1e9
            rate = gb * 1000 / (t1 - t0) if t1 > t0 else 0
            print(f"migrate: {info}  |  {t1 - t0:.1f}s @ {rate:.0f} MB/s")

            v = migrate_database.verify(source, rewriter, target)
            faithful = v["checked"] == info["documents"]
            print(f"verify: {v} in {time.time() - t1:.1f}s -> "
                  f"{'FAITHFUL' if faithful else 'MISMATCH'}")
            return 0 if faithful else 1
        finally:
            target.close()
            if owns_target:
                _remove(target_path)
    finally:
        source.close()


if __name__ == "__main__":
    if not 2 <= len(sys.argv) <= 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(soak(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None))
