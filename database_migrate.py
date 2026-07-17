#!/usr/bin/env python3
"""Migrate a Viper Database or CommitDatabase under a transformed schema.

Reads the source read-only and writes a fresh target — a rebuild, never an in-place
ALTER (a type's runtimeId is its storage key); the source is kept as rollback. A
`CommitDatabase` is replayed faithfully (history preserved). The schema change is
described by a migration file that defines
`build_directives(source_defs) -> TransformationDirectives`.

    python3 database_migrate.py migration_shop_v2.py old.db new.db --verify
"""
from __future__ import annotations
import argparse
import importlib.util
import os
import sys

import dsviper as V

from dsviper_database_tools import migrate_database, migrate_commit_database


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
    parser.add_argument("target", help="path of the fresh target to write")
    parser.add_argument("--verify", action="store_true",
                        help="prove the target is a faithful image of the source")
    parser.add_argument("--force", action="store_true", help="overwrite the target if it exists")
    parser.add_argument("-v", "--verbose", action="store_true", help="print the migration summary")
    args = parser.parse_args()

    source = os.path.expanduser(args.source)
    target = os.path.expanduser(args.target)
    if not os.path.exists(source):
        print(f"No such file: {source}", file=sys.stderr)
        sys.exit(1)
    if os.path.exists(target):
        if not args.force:
            print(f"Target exists (use --force to overwrite): {target}", file=sys.stderr)
            sys.exit(1)
        os.remove(target)

    build_directives = load_build_directives(os.path.expanduser(args.migration))
    if V.CommitDatabase.is_compatible(source):
        info = migrate_commit_database.run(source, build_directives, target, verify=args.verify)
    elif V.Database.is_compatible(source):
        info = migrate_database.run(source, build_directives, target, verify=args.verify)
    else:
        print(f"Not a dsviper Database or CommitDatabase: {source}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(info)
    print(f"migrated {source} -> {target}")


if __name__ == "__main__":
    main()
