"""The rewrite kernel — the pure, I/O-free core of the tool.

A schema-directed, **format-agnostic** value transformation over the Viper value model
(`Definitions` / `Type` / `Value`): `DefinitionsRewriter` builds a target `Definitions`
from a source schema + `TransformationDirectives` and rewrites any source-domain value to
the target domain, with no I/O, no store, and no serialization format of its own.

Everything else is a *peer that consumes this kernel* by feeding it values and taking them
back: the store loops (`migrate_database`, `migrate_commit_database`), and — because the
engine works on values, not bytes — any `format ↔ Value` codec (JSON, XML, plist), the
`Fuzzer`, or a hand-built value. See `REWRITE.md` (incl. §7, prior art).

Public surface:

    DefinitionsRewriter        # build_target_definitions() + value() — the engine
    TransformationDirectives   # the declarative edit script (the engine's input)
    build_target_definitions   # definitions ⇒ definitions (phase 1)
    Unrepresentable            # a value has no faithful target image (decreed elide)
    plan / format_plan         # the static plan report (schema-only pre-validation)
    DiagnosticSink / format_report   # the dynamic diagnostic report (real per-site loss)
"""

from .engine import DefinitionsRewriter, build_target_definitions, Unrepresentable
from .directives import TransformationDirectives
from .plan import plan, format_plan
from .report import DiagnosticSink, format_report

__all__ = [
    "DefinitionsRewriter",
    "TransformationDirectives",
    "build_target_definitions",
    "Unrepresentable",
    "plan",
    "format_plan",
    "DiagnosticSink",
    "format_report",
]
