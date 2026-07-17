"""The static plan report — pre-validation from directives + definitions ALONE.

`plan(source_defs, directives)` classifies every change a migration would make, reading
only the **schema** (no documents, no target build, no I/O): each site is Class A
(lossless — applies automatically), Class B (policied — can lose information), or refused;
and separately flagged **lossy** when data changes or disappears (a field drop is Class A
for the engine but a data loss for the author — the forgotten-rename trap).

It also raises **warnings** the author should see before running anything: a Class-B site
with no decreed policy, a refused op, and a drop+add in the same struct (a *possible
forgotten rename* that would silently lose the old field's data).

This is the "identify" half of *identify, inform, leave the choice*: cheap, data-free, and
it fixes the shape of the findings the dry-run report (which adds real counts + samples)
and Class-C will extend. Format-agnostic like the engine — the report is plain data.
"""

import dsviper as V

from .engine import WIDENING, INT_RANGE, _const, _vecmat_retype_class

_FLOATS = {"float", "double"}
_INTS = set(INT_RANGE)


def _classify_retype(src_type, new_type):
    """(class, human risk label) for a field retype, from the source and target leaf
    types. Class A = total/lossless (widen, X→string); B = policied (can lose); refused."""
    vm = _vecmat_retype_class(src_type, new_type)      # Vec/Mat element widen (A) / narrow (B) / refused
    if vm is not None:
        return vm
    sc, tc = src_type.type_code(), new_type.type_code()
    if sc == "variant" and tc == "variant":            # arm-set change (arms compared by repr —
        src_arms = {a.representation() for a in V.TypeVariant.cast(src_type).types()}   # approximate
        tgt_arms = {a.representation() for a in V.TypeVariant.cast(new_type).types()}   # for renamed arms
        removed = src_arms - tgt_arms
        if removed:
            return "B", f"variant arm removal ({', '.join(sorted(removed))})"
        return "A", "variant add/reorder arms (lossless)"
    if sc == "variant" or tc == "variant":
        return "refused", f"{sc}→{tc} — a variant↔non-variant retype is not supported"
    if (sc, tc) in WIDENING:
        return "A", f"widening {sc}→{tc} (lossless)"
    if tc == "string":
        return "A", f"{sc}→string (total)"
    if sc == "set" and tc == "vector":
        return "A", "Set→Vector (canonical total-order sequence, lossless)"
    if sc == "vector" and tc == "xarray":
        return "A", "Vector→XArray (order preserved, deterministic positions, lossless)"
    if sc == "xarray" and tc == "vector":
        return "A", "XArray→Vector (live elements in order, lossless)"
    if sc == "optional" and tc != "optional":
        return "B", "unwrap Optional (nil has no image)"
    if tc == "optional" and sc != "optional":
        return "B", f"wrap into Optional<{tc}>"
    if sc == "string":
        return "B", f"parse string→{tc}"
    if sc in _FLOATS and tc in _INTS:
        return "B", f"float→int ({sc}→{tc}: fraction + NaN/inf/overflow loss)"
    if sc in _INTS and tc in _INTS:
        return "B", f"narrowing {sc}→{tc} (overflow)"
    if sc in _FLOATS and tc in _FLOATS:
        return "B", f"float narrowing {sc}→{tc} (precision)"
    if sc in _INTS and tc in _FLOATS:
        return "B", f"{sc}→{tc} (precision for large magnitudes)"
    return "B", f"{sc}→{tc} (review)"


