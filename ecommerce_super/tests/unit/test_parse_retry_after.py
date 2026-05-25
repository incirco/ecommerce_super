"""Unit tests for client._parse_retry_after — exercises the tuple-form
`except (TypeError, ValueError):` clause that's been the recurring
Py2-syntax foundation defect (jsonpath.py, concurrency.py, client.py).

EE's Retry-After header carries a non-numeric string in some failure
modes (HTTP-date format per RFC 9110, or empty in misbehaving proxies).
Our parser only handles the integer-seconds form (§3.10) and must
gracefully fall back to None rather than raising — pre-fix, the
broken-syntax clause meant any non-numeric value would have either
raised `SyntaxError` in older Python OR caught only TypeError (since
in older Python 3 `except A, B:` was a SyntaxError, and in Python 3.14
the comma is parsed as a tuple expression so the catch worked
incidentally).

Now that all instances are normalised to the tuple form, this test
locks in the behaviour: non-numeric and weird-type inputs return None,
integer-like strings parse.
"""

from __future__ import annotations

import unittest

from ecommerce_super.easyecom.client.client import _parse_retry_after


class TestParseRetryAfter(unittest.TestCase):
    def test_none_input_returns_none(self) -> None:
        self.assertIsNone(_parse_retry_after(None))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(_parse_retry_after(""))

    def test_integer_seconds_string_parses(self) -> None:
        self.assertEqual(_parse_retry_after("30"), 30)
        self.assertEqual(_parse_retry_after("1"), 1)
        self.assertEqual(_parse_retry_after("3600"), 3600)

    def test_zero_seconds_parses(self) -> None:
        self.assertEqual(_parse_retry_after("0"), 0)

    def test_http_date_returns_none(self) -> None:
        """RFC 9110 lets a server send Retry-After: <HTTP-date>. We don't
        parse those — fall back to None, never raise."""
        self.assertIsNone(_parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT"))

    def test_garbage_string_returns_none(self) -> None:
        """The pre-fix `except TypeError, ValueError:` would have caught
        only TypeError under the old Py3 SyntaxError reading. The fixed
        tuple form catches both and returns None for non-numeric input."""
        self.assertIsNone(_parse_retry_after("not-a-number"))
        self.assertIsNone(_parse_retry_after("60s"))
        self.assertIsNone(_parse_retry_after("abc"))

    def test_float_string_returns_none(self) -> None:
        """int('60.5') raises ValueError — the except clause must catch it.
        This is the specific case that exercises the ValueError half of
        the `(TypeError, ValueError)` tuple, which the pre-fix Py2-form
        clause would not have caught under stricter parsers."""
        self.assertIsNone(_parse_retry_after("60.5"))
        self.assertIsNone(_parse_retry_after("1.0"))

    def test_non_string_types_return_none(self) -> None:
        """int() with a wrong-type arg raises TypeError — the other half
        of the tuple. Exercised here so a future regression where someone
        accidentally narrows to `except ValueError:` would fail."""
        self.assertIsNone(_parse_retry_after([60]))  # type: ignore[arg-type]
        self.assertIsNone(_parse_retry_after({"seconds": 60}))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
