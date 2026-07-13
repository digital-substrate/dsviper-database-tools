"""dsviper-database-tools — definitions-directed document rewriting & database
migration for Viper, in pure Python over the `dsviper` binding (no C++).

Public API:

    from dsviper_database_tools import (
        TransformationDirectives,      # the edit script (declarative)
        DefinitionsTransformer,        # target() + value() — the rewrite engine
        DropRecord,                    # loop-level "skip this document" signal
        migrate_database,              # read-old / write-new loop (given open DBs)
        run_migration,                 # open source, build directives, write target
        verify_migration,              # prove the target is a faithful image
        VerificationError,             # raised on any divergence
    )

Usage: write a migration file defining `build_directives(source_defs)` (see
`examples/`), then run the root tool `python3 database_migrate.py <migration>
<source> <target>`. Or call `run_migration(...)` directly as a library.
"""

from .directives import TransformationDirectives
from .rewrite import DefinitionsTransformer, DropRecord, build_target_definitions
from .migrate import migrate_database, run_migration
from .verify import verify_migration, VerificationError
from .commit_migrate import migrate_commit_database, run_commit_migration

__all__ = [
    "TransformationDirectives",
    "DefinitionsTransformer",
    "DropRecord",
    "build_target_definitions",
    "migrate_database",
    "run_migration",
    "verify_migration",
    "VerificationError",
    "migrate_commit_database",
    "run_commit_migration",
]

__version__ = "0.1.0"
