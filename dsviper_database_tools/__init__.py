"""dsviper-database-tools — definitions-directed document rewriting & database
migration for Viper, in pure Python over the `dsviper` binding (no C++).

Three silo modules, each self-contained and symmetric — `migrate` / `verify` / `run`:

    import dsviper_database_tools as ddt

    ddt.migrate_database.migrate(source, rewriter, target)        # the Database loop
    ddt.migrate_database.verify(source, rewriter, target)         #   its round-trip check
    ddt.migrate_database.run(src_path, build_directives, tgt_path, verify=True)

    ddt.migrate_commit_database.migrate(source, rewriter, target) # the CommitDatabase replay
    ddt.migrate_commit_database.verify(source, rewriter, target, remap)
    ddt.migrate_commit_database.run(src_path, build_directives, tgt_path, verify=True)

Top-level vocabulary:

    from dsviper_database_tools import (
        TransformationDirectives,      # the edit script (declarative)
        DefinitionsRewriter,           # build_target_definitions() + value() — the engine
        Unrepresentable,               # a value has no faithful target image (decreed elide)
        VerificationError,             # raised by verify() on any divergence
        migrate_database,              # silo 2 module: .migrate / .verify / .run
        migrate_commit_database,       # silo 3 module: .migrate / .verify / .run
    )

Write a migration file defining `build_directives(source_defs)` (see `examples/`), then
run the root tool `python3 database_migrate.py <migration> <source> <target>`, or call
`migrate_database.run(...)` directly as a library.
"""

from .rewrite import (TransformationDirectives, DefinitionsRewriter, Unrepresentable,
                      build_target_definitions, plan, format_plan,
                      DiagnosticSink, format_report)   # the pure kernel (sub-package)
from . import migrate_database, migrate_commit_database       # consumers: .migrate / .verify / .run
from .migrate_database import VerificationError, MigrationProgress
from .migrate_commit_database import CommitMigrationProgress

__all__ = [
    "TransformationDirectives",
    "DefinitionsRewriter",
    "Unrepresentable",
    "build_target_definitions",
    "plan",
    "format_plan",
    "DiagnosticSink",
    "format_report",
    "VerificationError",
    "MigrationProgress",
    "CommitMigrationProgress",
    "migrate_database",
    "migrate_commit_database",
]

__version__ = "0.1.0"
