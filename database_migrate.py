#!/usr/bin/env python3
"""Migrate a Viper Database or CommitDatabase under a transformed schema.

Reads the source read-only and writes a fresh target — a rebuild, never an in-place
ALTER (a type's runtimeId is its storage key); the source is kept as rollback. A
`CommitDatabase` is replayed faithfully (history preserved). The schema change is
described by a migration file that defines
`build_directives(source_defs) -> TransformationDirectives`.

The decision loop is on the command line — *identify, inform, then decide*:

    python3 database_migrate.py migration.py old.db          --plan       # identify: the static plan
    python3 database_migrate.py migration.py old.db          --dry-run    # inform:   real loss, no write
    python3 database_migrate.py migration.py old.db new.db   --verify     # decide:   migrate + prove
"""
from __future__ import annotations
import argparse
import os
import sys
import importlib.util

import dsviper as V

from dsviper_database_tools import (migrate_database, migrate_commit_database,
                                    DefinitionsRewriter, plan, format_plan, format_report)


def load_build_directives(path):
    """Import a migration file by path and return its `build_directives` callable.
    The file is arbitrary Python (the operator's own code) — there is no sandbox."""
    spec = importlib.util.spec_from_file_location("_dsviper_migration", path)
    if spec is None or spec.loader is None:
        print(f"cannot load migration file: {path}", file=sys.stderr)
        sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "build_directives"):
        print(f"{path}: must define build_directives(source_defs) -> TransformationDirectives",
              file=sys.stderr)
        sys.exit(1)
    return module.build_directives


def main():
    parser = argparse.ArgumentParser(
        description="Migrate a Viper Database or CommitDatabase under a transformed "
                    "schema. Reads the source read-only and writes a fresh target; the "
                    "source is kept as rollback (a rebuild, never an in-place ALTER).")
    parser.add_argument("migration", help="Python file defining build_directives(source_defs)")
    parser.add_argument("source", help="path to the source Database / CommitDatabase (read-only)")
    parser.add_argument("target", nargs="?",
                        help="path of the fresh target to write (omit with --plan / --dry-run)")
    parser.add_argument("--plan", action="store_true",
                        help="IDENTIFY: print the static plan (from the schema alone), write nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="INFORM: exercise the rewrite over the real data (which policies bite, "
                             "what would drop, blobs), write nothing")
    parser.add_argument("--verify", action="store_true",
                        help="DECIDE: after migrating, prove the target is a faithful image")
    parser.add_argument("--force", action="store_true", help="overwrite the target if it exists")
    parser.add_argument("-v", "--verbose", action="store_true", help="print the migration summary")
    args = parser.parse_args()

    source = os.path.expanduser(args.source)
    if not os.path.exists(source):
        print(f"No such file: {source}", file=sys.stderr)
        sys.exit(1)

    # dispatch on the source kind, once — the silo module + a read-only opener
    if V.CommitDatabase.is_compatible(source):
        silo, opener = migrate_commit_database, lambda: V.CommitDatabase.open(source, readonly=True)
    elif V.Database.is_compatible(source):
        silo, opener = migrate_database, lambda: V.Database.open(source, readonly=True)
    else:
        print(f"Not a dsviper Database or CommitDatabase: {source}", file=sys.stderr)
        sys.exit(1)

    build_directives = load_build_directives(os.path.expanduser(args.migration))

    # -- pre-flight (identify / inform): read-only, print, and exit before any write --
    if args.plan or args.dry_run:
        src_obj = opener()
        try:
            directives = build_directives(src_obj.definitions())
            if args.plan:
                print(format_plan(plan(src_obj.definitions(), directives)))
            else:                                            # --dry-run
                rewriter, _ = DefinitionsRewriter.from_directives(src_obj.definitions(), directives)
                info = silo.dry_run(src_obj, rewriter)
                diagnostics = info.pop("diagnostics", None)
                print(info)                                  # counts (documents/commits, drops, blobs, …)
                if diagnostics is not None:
                    print(format_report(diagnostics))        # per-site loss, before → after
        finally:
            src_obj.close()
        return

    # -- decide: the real migration (a target is required) --
    if not args.target:
        print("a target is required for a migration (omit it only with --plan / --dry-run)",
              file=sys.stderr)
        sys.exit(1)
    target = os.path.expanduser(args.target)
    if os.path.exists(target):
        if not args.force:
            print(f"Target exists (use --force to overwrite): {target}", file=sys.stderr)
            sys.exit(1)
        os.remove(target)

    info = silo.run(source, build_directives, target, verify=args.verify)
    if args.verbose:
        print(info)
    print(f"migrated {source} -> {target}")


if __name__ == "__main__":
    main()
