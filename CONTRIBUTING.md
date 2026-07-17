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

- **Kernel** (`rewrite/`) — the pure, format-agnostic core: the engine
  (`rewrite/engine.py`: target-directed `value()` + `build_target_definitions`) and its
  declarative edit script (`rewrite/directives.py`). No I/O, no store, no format.
- **Database** (`migrate_database.py`) — silo 2: the `migrate` loop, its `verify`
  self-check, and the `run` entry point.
- **CommitDatabase** (`migrate_commit_database.py`) — silo 3: the faithful DAG replay
  `migrate`, its `verify` self-check, and the `run` entry point.
- **Tool** (`database_migrate.py`, at the repo root) — the command-line entry point;
  loads a migration file and calls `migrate_database.run` / `migrate_commit_database.run`.

## No silent loss

Every lossy operation must be explicitly opted into with a policy, and refused
otherwise *before* any data is touched. New Class-B operations follow the same rule:
default to `fail`, consult the policy only on the offending value.

## Tests

`tests/` (unittest) runs against the freshly-installed package. Add tests for any new
operation.
