"""The dynamic diagnostic report — what a migration would actually do to the DATA.

The static `plan` (`plan.py`) reads the schema alone and says which sites *could* lose
information. This report reads the values: it aggregates the findings the engine emits
each time a Class-B policy actually **bites** — a narrowing saturated, a fraction
truncated, a nil/removed-case elided, a set/map member collapsed — into per-site counts
and a bounded sample of `before → after` pairs.

`DiagnosticSink` is that aggregator: a callable the engine notifies (one finding dict per
bite). It owns the aggregation and the sample cap; the engine only reports (see
`DefinitionsRewriter._emit`). Wire it over any value stream — `migrate_database.dry_run`
runs it over a whole `Database`, but because `rewriter.value` is per-value and pure the
same sink works over one attachment, one document, or a single hand-built value.

This is the "inform" half of *identify, inform, leave the choice*: the operator sees the
real loss — counts and concrete offenders — and revises the plan or picks another strategy
*before* committing. It is the same channel a future Class-C hook will use to turn an
opaque runtime failure into a pre-validation finding.
"""


class DiagnosticSink:
    """Aggregates engine findings into a per-site loss report. Pass an instance where a
    `sink` is expected; the engine calls it with one finding dict `{site, op, policy,
    before, after}` per Class-B bite. `max_samples` bounds the `before→after` pairs kept
    per `(site, op)` group (the counts stay exact regardless)."""

    def __init__(self, max_samples=5):
        self.max_samples = max_samples
        self._groups = {}          # (site, op) -> record; insertion order preserved

    def __call__(self, finding):
        key = (finding["site"], finding["op"])
        rec = self._groups.get(key)
        if rec is None:
            rec = {"site": finding["site"], "op": finding["op"],
                   "policy": finding["policy"], "count": 0, "dropped": 0, "samples": []}
            self._groups[key] = rec
        rec["count"] += 1
        if finding["after"] is None:                   # an elided value (drop-record) — count it here,
            rec["dropped"] += 1                         # per finding, not later from the bounded samples
        if len(rec["samples"]) < self.max_samples:
            rec["samples"].append((finding["before"], finding["after"]))

    def report(self):
        """The aggregate, as plain serialisable data: `{"sites": [...], "summary": {...}}`.
        Each site record is `{site, op, policy, count, dropped, samples}`; `samples` is a list of
        `(before, after)` pairs (`after` is `None` when the value was dropped/elided)."""
        sites = list(self._groups.values())
        return {
            "sites": sites,
            "summary": {
                "findings": sum(r["count"] for r in sites),    # total offenders touched
                "sites": len(sites),                           # distinct (site, op) groups
                "dropped": sum(r["dropped"] for r in sites),   # findings that elided the value —
                                                               # counted per finding, not from the samples
            },
        }


def format_report(report):
    """Render a `DiagnosticSink.report()` as human-readable text (the operator's post-run,
    pre-commit view). Mirrors `format_plan`: one line per lossy site, with a sample."""
    s = report["summary"]
    if not report["sites"]:
        return "Diagnostic report — no Class-B policy fired: nothing was lost."
    out = [f"Diagnostic report — {s['findings']} value(s) lost/altered across "
           f"{s['sites']} site(s)."]
    for r in report["sites"]:
        pol = f"  policy={r['policy']}" if r["policy"] is not None else ""
        out.append(f"  {r['op']:<20} {str(r['site']):<32} ×{r['count']}{pol}")
        for before, after in r["samples"]:
            arrow = "dropped" if after is None else after
            out.append(f"      {before} → {arrow}")
    return "\n".join(out)
