#!/usr/bin/env python3
"""definitions_migrate.py — the DSM-source twin of database_migrate.py.

A schema change migrates two artefacts: the data in a base (`database_migrate.py`)
and the hand-authored ``.dsm`` files that document that schema. This tool patches the
``.dsm`` source **in place** under the *same* ``transformation.py`` — a structured
codemod, not a renderer — so the file split, comments, ordering and formatting are
preserved by construction. It is non-destructive: it reads the source tree and writes
a fresh target tree; the original is never mutated.

The edits are span-precise. The parser (`dsviper >= 1.2.6`) yields a ``DSMSourceMap``
alongside the parsed definitions: the exact source span of every declaration, field,
case, namespace and *resolved* type-reference, each declaration carrying its identity
(``identifier()`` — ``NS::Name``, or ``NS::KeyConcept.name`` for an attachment). Each
directive maps to edits at those spans. The shipped ``rewrite/`` engine is reused only as the verification oracle:
re-parse the patched tree and compare its ``Definitions`` digest to the engine's target
(``runtimeId`` is a structure fingerprint, so equal digests prove the patch faithful).

Function pools live *outside* the persistence ``Definitions`` (they are binding/service,
not stored data), so the engine's digest ignores them and carries no pool directive. But
their signatures reference named types, and the source-map captures those references like
any other — so a type rename or a namespace rename flows into pool signatures through the
same reference pass, keeping the patched tree resolvable (which the verify re-parse checks).
"""

from __future__ import annotations

import os

import dsviper as V

from dsviper_database_tools import DefinitionsRewriter


# -- name helpers ----------------------------------------------------------------------

def _repr(type_name) -> str:
    """The qualified ``NS::Name`` representation of a binding ``TypeName``."""
    return f"{type_name.name_space().name()}::{type_name.name()}"


def _simple(qualified: str) -> str:
    """The simple (unqualified) name of a ``NS::Name`` representation."""
    return qualified.rsplit("::", 1)[-1]


# -- function pools: outside the persistence Definitions, so outside the engine's view -----
#
# A pool declares no storage, so the engine never sees one — but its signatures NAME types, and
# a migration can leave one naming nothing. Two failure modes, and they differ in kind:
#
#   * a DROPPED type leaves a signature naming a type that no longer exists. That is membership
#     of a name in a set, answerable HERE, before any edit, on the parsed DSM model (where a
#     reference is a `TypeName` — an FQN — whatever the source text writes);
#   * a RENAMED type can instead make a bare signature reference AMBIGUOUS (two namespaces now
#     offering one simple name). That is a property of the whole patched tree, answerable only
#     by resolving it — the parser's job at the verify re-parse, which reports it sited and with
#     its candidates. Pre-computing it would mean re-implementing the inspector; do not.

def _signature_type_names(node, out: list) -> None:
    """Every named type a signature's return/parameter type references, by FQN. The DSM model
    nests typed nodes (`element_type` / `key_type` / `types`) down to a leaf reference, so the
    walk is by shape, not by class — a composite added later is followed, not missed."""
    if hasattr(node, "type_name"):                      # a leaf reference
        out.append(str(node.type_name()))
        return
    if hasattr(node, "types"):                          # tuple / variant
        for t in node.types():
            _signature_type_names(t, out)
        return
    if hasattr(node, "key_type"):                       # map
        _signature_type_names(node.key_type(), out)
    if hasattr(node, "element_type"):                   # vector / set / optional / key / map
        _signature_type_names(node.element_type(), out)


def _pool_findings(dsm_defs, directives):
    """Walk every signature of both pool kinds — `function_pool` (stateless) and
    `attachment_function_pool` (stateful; the name is a codegen contract about an implicit first
    parameter, it binds no persistence attachment) — and classify each named type it references:
    dropped (dangling, refused) or transform_type'd (rewritten, worth telling the author)."""
    transformed = {}
    for rid, (new_type, _fn) in directives.transformed_types.items():
        name = directives.transformed_type_names.get(rid)
        if name is not None:
            transformed[name] = new_type.representation()

    dangling, rewritten = [], []
    pools = [p for p in dsm_defs.function_pools()]
    pools += [p for p in dsm_defs.attachment_function_pools()]
    for pool in pools:
        for function in pool.functions():
            prototype = function.prototype()
            sites = [("return type", prototype.return_type())]
            sites += [(f"parameter '{name}'", node) for name, node in prototype.parameters()]
            for label, node in sites:
                names: list = []
                _signature_type_names(node, names)
                for fqn in names:
                    site = (f"{pool.name()}::{prototype.name()}", label, fqn)
                    if fqn in directives.dropped_types:
                        dangling.append(site)
                    elif fqn in transformed:
                        rewritten.append(site + (transformed[fqn],))
    return dangling, rewritten