def plan(source_defs, directives):
    """Classify every change `directives` would make to `source_defs`, from the schema
    alone. Returns `{"changes": [...], "warnings": [...], "summary": {...}}` — plain data,
    serialisable. Each change is `{kind, site, detail, class: A|B|refused, loss: bool,
    policy}`. No documents are read and no target is built."""
    defs = _const(source_defs)
    d = directives
    structs = {s.representation(): s for s in defs.structures()}
    changes, warnings = [], []

    def add(kind, site, detail, cls="A", loss=False, policy=None):
        changes.append({"kind": kind, "site": site, "detail": detail,
                        "class": cls, "loss": loss, "policy": policy})

    for old, new in d.type_renames.items():
        add("rename_type", old, f"→ {new}")
    for key, name in d.namespace_names.items():
        add("rename_namespace", key, f"→ name '{name}' (new representations, same ids)")
    for key, _uuid in d.namespace_uuids.items():
        add("remap_namespace", key, "→ new UUID (re-id, same representations)")

    for srep, fields in d.field_renames.items():
        for o, n in fields.items():
            add("rename_field", f"{srep}.{o}", f"→ {n}")
    for srep in d.field_order:
        add("reorder_fields", srep, "target field order set")
    for srep, adds in d.added_fields.items():
        for name, payload, derive in adds:
            if derive is not None:                         # Class-C derived field (hook-computed)
                add("add_field", f"{srep}.{name}",
                    f"derived {payload.representation()} ({getattr(derive, '__name__', 'hook')})",
                    cls="C", loss=True)
            else:
                add("add_field", f"{srep}.{name}", f"seeded {payload.type().representation()}")
    for srep, drops in d.dropped_fields.items():
        for name in drops:
            add("drop_field", f"{srep}.{name}", "field removed — its data is DROPPED",
                cls="A", loss=True)                       # total for the engine, a loss for the author
    for srep, retypes in d.retyped_fields.items():
        st = structs.get(srep)
        for fname, (new_type, policy) in retypes.items():
            if st is not None:
                cls, risk = _classify_retype(st.check(fname).type(), new_type)
            else:
                cls, risk = "B", "retype (source struct not found)"
            add("retype_field", f"{srep}.{fname}", risk, cls, loss=(cls == "B"), policy=policy)
            if cls == "B" and policy is None:
                warnings.append(f"missing policy — {srep}.{fname}: {risk} (Class B must decree a policy)")
            if cls == "refused":
                warnings.append(f"refused — {srep}.{fname}: {risk}")
    for srep, fields in d.transformed_fields.items():
        for fname, (new_type, fn) in fields.items():
            add("transform_field", f"{srep}.{fname}",
                f"custom transform → {new_type.representation()} ({getattr(fn, '__name__', 'hook')})",
                cls="C", loss=True)
            warnings.append(f"custom transform — {srep}.{fname}: a Class-C hook owns the loss "
                            f"model (the engine validates its output; it may drop the record)")
    if d.transformed_types:
        by_rid = {t.runtime_id().representation(): t for t in
                  [*defs.concepts(), *defs.clubs(), *defs.enumerations(), *defs.structures()]}
        for rid, (new_type, fn) in d.transformed_types.items():
            site = by_rid[rid].representation() if rid in by_rid else rid
            add("transform_type", site,
                f"custom transform of EVERY occurrence → {new_type.representation()} "
                f"({getattr(fn, '__name__', 'hook')})", cls="C", loss=True)
            warnings.append(f"custom transform — {site}: a Class-C hook rewrites every occurrence "
                            f"of this type (author owns the loss model)")

    for srep, fields in d.resized_fields.items():
        st = structs.get(srep)
        for fname, (kind, dims, fill, on_shrink) in fields.items():
            shrinks = False
            if st is not None:
                t = st.check(fname).type()
                if kind == "vec" and t.type_code() == "vec":
                    shrinks = V.TypeVec.cast(t).size() > dims[0]
                elif kind == "mat" and t.type_code() == "mat":
                    m = V.TypeMat.cast(t)
                    shrinks = m.columns() > dims[0] or m.rows() > dims[1]
            if shrinks:                                    # drops trailing cells → Class B
                add("resize_field", f"{srep}.{fname}", f"{kind} shrink (drops cells)",
                    "B", loss=True, policy=on_shrink)
                if on_shrink not in ("accept",):
                    warnings.append(f"resize shrink — {srep}.{fname}: drops cells; "
                                    f"on_shrink='accept' to allow (currently {on_shrink!r})")
            else:                                          # grow / same → Class A, fill invented
                add("resize_field", f"{srep}.{fname}", f"{kind} grow (fill={fill})", "A")
    for srep, fset in d.transposed_fields.items():
        for fname in sorted(fset):
            add("transpose_field", f"{srep}.{fname}", "Mat transpose [i,j]->[j,i] (lossless)", "A")

    for erep, cren in d.case_renames.items():
        for o, n in cren.items():
            add("rename_case", f"{erep}::{o}", f"→ {n}")
    for erep, added in d.added_cases.items():
        for name in added:
            add("add_case", f"{erep}::{name}", "appended")
    for erep in d.case_order:
        add("reorder_cases", erep, "target case order set")
    for erep, removed in d.removed_cases.items():
        for case, policy in removed.items():
            add("remove_case", f"{erep}::{case}", "case removed — populated values need a policy",
                cls="B", loss=True, policy=policy)
            if policy is None:
                warnings.append(f"missing policy — {erep}::{case}: remove-case (Class B must decree a policy)")

    # a drop + an add in the same struct is a *possible forgotten rename*, which
    # would silently lose the dropped field's data. Surface it — the engine cannot know intent.
    for srep in set(d.dropped_fields) & set(d.added_fields):
        dropped = sorted(d.dropped_fields[srep])
        added = sorted(n for n, _p, _d in d.added_fields[srep])
        warnings.append(
            f"possible forgotten rename in {srep}: dropped {dropped}, added {added} — a "
            f"drop+add silently loses the old data; declare rename_field if a rename was meant")

    # non-injective type mapping: two source named types landing in one target registry slot
    # (post-remap namespace + post-rename name). The runtime *governs* this — it refuses the
    # duplicate at build (DSM governance) — but opaquely and late; we surface it early, as a
    # warning, so the operator sees it in the plan rather than as a mid-build error. Advisory,
    # not authoritative: the runtime remains the arbiter.
    slots = {}
    for named in [*defs.concepts(), *defs.clubs(), *defs.enumerations(), *defs.structures()]:
        full = named.representation()
        simple = d.type_renames.get(full, full).split("::")[-1]
        ns_uuid = named.type_name().name_space().uuid().representation()
        remapped = d.namespace_uuids.get(ns_uuid)
        slot = (remapped.representation() if remapped is not None else ns_uuid, simple)
        if slot in slots:
            warnings.append(
                f"non-injective type mapping: '{full}' and '{slots[slot]}' both map to target "
                f"'{simple}' in the same namespace — the runtime will refuse the duplicate at "
                f"build (and a variant with both as arms would be illegal); give them distinct "
                f"target names")
        else:
            slots[slot] = full

    summary = {
        "changes": len(changes),
        "class_a": sum(1 for c in changes if c["class"] == "A"),
        "class_b": sum(1 for c in changes if c["class"] == "B"),
        "class_c": sum(1 for c in changes if c["class"] == "C"),
        "refused": sum(1 for c in changes if c["class"] == "refused"),
        "lossy": sum(1 for c in changes if c["loss"]),
        "warnings": len(warnings),
    }
    return {"changes": changes, "warnings": warnings, "summary": summary}


def format_plan(report):
    """Render a `plan()` report as human-readable text (the operator's pre-flight view)."""
    s = report["summary"]
    out = [f"Migration plan — {s['changes']} changes: {s['class_a']} lossless (A), "
           f"{s['class_b']} policied (B), {s.get('class_c', 0)} custom (C), {s['refused']} refused; "
           f"{s['lossy']} lossy; {s['warnings']} warning(s)."]
    tag = {"A": "A ", "B": "B!", "C": "C~", "refused": "XX"}
    for c in report["changes"]:
        loss = " [LOSS]" if c["loss"] else ""
        if c["policy"] is not None:
            pol = f"  policy={c['policy']}"
        elif c["class"] == "B":
            pol = "  policy=REQUIRED"
        else:
            pol = ""
        out.append(f"  [{tag[c['class']]}] {c['kind']:<16} {c['site']:<30} {c['detail']}{loss}{pol}")
    if report["warnings"]:
        out.append("Warnings:")
        out += [f"  ! {w}" for w in report["warnings"]]
    return "\n".join(out)
