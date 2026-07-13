"""Example migration file — defines the schema change; the tool loads and runs it:

    python3 database_migrate.py examples/migration_shop_v2.py old.db new.db --verify

`build_directives` receives the source's live `Definitions`, so you build directives
against real type and field names instead of guessing.
"""

from dsviper import Type, ValueString

from dsviper_database_tools import TransformationDirectives


def build_directives(source_defs):
    d = TransformationDirectives()

    # --- renames (family 1, size-preserving) ---
    d.rename_field("Shop::Customer", "fullname", "full_name")
    d.rename_case("Shop::OrderStatus", "Pending", "AwaitingPayment")

    # --- shape changes (family 2) ---
    d.add_field("Shop::Customer", "email", ValueString(""))         # A: seeded default
    d.drop_field("Shop::Customer", "legacyId")                      # A
    d.retype_field("Shop::Order", "amountCents", Type.INT64)        # A: int32→int64 widening
    d.retype_field("Shop::Order", "quantity", Type.INT16, policy="saturate")   # B: narrowing
    d.add_case("Shop::OrderStatus", "Refunded")                     # A: at end
    d.remove_case("Shop::OrderStatus", "Legacy", ("map-case", "Cancelled"))    # B

    return d
