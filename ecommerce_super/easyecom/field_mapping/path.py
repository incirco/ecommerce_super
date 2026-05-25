"""JSONPath-subset for Field Mapping (SPEC §5.4).

Re-exports the read helpers from `easyecom/utils/jsonpath.py` (already
built as a foundation utility) and adds:

  - `validate_path()` — compile-time path-syntax check, used by the
    compiler so a bad path fails at ruleset save, not at run time.
  - `set_path()` — write a value at a dot-path into a target dict;
    needed by the executor when projecting transformed values onto the
    EasyEcom payload shape.
  - `path_has_iteration()` — does the path traverse an array
    (`items[]`, `items[*]`, `items[?...]`, `items[0]`)? The executor uses
    this to decide per-element vs scalar application.

Path syntax supported (subset of JSONPath, §5.4):
  customer.gstin                  dot
  items[].item_code               brackets iteration
  items[*].item_code              wildcard (synonym for [])
  items[?type='CGST'].amount      filter predicate
  items[0].item_code              index access
  ..hsn_code                      recursive descent (read-only)

Deliberately NOT supported (§5.4): full JSONPath script expressions,
regex matching in paths, recursive transformations. Use a rule's
`condition` field for those — it has the full sandboxed-Python escape
hatch.
"""

from __future__ import annotations

import re
from typing import Any

from ecommerce_super.easyecom.exceptions import FieldMappingCompileError

# Re-export the read helpers — these are already implemented as part of
# the §3+§4 foundation utilities.
from ecommerce_super.easyecom.utils.jsonpath import (
    filter_path,
    get_first,
    get_path,
    sum_path,
)

# Valid segment patterns (one per §5.4 syntax)
_VALID_SEGMENT_RE = re.compile(
    r"^("
    r"\.\.|"  # recursive descent
    r"\[\]|\[\*\]|"  # iteration / wildcard
    r"\[\d+\]|"  # index access
    r"\[\?[\w]+\s*=\s*'[^']*'\]|"  # filter predicate
    # Plain key — allows internal spaces because EasyEcom payloads
    # legitimately ship literal-space keys (e.g. 'address type',
    # 'phone number'). §8a packet explicitly mandates the parser
    # tolerate space-bearing source keys. Pattern: starts with a
    # letter/underscore, ends with a word char (no trailing space,
    # no whitespace-only segments), with word chars or single spaces
    # in between.
    r"[A-Za-z_](?:[\w ]*[\w])?"
    r")$"
)

# Forbidden patterns we explicitly reject (rather than relying on
# allow-list completeness) — gives a clearer error to the FDE.
_FORBIDDEN_PATTERNS = [
    (re.compile(r"\$\.|\$\["), "JSONPath '$' root selector — not supported, omit it"),
    (re.compile(r"@\.|@\["), "JSONPath '@' current selector — not supported"),
    (re.compile(r"\[\?\s*@|\[\?\s*\("), "Script-based filters — not supported"),
    (re.compile(r"\.\*\."), "Wildcard outside brackets — use [*] inside brackets"),
]


def validate_path(path: str, *, rule_label: str) -> None:
    """Validate a JSONPath-subset expression at compile time.

    Raises FieldMappingCompileError on syntax violations. Caller (the
    compiler) surfaces this to the FDE so the ruleset save fails before
    the bad path can be persisted.
    """
    if not isinstance(path, str) or not path.strip():
        raise FieldMappingCompileError(
            f"Empty path in {rule_label}",
            parse_error="path is empty",
        )

    p = path.strip()

    for pattern, message in _FORBIDDEN_PATTERNS:
        if pattern.search(p):
            raise FieldMappingCompileError(
                f"Path {p!r} in {rule_label}: {message}",
                parse_error=f"forbidden pattern: {message}",
            )

    # Walk the path and validate every segment.
    try:
        segments = _split_segments(p)
    except ValueError as e:
        raise FieldMappingCompileError(
            f"Path {p!r} in {rule_label}: {e}",
            parse_error=str(e),
        ) from e

    for seg in segments:
        if not _VALID_SEGMENT_RE.match(seg):
            raise FieldMappingCompileError(
                f"Path segment {seg!r} in {rule_label} is not a supported §5.4 syntax",
                parse_error=f"invalid segment: {seg}",
            )


def path_has_iteration(path: str) -> bool:
    """True if the path traverses an array (any of `[]`, `[*]`, `[?...]`,
    `..`). Index access `[N]` is treated as scalar — it selects one element.
    """
    if not isinstance(path, str):
        return False
    if "[]" in path or "[*]" in path or "[?" in path or ".." in path:
        return True
    return False


def set_path(target: dict, path: str, value: Any) -> None:
    """Write `value` at `path` inside the `target` dict, creating
    intermediate dicts as needed.

    Supported targets:
      - plain dot paths            → `customer.gstin`
      - index access               → `items[0].sku` (creates list as needed)
      - bracket iteration          → `items[].sku` is NOT supported here;
        callers iterating over source rows must call set_path per row with
        an index-form target (`items[0].sku`, `items[1].sku`, ...).

    The deliberate restriction keeps the executor in charge of iteration
    semantics; this helper is the per-row writer.
    """
    if not isinstance(target, dict):
        raise TypeError("set_path target must be a dict")
    if not path:
        raise ValueError("set_path requires a non-empty path")

    segments = _split_segments(path)
    cur: Any = target

    for i, seg in enumerate(segments):
        is_last = i == len(segments) - 1
        if seg.startswith("["):
            # Only [N] index access is supported for set.
            if not (seg.startswith("[") and seg.endswith("]") and seg[1:-1].isdigit()):
                raise ValueError(
                    f"set_path supports only [N] index access in path segments, got {seg!r}. "
                    "For [] iteration, the executor must call set_path per row with an index."
                )
            idx = int(seg[1:-1])
            # Parent of this segment must be a list; if it's currently a dict
            # (because the previous segment created it), we need to coerce.
            if not isinstance(cur, list):
                raise ValueError(
                    f"set_path: segment {seg!r} expects a list but cur is {type(cur).__name__}"
                )
            while len(cur) <= idx:
                cur.append({})
            if is_last:
                cur[idx] = value
            else:
                if not isinstance(cur[idx], dict):
                    cur[idx] = {}
                cur = cur[idx]
            continue

        # Plain key.
        if seg == "..":
            raise ValueError("set_path does not support recursive descent `..`")

        if is_last:
            cur[seg] = value
        else:
            # Peek ahead: if next segment is [N], create a list; otherwise a dict.
            next_seg = segments[i + 1]
            if (
                next_seg.startswith("[")
                and next_seg.endswith("]")
                and next_seg[1:-1].isdigit()
            ):
                if seg not in cur or not isinstance(cur[seg], list):
                    cur[seg] = []
            else:
                if seg not in cur or not isinstance(cur[seg], dict):
                    cur[seg] = {}
            cur = cur[seg]


# ----- Internal: re-use the foundation's segment splitter -----


def _split_segments(path: str) -> list[str]:
    """Split a path into segments. Mirrors `utils.jsonpath._split_segments`
    so validation and set_path agree on segmentation semantics. Re-imported
    here rather than re-implemented to avoid two parsers drifting apart."""
    from ecommerce_super.easyecom.utils.jsonpath import _split_segments as _impl

    return _impl(path)


# Public re-exports for callers (executor, sandbox helpers).
__all__ = [
    "filter_path",
    "get_first",
    "get_path",
    "sum_path",
    "validate_path",
    "path_has_iteration",
    "set_path",
]