def _refuse_dangling_pools(dsm_defs, directives, on_notice=None) -> None:
    """Refuse a migration that would leave a pool signature naming a dropped type, with every
    site accumulated into one report. A transform_type'd type is NOT refused — the signature is
    rewritten to the new type, which is what was asked — but it silently changes a pool's API,
    so it is notified instead."""
    dangling, rewritten = _pool_findings(dsm_defs, directives)
    if on_notice is not None:
        for signature, label, fqn, new in sorted(rewritten):
            on_notice(f"[pool-signature-rewritten] {signature} — {label} : {fqn} -> {new}")
    if not dangling:
        return
    lines = "\n".join(f"  {signature} — {label} : {fqn}"
                      for signature, label, fqn in sorted(dangling))
    raise ValueError("[dropped-type-in-pool] drop_type would leave "
                     f"{len(dangling)} function-pool signature(s) naming a type that no longer "
                     "exists:\n" + lines +
                     "\nA pool is an API, not a document — no policy converts a live call. Edit the "
                     "signature by hand, drop the function, or keep the dropped type.")


# -- span resolution: a global (content) offset -> (file, local offset) ----------------

def _line_starts(text: str) -> list[int]:
    """Character offset of the start of each 1-based line (``out[line - 1]``). A span's
    offsets index ``builder.content()`` as a Python ``str`` — code points, not bytes — so a
    non-ASCII docstring above a declaration does not shift the arithmetic."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


class _Resolver:
    """Maps a ``DSMSourceSpan`` (global offsets into ``builder.content()``) to
    ``(source_file, local_start, local_stop)``. A file starts at a line boundary in the
    assembled content, so its global base is the content offset of its first line; the
    local offset is ``global - base`` (valid across multi-line spans)."""

    def __init__(self, builder):
        content_starts = _line_starts(builder.content())
        first_line: dict[str, int] = {}
        for part in builder.parts():
            src = part.source()
            first_line[src] = min(first_line.get(src, part.line_start()), part.line_start())
        self._base = {src: content_starts[line - 1] for src, line in first_line.items()}
        self._builder = builder

    def resolve(self, span):
        source = self._builder.part(span.line()).source()
        base = self._base[source]
        return source, span.start() - base, span.stop() - base + 1   # [start, stop) half-open


# -- an edit is a (source_file, local_start, local_stop, replacement) -------------------
#
# start == stop marks an insertion (a zero-width splice). `tidy` widens a deletion to
# swallow its statement terminator and leave no dangling line.

class _Edit:
    __slots__ = ("source", "start", "stop", "replacement", "tidy")

    def __init__(self, source, start, stop, replacement, tidy=False):
        self.source, self.start, self.stop = source, start, stop
        self.replacement, self.tidy = replacement, tidy


def _tidy_cut(text: str, start: int, stop: int) -> tuple[int, int]:
    """Widen ``[start, stop)`` of a deletion to swallow the trailing statement
    terminator and leave no dangling blank line: eat a following ``;`` (past any
    spaces), then the rest of the line through its newline, and the indentation
    back to the line start when nothing else remains before it."""
    n = len(text)
    end = stop
    while end < n and text[end] in " \t":
        end += 1
    if end < n and text[end] == ";":
        end += 1
    while end < n and text[end] in " \t":
        end += 1
    if end < n and text[end] == "\n":
        end += 1
    begin = start
    while begin > 0 and text[begin - 1] in " \t":
        begin -= 1
    if begin == 0 or text[begin - 1] == "\n":
        if begin >= 2 and text[begin - 2] == "\n":            # a blank line sits ABOVE the cut —
            after = end                                       # collapse a blank line now left BELOW
            while after < n and text[after] in " \t":
                after += 1
            if after < n and text[after] == "\n":
                end = after + 1
        return begin, end
    return start, end


def _tidy_cut_case(text: str, start: int, stop: int) -> tuple[int, int]:
    """A case lives in a comma-separated list, not a ``;``-terminated statement.
    Eat a following comma (and its trailing blank line) if present; otherwise eat a
    *preceding* comma (removing the list's last case), plus any leading indentation."""
    n = len(text)
    end = stop
    while end < n and text[end] in " \t":
        end += 1
    if end < n and text[end] == ",":                       # a middle/leading case: eat "<name>,"
        end += 1
        while end < n and text[end] in " \t":
            end += 1
        if end < n and text[end] == "\n":
            end += 1
        begin = start
        while begin > 0 and text[begin - 1] in " \t":
            begin -= 1
        return (begin, end) if (begin == 0 or text[begin - 1] == "\n") else (start, end)
    begin = start                                          # the last case: eat the preceding ", "
    while begin > 0 and text[begin - 1] in " \t\n":
        begin -= 1
    if begin > 0 and text[begin - 1] == ",":
        begin -= 1
    return begin, stop


def _resolve_overlaps(edits: list[_Edit]) -> list[_Edit]:
    """Drop any edit strictly contained within another edit's span: a wholesale
    replacement (a retyped field's type, a dropped block) subsumes the finer edits
    inside it (e.g. a reference rename that falls within a rewritten type)."""
    replacements = [e for e in edits if e.stop > e.start]
    kept = []
    for e in replacements:
        if any(o is not e and o.source == e.source          # offsets are per-file — compare within one
               and o.start <= e.start and e.stop <= o.stop
               and (o.stop - o.start) > (e.stop - e.start) for o in replacements):
            continue
        kept.append(e)
    kept.extend(e for e in edits if e.stop == e.start)     # insertions never subsume
    return kept


def _apply(text: str, edits: list[_Edit]) -> str:
    """Splice edits into one file's text, right-to-left. Assumes non-overlapping
    (see ``_resolve_overlaps``); insertions (start == stop) splice cleanly."""
    for e in sorted(edits, key=lambda e: (e.start, e.stop), reverse=True):
        start, stop = e.start, e.stop
        if e.tidy and e.replacement == "":
            start, stop = _tidy_cut(text, start, stop)
        text = text[:start] + e.replacement + text[stop:]
    return text


# -- rendering a lone member (an added field): reuse the binding's own DSM renderer -----

def _render_field_line(name, value_or_type) -> str:
    """The DSM text of one field — ``<type> <name>[ = <literal>];`` — produced by the
    binding's renderer over a throwaway one-field struct, so literal formatting (floats,
    uuids, containers) is the engine's, not ours. ``value_or_type`` is a default ``Value``
    (static add) or a ``Type`` (a ``derive=`` field, which carries no default)."""
    ns = V.NameSpace(V.ValueUUId("dede0000-0000-4000-8000-000000000001"), "T")
    d = V.Definitions()
    ds = V.TypeStructureDescriptor("F")
    ds.add_field(name, value_or_type, "")
    d.create_structure(ns, ds)
    dsm = V.DSMDefinitions.from_definitions(d.const()).to_dsm()
    for line in dsm.splitlines():
        stripped = line.strip()
        if stripped.endswith(";") and stripped != "};" and "struct" not in stripped:
            return stripped
    raise AssertionError(f"could not render field {name!r}")


# -- edit derivation: directives + source-map -> edits ---------------------------------

def _index(source_map):
    """Build lookup indices over the flat source-map lists. A declaration is keyed by its
    ``identifier()`` — the source map's own identity for it: ``NS::Name`` for a type, and
    ``NS::KeyConcept.name`` for an attachment, whose key concept is part of its identity (one
    namespace may hold two attachments of the same name)."""
    decl = {d.identifier(): d for d in source_map.declarations()}
    field = {(_repr(f.structure()), f.name()): f for f in source_map.fields()}
    case = {(_repr(c.enumeration()), c.name()): c for c in source_map.cases()}
    return decl, field, case


def _insert_before_close(decl_span, member: str, resolve, files, member_span=None,
                         join_comma=False) -> _Edit | None:
    """An insertion of ``member`` just before a declaration block's closing ``}``,
    indented like the existing members. ``member_span`` (an existing member) supplies the
    indentation; ``join_comma`` prefixes ``, `` onto the previous (comma-list) member."""
    src, bstart, bstop = resolve(decl_span)
    text = files[src]
    brace = text.rfind("}", bstart, bstop)
    if brace < 0:
        return None
    line_begin = text.rfind("\n", bstart, brace) + 1
    brace_indent = text[line_begin:brace]
    member_indent = None
    if member_span is not None:
        mstart = resolve(member_span)[1]
        mline = text.rfind("\n", 0, mstart) + 1
        member_indent = text[mline:mstart]
    if not member_indent:
        member_indent = brace_indent + "    "
    if join_comma and member_span is not None:            # append after the last case: ",\n<indent><m>"
        mstop = resolve(member_span)[2]
        return _Edit(src, mstop, mstop, ",\n" + member_indent + member)
    return _Edit(src, brace, brace, member_indent + member + "\n" + brace_indent)


def _derive(directives, source_map, resolve, files, rewriter, source_defs) -> list[_Edit]:
    decl, field, case = _index(source_map)
    src_struct = {s.representation(): s for s in source_defs.structures()}
    # an attachment directive addresses its target by `identifier()` (`NS::KeyConcept.name`) —
    # the attachment's identity, and the declaration's key — or, legacy, by the bare local name.
    # A local name is NOT an identity (one namespace may hold `A.orders` and `B.orders`), so a
    # legacy key that hits several attachments resolves to none of them here: the engine renames
    # every homonym, this layer would patch one, and the digest refuses. Map the unambiguous ones.
    att_repr = {}
    ambiguous = set()
    for a in source_defs.attachments():
        att_repr[a.identifier()] = a.identifier()
        local = a.identifier().rsplit(".", 1)[-1]
        if local in att_repr:
            ambiguous.add(local)
        att_repr[local] = a.identifier()
    for local in ambiguous:
        del att_repr[local]
    edits: list[_Edit] = []

    def edit(span, replacement, tidy=False):
        if span is None:
            return
        source, start, stop = resolve(span)
        edits.append(_Edit(source, start, stop, replacement, tidy))

    # type rename: patch the declaration name (its references are handled below)
    for src_repr, dst_repr in directives.type_renames.items():
        if src_repr in decl:
            edit(decl[src_repr].name_span(), _simple(dst_repr))

    # unified reference pass: every resolved type-reference — in a struct field AND in a
    # function-pool signature (pools sit outside the persistence Definitions, so the engine
    # digest ignores them, but the parser resolves them and the source-map captures them) —
    # rewritten to its target name, MIRRORING the source's qualification. A bare reference
    # (`Customer`) stays bare; a qualified one (`N::SI`, as pool signatures write) keeps its
    # `NS::` prefix. A type rename (simple name), a namespace rename (the prefix), and a
    # move_type (both, and always fully-qualified) are applied here, so a signature that
    # outlives its type's edit stays valid.
    for r in source_map.references():
        referent = r.referent()
        if referent is None:
            continue
        rns = referent.name_space()
        src_repr = f"{rns.name()}::{referent.name()}"
        ns_uuid = rns.uuid().representation()
        renamed_type = src_repr in directives.type_renames
        renamed_ns = ns_uuid in directives.namespace_names
        moved = src_repr in directives.type_namespaces
        if not (renamed_type or renamed_ns or moved):         # untouched (incl. every primitive)
            continue
        src_file, start, stop = resolve(r.span())
        original = files[src_file][start:stop]
        tgt_simple = _simple(directives.type_renames[src_repr]) if renamed_type else referent.name()
        if moved:                                             # a bare `T` in the old namespace would
            tgt_ns = directives.type_namespaces[src_repr].name()   # dangle — always qualify to Y::T
            replacement = tgt_ns + "::" + tgt_simple
        else:
            tgt_ns = directives.namespace_names.get(ns_uuid, rns.name())
            replacement = (tgt_ns + "::" + tgt_simple) if "::" in original else tgt_simple
        if replacement != original:
            edits.append(_Edit(src_file, start, stop, replacement))

    # transform_type: a GLOBAL type substitution (source -> new_type, at every occurrence incl.
    # nested). The directive keys the source by runtimeId (engine storage) and records the source
    # type's representation alongside, which is the name this layer matches on — every occurrence
    # in source_map.types() whose representation matches is rewritten (the span covers the whole
    # expression, composites included). A nested source's inner occurrence lands inside the outer
    # replacement — overlap resolution keeps the outer one. A named source's declaration is dropped
    # by the engine (hooked), so cut it.
    if directives.transformed_types:
        fqn_to_new = {directives.transformed_type_names[rid]: new_type.representation()
                      for rid, (new_type, _fn) in directives.transformed_types.items()
                      if rid in directives.transformed_type_names}
        for occ in source_map.types():
            new_fqn = fqn_to_new.get(occ.representation())
            if new_fqn is not None:
                src, start, stop = resolve(occ.span())
                edits.append(_Edit(src, start, stop, new_fqn))
        for fqn in fqn_to_new:                                # a named source's declaration is dropped
            if fqn in decl:
                edit(decl[fqn].block_span(), "", tidy=True)

    # field rename
    for struct_repr, renames in directives.field_renames.items():
        for old, new in renames.items():
            f = field.get((struct_repr, old))
            if f is not None:
                edit(f.name_span(), new)

    # case rename
    for enum_repr, renames in directives.case_renames.items():
        for old, new in renames.items():
            c = case.get((enum_repr, old))
            if c is not None:
                edit(c.name_span(), new)

    # attachment rename: an attachment lives in `declarations()` too (the Converter records it),
    # and NOTHING references an attachment (a key is a concept-instance identity, not a foreign
    # key), so only its declaration name needs patching — no reference sweep. Keyed by local name.
    for old_id, new_id in directives.attachment_renames.items():
        d = decl.get(att_repr.get(old_id, ""))
        if d is not None:
            edit(d.name_span(), new_id.rsplit(".", 1)[-1])   # a new id may be written qualified

    # field type change (retype / transform / resize / transpose): replace the type
    # expression with the engine-computed target type — the single oracle for the shape.
    # The Class-C `fn` is data-only; the dimension/policy directives carry no DSM text.
    type_changed: dict[str, set] = {}
    for group in (directives.retyped_fields, directives.transformed_fields,
                  directives.resized_fields, directives.transposed_fields):
        for struct_repr, entry in group.items():
            names = entry if isinstance(entry, set) else set(entry)
            type_changed.setdefault(struct_repr, set()).update(names)
    for struct_repr, fnames in type_changed.items():
        s = src_struct.get(struct_repr)
        if s is None:
            continue
        tgt = rewriter.type_map.get(s.runtime_id().representation())
        if tgt is None:
            continue
        tns = tgt.type_name().name_space()
        tgt_field = {tf.name(): tf for tf in tgt.fields()}
        renames = directives.field_renames.get(struct_repr, {})
        for fname in fnames:
            f = field.get((struct_repr, fname))
            tf = tgt_field.get(renames.get(fname, fname))
            if f is None or tf is None:
                continue
            edit(f.type_span(), tf.type().representation(namespace=tns) + " ")
            # a default was authored against the OLD type, so the engine does not carry it onto a
            # type-changed field. Follow the engine (it is the authority on the shape) and cut the
            # `= <literal>` clause, or the text would declare a default the target definition does
            # not have. The clause is a grammar rule of its own, so the parser hands us its span —
            # nothing to infer from the neighbouring spans.
            if tf.default_value() is None and f.default_span() is not None:
                src, dstart, dstop = resolve(f.default_span())
                text = files[src]
                while dstart > 0 and text[dstart - 1] in " \t":   # the clause starts at `=`; the
                    dstart -= 1                                    # space before it is a separator
                edits.append(_Edit(src, dstart, dstop, ""))

    # namespace rename (display name) / remap (uuid): patch every occurrence of the
    # namespace declaration across the file split (a namespace may span several files).
    for ns in source_map.name_spaces():
        key = ns.name_space().uuid().representation()
        if key in directives.namespace_names:
            edit(ns.name_span(), directives.namespace_names[key])
        if key in directives.namespace_uuids:
            edit(ns.uuid_span(), "{" + directives.namespace_uuids[key].representation() + "}")

    # documentation authoring: replace an existing docstring, else insert one before the
    # declaration (Class A — a doc change is outside the runtimeId, but carried faithfully).
    def doc_edit(doc_span, anchor_span, text):
        block = _render_doc(text)
        if doc_span is not None:                          # replace (or clear) an existing docstring
            edit(doc_span, block, tidy=(not block))
        elif block:                                       # author a new one, before the declaration
            src, astart, _ = resolve(anchor_span)
            line_begin = files[src].rfind("\n", 0, astart) + 1
            indent = files[src][line_begin:astart]
            edits.append(_Edit(src, line_begin, line_begin, _reindent(block, indent)))

    for type_repr, text in directives.type_docs.items():
        d = decl.get(type_repr)
        if d is not None:
            doc_edit(d.documentation_span(), d.block_span(), text)
    for struct_repr, docs in directives.field_docs.items():
        for fname, text in docs.items():
            f = field.get((struct_repr, fname))
            if f is not None:
                doc_edit(f.documentation_span(), f.declaration_span(), text)
    for enum_repr, docs in directives.case_docs.items():
        for cname, text in docs.items():
            c = case.get((enum_repr, cname))
            if c is not None:
                doc_edit(c.documentation_span(), c.name_span(), text)
    for local, text in directives.attachment_docs.items():
        d = decl.get(att_repr.get(local, ""))
        if d is not None:
            doc_edit(d.documentation_span(), d.block_span(), text)

    # add a field: render it and splice before the struct's closing brace
    for struct_repr, adds in directives.added_fields.items():
        d = decl.get(struct_repr)
        if d is None:
            continue
        members = [field[(struct_repr, n)].declaration_span()
                   for (sr, n) in field if sr == struct_repr]
        anchor = members[-1] if members else None
        for name, payload, _derive in adds:
            line = _render_field_line(name, payload)   # a Value (static) or a Type (derive=)
            e = _insert_before_close(d.block_span(), line, resolve, files, member_span=anchor)
            if e is not None:
                edits.append(e)

    # add a case: splice after the last case (comma-joined) or before the enum brace
    for enum_repr, names in directives.added_cases.items():
        d = decl.get(enum_repr)
        if d is None:
            continue
        cases = [case[(enum_repr, c)].name_span() for (er, c) in case if er == enum_repr]
        last = cases[-1] if cases else None
        for name in names:
            e = _insert_before_close(d.block_span(), name, resolve, files,
                                     member_span=last, join_comma=last is not None)
            if e is not None:
                edits.append(e)

    # remove a case: comma-aware cut
    for enum_repr, removed in directives.removed_cases.items():
        for cname in removed:
            c = case.get((enum_repr, cname))
            if c is not None:
                src, start, stop = resolve(c.name_span())
                start, stop = _tidy_cut_case(files[src], start, stop)
                edits.append(_Edit(src, start, stop, ""))

    # drop type / drop attachment: cut the whole declaration block (attachments are declarations
    # too; nothing references one, so a cut dangles nothing)
    for type_repr in directives.dropped_types:
        if type_repr in decl:
            edit(decl[type_repr].block_span(), "", tidy=True)
    for local in directives.dropped_attachments:
        d = decl.get(att_repr.get(local, ""))
        if d is not None:
            edit(d.block_span(), "", tidy=True)

    # drop field: cut the whole field declaration
    for struct_repr, dropped in directives.dropped_fields.items():
        for field_name in dropped:
            f = field.get((struct_repr, field_name))
            if f is not None:
                edit(f.declaration_span(), "", tidy=True)

    # reorder fields / cases: rewrite the member region in the TARGET order (a full permutation
    # of the target member set), each member carrying its own baked-in edits. Before move, so a
    # reordered+moved declaration's region edit is picked up as the moved text's internal edit.
    edits = _reorder_fields(edits, directives, decl, field, resolve, files)
    edits = _reorder_cases(edits, directives, decl, case, resolve, files)

    # move_type: relocate a whole declaration to a different namespace. Its text (docstring +
    # block, with any of its OWN edits — a rename/retype of the moved type — baked in) is CUT
    # from its source namespace block and spliced into the target: a fresh `namespace Y {uuid}`
    # block appended to a file where Y already lives, else to the declaration's own file (two
    # adjacent blocks for one namespace re-open it — valid DSM). References were re-qualified
    # to Y:: above. Run last, so a moved declaration's internal edits are already in `edits`.
    edits = _relocate_moved_types(edits, directives, decl, source_map, resolve, files, att_repr)

    return _resolve_overlaps(edits)


# -- reorder: rewrite a declaration's member region in the target order --------------------

def _line_indent(text: str, pos: int) -> str:
    """The leading whitespace of the line containing ``pos``."""
    line_begin = text.rfind("\n", 0, pos) + 1
    i = line_begin
    while i < len(text) and text[i] in " \t":
        i += 1
    return text[line_begin:i]


def _field_unit(f, resolve, files):
    """A field's full text extent: [docstring-or-declaration start, past the ``;``]."""
    src, dstart, dstop = resolve(f.declaration_span())
    text = files[src]
    end = dstop
    while end < len(text) and text[end] in " \t":
        end += 1
    if end < len(text) and text[end] == ";":
        end += 1
    doc = f.documentation_span()
    start = resolve(doc)[1] if doc is not None else dstart
    return src, start, end


def _case_unit(c, resolve, files):
    """A case's full text extent: [docstring-or-name start, name end] (the comma is excluded)."""
    src, nstart, nstop = resolve(c.name_span())
    doc = c.documentation_span()
    start = resolve(doc)[1] if doc is not None else nstart
    return src, start, nstop


def _bake(edits, src, start, stop, files):
    """The text of ``files[src][start:stop]`` with the edits falling inside it applied
    (rebased to local offsets) — a member carries its own rename/retype into its new slot."""
    inside = [e for e in edits if e.source == src and start <= e.start and e.stop <= stop]
    return _apply(files[src][start:stop],
                  [_Edit(e.source, e.start - start, e.stop - start, e.replacement, e.tidy)
                   for e in inside])


def _reorder_fields(edits, directives, decl, field, resolve, files):
    for struct_repr, order in directives.field_order.items():
        d = decl.get(struct_repr)
        if d is None:
            continue
        units = [(f, _field_unit(f, resolve, files))
                 for (sr, _n), f in field.items() if sr == struct_repr]
        if not units:
            continue
        renames = directives.field_renames.get(struct_repr, {})
        dropped = directives.dropped_fields.get(struct_repr, set())
        src = units[0][1][0]
        rstart = min(u[1][1] for u in units)
        rend = max(u[1][2] for u in units)
        block_stop = resolve(d.block_span())[2]
        texts = {renames.get(f.name(), f.name()): _bake(edits, s, us, ue, files)
                 for f, (s, us, ue) in units if f.name() not in dropped}
        for name, payload, _derive in directives.added_fields.get(struct_repr, []):
            texts[name] = _render_field_line(name, payload)
        if set(order) != set(texts):
            raise ValueError(f"reorder_fields({struct_repr}) not a permutation of {sorted(texts)}")
        edits = _drop_region_edits(edits, src, rstart, rend, block_stop)
        indent = _line_indent(files[src], rstart)
        edits.append(_Edit(src, rstart, rend, ("\n" + indent).join(texts[n] for n in order)))
    return edits


def _reorder_cases(edits, directives, decl, case, resolve, files):
    for enum_repr, order in directives.case_order.items():
        d = decl.get(enum_repr)
        if d is None:
            continue
        units = [(c, _case_unit(c, resolve, files))
                 for (er, _n), c in case.items() if er == enum_repr]
        if not units:
            continue
        renames = directives.case_renames.get(enum_repr, {})
        removed = directives.removed_cases.get(enum_repr, {})
        src = units[0][1][0]
        rstart = min(u[1][1] for u in units)
        rend = max(u[1][2] for u in units)
        block_stop = resolve(d.block_span())[2]
        texts = {renames.get(c.name(), c.name()): _bake(edits, s, us, ue, files)
                 for c, (s, us, ue) in units if c.name() not in removed}
        for name in directives.added_cases.get(enum_repr, []):
            texts[name] = name
        if set(order) != set(texts):
            raise ValueError(f"reorder_cases({enum_repr}) not a permutation of {sorted(texts)}")
        edits = _drop_region_edits(edits, src, rstart, rend, block_stop)
        indent = _line_indent(files[src], rstart)
        edits.append(_Edit(src, rstart, rend, (",\n" + indent).join(texts[n] for n in order)))
    return edits


def _drop_region_edits(edits, src, rstart, rend, block_stop):
    """Remove edits superseded by a region rewrite: everything inside the member region (baked
    into the member texts), and the add-member insertions past it (their text is now in order)."""
    return [e for e in edits if not (e.source == src and (
            (rstart <= e.start and e.stop <= rend)
            or (e.start == e.stop and rend <= e.start <= block_stop)))]


def _match_brace(text: str, open_pos: int) -> int:
    """Index of the ``}`` matching the ``{`` at ``open_pos``, counting braces but
    skipping string and docstring bodies (a ``"has { brace"`` default or a docstring must
    not throw off the depth). A ``{uuid}`` default is self-balancing, so it needs no care."""
    depth, i, n = 0, open_pos, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            if text[i:i + 3] == '"""':                        # docstring — cannot contain """
                close = text.find('"""', i + 3)
                i = n if close < 0 else close + 3
                continue
            i += 1                                            # string literal — skip to closing "
            while i < n and text[i] != '"':
                i += 2 if text[i] == '\\' else 1
            i += 1
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _relocate_moved_types(edits, directives, decl, source_map, resolve, files, att_repr):
    # types AND attachments move the same way (both are declarations); an attachment names its
    # target by identifier (or a legacy local name), resolved to the declaration key via att_repr.
    moves = [(t, ns) for t, ns in directives.type_namespaces.items()]
    moves += [(att_repr[i], ns) for i, ns in directives.attachment_namespaces.items()
              if i in att_repr]
    if not moves:
        return edits
    blocks = {}                                               # namespace uuid -> [(file, uuid_stop)]
    for ns in source_map.name_spaces():
        src, _s, ustop = resolve(ns.uuid_span())
        blocks.setdefault(ns.name_space().uuid().representation(), []).append((src, ustop))
    target_of = {t: ns.uuid().representation() for t, ns in moves}   # repr -> its target ns uuid
    for type_repr, target_ns in moves:
        d = decl.get(type_repr)
        if d is None:
            continue
        src_file, bstart, bstop = resolve(d.block_span())
        doc = d.documentation_span()
        carry_start = resolve(doc)[1] if doc is not None else bstart
        target_uuid = target_ns.uuid().representation()
        internal = [e for e in edits                          # this declaration's own edits
                    if e.source == src_file and carry_start <= e.start and e.stop <= bstop]
        # a reference inside the moved declaration to a type NOT landing in the target namespace
        # (e.g. an attachment's key concept, staying behind) would dangle once the declaration is
        # in Y — a bare `Person` no longer resolves. Qualify those (the reference pass only touched
        # renamed/moved referents; an unchanged staying sibling needs this).
        for r in source_map.references():
            referent = r.referent()
            if referent is None or not referent.name_space().name():   # skip primitives
                continue
            rsrc, rstart, rstop = resolve(r.span())
            if rsrc != src_file or not (carry_start <= rstart and rstop <= bstop):
                continue
            rns = referent.name_space()
            rrepr = f"{rns.name()}::{referent.name()}"
            eff_uuid = target_of.get(rrepr, rns.uuid().representation())
            original = files[rsrc][rstart:rstop]
            if eff_uuid == target_uuid or "::" in original:    # lands in target, or already qualified
                continue
            if any(rstart < e.stop and e.start < rstop for e in internal):   # already edited
                continue
            ns_name = directives.namespace_names.get(rns.uuid().representation(), rns.name())
            internal.append(_Edit(rsrc, rstart, rstop, ns_name + "::" + referent.name()))
        edits = [e for e in edits if e not in internal]       # they travel with the text, not the hole
        carried = _apply(files[src_file][carry_start:bstop],
                         [_Edit(e.source, e.start - carry_start, e.stop - carry_start,
                                e.replacement, e.tidy) for e in internal])
        edits.append(_Edit(src_file, carry_start, bstop, "", tidy=True))   # cut it out

        uuid = target_uuid
        existing = blocks.get(uuid)
        if existing:                                          # merge into a live namespace block
            dest, ustop = next((b for b in existing if b[0] == src_file), existing[0])
            text = files[dest]
            close = _match_brace(text, text.index("{", ustop))     # this block's closing brace
            line_begin = text.rfind("\n", 0, close) + 1
            edits.append(_Edit(dest, line_begin, line_begin, carried + "\n\n"))
        else:                                                 # split: a fresh block re-opens/creates Y
            block = f"\nnamespace {target_ns.name()} {{{uuid}}} {{\n\n{carried}\n\n}};\n"
            edits.append(_Edit(src_file, len(files[src_file]), len(files[src_file]), block))
    return edits


def _render_doc(text: str) -> str:
    """A DSM docstring block for ``text`` (``\"\"\"…\"\"\"``), or ``""`` to clear it."""
    if not text:
        return ""
    if "\n" in text:
        return '"""\n' + text + '\n"""'
    return '"""' + text + '"""'


def _reindent(block: str, indent: str) -> str:
    """Prefix every line of a docstring block with ``indent`` and a trailing newline. It is
    spliced at the anchor's line start (before the anchor's own indent), so the anchor line
    keeps its existing indentation — no trailing indent here, or it would double."""
    return "".join(indent + line + "\n" for line in block.splitlines())


# -- the migration ---------------------------------------------------------------------

def _read_tree(dsm_dir: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for name in sorted(os.listdir(dsm_dir)):
        if name.endswith(".dsm"):
            with open(os.path.join(dsm_dir, name), encoding="utf-8") as handle:
                files[name] = handle.read()
    return files


def _parse(files: dict[str, str], source_map=None):
    builder = V.DSMBuilder()
    for name, text in files.items():
        builder.append(name, text)
    report, dsm_defs, definitions = builder.parse(source_map=source_map)
    return builder, report, dsm_defs, definitions   # dsm_defs holds the pools (outside Definitions)


# Every TransformationDirectives edit now has a source-patch; the whole surface is covered.
# The guard stays (empty) as the fail-closed seam: a directive added upstream lands here first,
# refused up front rather than left to the digest oracle to reject after the fact.
_UNSUPPORTED: dict[str, str] = {}


def _refuse_unsupported(directives):
    reasons = [why for attr, why in _UNSUPPORTED.items() if getattr(directives, attr, None)]
    if reasons:
        raise NotImplementedError(
            "definitions_migrate does not yet patch these directives: "
            + ", ".join(sorted(reasons))
            + " — migrate the data with database_migrate.py and edit the .dsm by hand.")


def definitions_migrate(dsm_dir, transformation_module, out_dir, *, verify=True, on_notice=None):
    """Patch the ``.dsm`` tree under ``transformation_module.build_directives`` and
    write the result to ``out_dir``. Returns the parse report. ``on_notice`` (a callable taking
    one line) receives the findings that inform rather than refuse."""
    files = _read_tree(dsm_dir)
    if not files:
        raise ValueError(f"no .dsm files under {dsm_dir!r}")

    # 1. parse the source, collecting the source-map
    source_map = V.DSMSourceMap()
    builder, report, dsm_defs, source_defs = _parse(files, source_map)
    if report.has_error():
        raise ValueError("source .dsm does not parse:\n"
                         + "\n".join(f"  {e.source()}:{e.line()}:{e.pos()} {e.message()}"
                                     for e in report.errors()))

    # 2. the SAME transformation.py, from the source definitions
    directives = transformation_module.build_directives(source_defs)
    _refuse_unsupported(directives)

    # 3. engine oracle: the target definitions (+ the source->target type map). The engine sees
    #    the persistence schema only, so the pools are checked here, on the same up-front footing.
    rewriter, target_defs = DefinitionsRewriter.from_directives(source_defs, directives)
    _refuse_dangling_pools(dsm_defs, directives, on_notice)

    # 4. derive span-precise edits and apply them per file
    resolver = _Resolver(builder)
    edits = _derive(directives, source_map, resolver.resolve, files, rewriter, source_defs)
    by_file: dict[str, list[_Edit]] = {name: [] for name in files}
    for e in edits:
        by_file[e.source].append(e)
    patched = {name: _apply(text, by_file[name]) for name, text in files.items()}

    # 5. verify (oracle): re-parse the patched tree IN MEMORY, compare the definitions digest.
    #    Before the write, not after: a failed verify must leave no target tree behind (the
    #    codemod's twin of the data migration discarding a partial target).
    if verify:
        _vbuilder, vreport, _vdsm, vdefs = _parse(patched)
        if vreport.has_error():
            raise AssertionError("patched .dsm does not parse:\n"
                                 + "\n".join(f"  {e.source()}:{e.line()}:{e.pos()} {e.message()}"
                                             for e in vreport.errors()))
        target_digest = target_defs.const().hexdigest()
        if vdefs.hexdigest() != target_digest:
            raise AssertionError("verify failed: patched definitions digest "
                                 f"{vdefs.hexdigest()[:12]} != engine target "
                                 f"{target_digest[:12]}")

    # 6. write the fresh target tree
    os.makedirs(out_dir, exist_ok=True)
    for name, text in patched.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    return report


# -- CLI -------------------------------------------------------------------------------

def _load_transformation(path):
    """Import a transformation file by path; it must define
    ``build_directives(source_defs) -> TransformationDirectives`` (the SAME file the data
    migration uses). Arbitrary Python — the operator's own code, no sandbox."""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("_dsviper_transformation", path)
    if spec is None or spec.loader is None:
        print(f"cannot load transformation file: {path}", file=sys.stderr)
        sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "build_directives"):
        print(f"{path}: must define build_directives(source_defs) -> TransformationDirectives",
              file=sys.stderr)
        sys.exit(1)
    return module


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(
        description="Patch a hand-authored .dsm tree under a transformed schema — the "
                    "DSM-source twin of database_migrate.py. A structured codemod: the file "
                    "split, comments, ordering and formatting are preserved; the source tree "
                    "is read-only and a fresh target tree is written. The result is verified "
                    "against the migration engine (equal definitions digest) unless --no-verify.")
    parser.add_argument("transformation",
                        help="Python file defining build_directives(source_defs) — the same "
                             "file database_migrate.py uses")
    parser.add_argument("source_dir", help="directory of source .dsm files (read-only)")
    parser.add_argument("out_dir", help="directory to write the patched .dsm tree")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the digest self-check against the engine target")
    parser.add_argument("--force", action="store_true",
                        help="write into out_dir even if it is not empty")
    args = parser.parse_args()

    source_dir = os.path.expanduser(args.source_dir)
    if not os.path.isdir(source_dir):
        print(f"No such directory: {source_dir}", file=sys.stderr)
        sys.exit(1)
    out_dir = os.path.expanduser(args.out_dir)
    if os.path.isdir(out_dir) and os.listdir(out_dir) and not args.force:
        print(f"out_dir is not empty (use --force): {out_dir}", file=sys.stderr)
        sys.exit(1)

    module = _load_transformation(os.path.expanduser(args.transformation))
    try:
        definitions_migrate(source_dir, module, out_dir, verify=not args.no_verify,
                            on_notice=lambda line: print(line, file=sys.stderr))
    except (ValueError, AssertionError, NotImplementedError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"patched {source_dir} -> {out_dir}"
          + ("" if args.no_verify else " (verified against the engine target)"))


if __name__ == "__main__":
    main()
