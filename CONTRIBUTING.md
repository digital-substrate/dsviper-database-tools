# Contributing

`dsviper-database-tools` is a pure-Python layer over the `dsviper` binding. No
compiled build.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # pulls in dsviper
python -m unittest discover -s tests -v
```

## Scope

The package is a *composition of runtime atoms* — it consumes the public `dsviper`
API and adds no C++. Keep it that way: if something seems to need a binding change,
that belongs in the runtime/binding, not here.

- **Engine** (`rewrite.py`) — the target-directed `value()` and the
  `build_target_definitions` pass. No I/O.
- **Migration** (`migrate.py`) — the `Database` read-old / write-new loop.
- **Directives** (`directives.py`) — pure data; the declarative edit script.
- **Verify** (`verify.py`) — the round-trip self-check.
- **Tool** (`database_migrate.py`, at the repo root) — the command-line entry point;
  loads a migration file and calls `run_migration`.

## No silent loss

Every lossy operation must be explicitly opted into with a policy, and refused
otherwise *before* any data is touched. New Class-B operations follow the same rule:
default to `fail`, consult the policy only on the offending value.

## Tests

`tests/` (unittest) runs against the freshly-installed package. Add tests for any new
operation.
