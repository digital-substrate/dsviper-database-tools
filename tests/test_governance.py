"""database-tools inherits Viper's DSM governance and fails closed.

The engine builds the target `Definitions` through the binding's construction gates
(`create_structure` / `add_field` / `add_case` / `TypeStructureDescriptor(documentation=…)`),
so a directive that would mint a non-DSM-expressible **identifier** or **docstring** is refused
*there*, reached through our build — never a silent bad name, consistent with the
total-or-explicit-refusal invariant. These tests pin that boundary as database-tools' own contract.

The governance is a property of the binding, and its two halves shipped at different times
(the identifier policy is in the published floor; the docstring rule shipped later). Each "refused"
test is therefore guarded by a live probe of the installed binding and skips cleanly where the rule
is absent — so the suite documents the contract without breaking on an older peer.
"""

import unittest

import dsviper as V

from dsviper_database_tools import TransformationDirectives, DefinitionsRewriter

NS = V.NameSpace(V.ValueUUId("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "Demo")


def _src_order():
    """A one-field source struct `Demo::Order { amount: int32 }` to migrate from."""
    d = V.TypeStructureDescriptor("Order")
    d.add_field("amount", V.Type.INT32)
    defs = V.Definitions()
    defs.create_structure(NS, d)
    return defs


def _identifier_governance_active():
    """True iff the binding enforces the struct-field identifier policy (reserved word 'set')."""
    try:
        d = V.TypeStructureDescriptor("Probe")
        d.add_field("set", V.Type.INT32)                       # 'set' is Viper-reserved
        return False
    except Exception:
        return True


def _docstring_governance_active():
    """True iff the binding rejects a non-DSM-expressible docstring (contains '\"\"\"')."""
    try:
        V.TypeStructureDescriptor("Probe", documentation='a"""b')
        return False
    except Exception:
        return True


_ID_GOV = _identifier_governance_active()
_DOC_GOV = _docstring_governance_active()


class TestIdentifierGovernanceFailsClosed(unittest.TestCase):
    """A `rename_field` to a non-DSM-expressible name is refused at target construction — the
    binding's identifier policy (`^[A-Za-z][A-Za-z0-9_]*$`, no reserved word / DSM keyword),
    reached through our build, not swallowed. A valid rename is accepted."""

    def _rename(self, new_name):
        d = TransformationDirectives()
        d.rename_field("Demo::Order", "amount", new_name)
        DefinitionsRewriter.from_directives(_src_order(), d)

    def test_valid_rename_is_accepted(self):
        self._rename("total")                                  # a plain identifier: no error

    @unittest.skipUnless(_ID_GOV, "identifier DSM governance absent from this binding")
    def test_reserved_word_refused(self):
        for bad in ("set", "type", "class"):                   # Viper- / C++- / Python-reserved
            with self.subTest(name=bad), self.assertRaises(Exception):
                self._rename(bad)

    @unittest.skipUnless(_ID_GOV, "identifier DSM governance absent from this binding")
    def test_dsm_keyword_refused(self):
        for bad in ("struct", "enum", "namespace"):            # DSM keywords
            with self.subTest(name=bad), self.assertRaises(Exception):
                self._rename(bad)

    @unittest.skipUnless(_ID_GOV, "identifier DSM governance absent from this binding")
    def test_non_identifier_shape_refused(self):
        for bad in ("bad name", "2nd", "with-dash", ""):       # space / leading digit / dash / empty
            with self.subTest(name=bad), self.assertRaises(Exception):
                self._rename(bad)


class TestDocstringGovernanceFailsClosed(unittest.TestCase):
    """Authoring a non-DSM-expressible docstring (contains '\"\"\"' or invalid UTF-8) via
    `document_field` is refused at construction. A plain docstring — backslashes, single/double
    quotes, prose — is accepted."""

    def _document(self, doc):
        d = TransformationDirectives()
        d.document_field("Demo::Order", "amount", doc)
        DefinitionsRewriter.from_directives(_src_order(), d)

    def test_plain_docstring_is_accepted(self):
        self._document('the amount, in cents; C:\\path and "quotes" are fine')

    @unittest.skipUnless(_DOC_GOV, "docstring DSM governance absent from this binding (pre-1.2.6)")
    def test_triple_quote_docstring_refused(self):
        with self.assertRaises(Exception):
            self._document('a bad """ docstring')


if __name__ == "__main__":
    unittest.main()
