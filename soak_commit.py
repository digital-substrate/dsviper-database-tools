#!/usr/bin/env python3
"""Real-scale soak — replay a real CommitDatabase verbatim and prove the round-trip.

An **identity** migration (empty directives) of a real, historied `CommitDatabase`: read the
source read-only, re-issue every commit of its DAG in topological order (opcodes rewritten
through the engine — identity here), stream every referenced blob on reference to a fresh
target, and verify the rebuild opcode by opcode. It exercises the whole silo-3 loop — the DAG
consumption, the per-opcode rewrite, copy-on-reference blob streaming, and the opcode-level
`verify` — at real scale, where the cost is blob VOLUME (GB) and the commit count.

    python3 soak_commit.py <source.rapmc> [target.rapmc]

`source` is opened read-only and never modified. `target` (a temp file if omitted) is written
fresh and deleted afterwards. Prints throughput and a byte/commit-progress trace over the run.
"""
import os
import sys
import tempfile
import time

import dsviper as V

from dsviper_database_tools import (
    TransformationDirectives, DefinitionsRewriter, migrate_commit_database)


def _remove(path):
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def soak(source_path, target_path=None):
    owns_target = target_path is None
    if owns_target:
        target_path = os.path.join(tempfile.gettempdir(), "soak_commit_target.rapmc")
    _remove(target_path)

    source = V.CommitDatabase.open(source_path, readonly=True)
    try:
        st = source.blob_statistics()
        commits = len(source.commit_ids())
        atts = len(source.definitions().attachments())
        print(f"source: {commits} commits, {atts} attachments, {st.count()} blobs, "
              f"{st.total_size() / 1e9:.2f} GB (max blob {st.max_size() / 1e6:.0f} MB)")

        rewriter, target_defs = DefinitionsRewriter.from_directives(
            source.definitions(), TransformationDirectives())          # identity
        target = V.CommitDatabase.create(target_path)
        try:
            target.extend_definitions(target_defs.const())

            mark = [500e6]
            def on_progress(p):
                if p.bytes_copied >= mark[0] or p.commits == p.commit_count:
                    print(f"  ... {p.bytes_copied / 1e9:.2f}/{p.bytes_total / 1e9:.2f} GB, "
                          f"commit {p.commits}/{p.commit_count} ({p.blobs} blobs)")
                    while p.bytes_copied >= mark[0]:
                        mark[0] += 500e6

            t0 = time.time()
            info = migrate_commit_database.migrate(source, rewriter, target, on_progress=on_progress)
            t1 = time.time()
            gb = st.total_size() / 1e9
            rate = gb * 1000 / (t1 - t0) if t1 > t0 else 0
            print(f"migrate: {info['commits']} commits, {info['blobs']} blobs  |  "
                  f"{t1 - t0:.1f}s @ {rate:.0f} MB/s")

            v = migrate_commit_database.verify(source, rewriter, target, info["remap"])
            faithful = v["commits"] == commits
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
