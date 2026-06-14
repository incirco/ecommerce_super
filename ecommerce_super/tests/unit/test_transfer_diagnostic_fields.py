"""gh#26 retest — `trace_dn` must only query fields that actually
exist on EasyEcom Transfer Map.

mmpl16 (2026-06-13): clicking the §10 Trace button on DL-260550 hit:
  `MySQLdb.OperationalError: (1054, "Unknown column 'branch' in 'SELECT'")`
The diagnostic was speculatively reading a `branch` field that
doesn't exist on the DocType. Aborted before any artifact reached
the FDE, blocking §10 investigation entirely.

This test freezes the column list against the actual DocType JSON,
catching any future regression where a column ref is added to the
diagnostic without first being added to the DocType.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path


_DOCTYPE_JSON = Path(__file__).parents[3] / (
    "ecommerce_super/easyecom/doctype/easyecom_transfer_map/"
    "easyecom_transfer_map.json"
)


def _transfer_map_fieldnames() -> set[str]:
    """Read the on-disk Transfer Map JSON and return its fieldnames."""
    data = json.loads(_DOCTYPE_JSON.read_text())
    fieldnames = set()
    for field in data.get("fields", []):
        fn = field.get("fieldname")
        if fn:
            fieldnames.add(fn)
    return fieldnames


def _trace_dn_queried_columns() -> set[str]:
    """Parse the diagnostic module's source and extract the column list
    inside the `transfer_map` get_value call.

    Source-level parse rather than runtime introspection because the
    function takes a live DN docname and runs DB queries — we want a
    pure check that doesn't require a live Frappe env."""
    src_path = (
        Path(__file__).parents[3]
        / "ecommerce_super/easyecom/api/transfer_diagnostic.py"
    )
    src = src_path.read_text()
    # The query is the only one inside `trace_dn` against EasyEcom
    # Transfer Map. Find the column list literal.
    marker = '"EasyEcom Transfer Map"'
    idx = src.find(marker)
    assert idx > 0, "transfer_diagnostic.py no longer references EasyEcom Transfer Map"
    # Look for the surrounding `[ ... ]` list of column strings after
    # the marker — first one is the columns arg.
    list_start = src.find("[", idx)
    list_end = src.find("]", list_start)
    list_body = src[list_start : list_end + 1]
    # Extract bare string literals.
    cols: set[str] = set()
    for chunk in list_body.split(","):
        token = chunk.strip().strip("[]")
        if token.startswith('"') and token.endswith('"'):
            cols.add(token.strip('"'))
    return cols


class TestTransferDiagnosticColumnList(unittest.TestCase):
    def test_every_queried_column_exists_on_doctype(self) -> None:
        queried = _trace_dn_queried_columns()
        # `name` is always queryable on any DocType even though it's
        # not listed in fields[]; exclude it.
        queried_real = queried - {"name"}
        existing = _transfer_map_fieldnames()
        missing = queried_real - existing
        self.assertEqual(
            missing,
            set(),
            f"trace_dn queries columns that don't exist on EasyEcom Transfer Map: "
            f"{sorted(missing)} (existing fields: {sorted(existing)})",
        )

    def test_known_safe_columns_present_in_query(self) -> None:
        """Regression — make sure removing `branch` didn't accidentally
        drop the columns the FDE actually needs from the trace."""
        queried = _trace_dn_queried_columns()
        for required in {"status", "ee_order_id", "flag_reason"}:
            self.assertIn(required, queried)


if __name__ == "__main__":
    unittest.main()
