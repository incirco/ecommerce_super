"""JSONPath subset traversal helpers used by Field Mapping (§5.4) and by
the Sync Record diff utilities. This module covers PATH ACCESS only — the
Field Mapping compiler and executor (the full §5 engine) are built when the
Field Mapping packet ships.

Supported syntax (subset of JSONPath, per §5.4):
  - Dot notation:        customer.gstin
  - Brackets iteration:  items[].item_code        (apply per row)
  - Filter predicates:   items[?type='CGST'].amount
  - Wildcards:           items[*].item_code        (synonym for items[])
  - Recursive descent:   ..hsn_code
  - Index access:        items[0].item_code

Not supported (deliberate; §5.4): full JSONPath script expressions, regex in
paths, recursive transformations.

These helpers are pure functions over dict/list trees. They make no Frappe
calls and have no side effects, so they are safely importable from anywhere.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_FILTER_RE = re.compile(r"\[\?([\w]+)\s*=\s*'([^']*)'\]")
_INDEX_RE = re.compile(r"\[(\d+)\]")
_WILDCARD_RE = re.compile(r"\[\*\]|\[\]")


def _split_segments(path: str) -> list[str]:
    """Split a JSONPath into navigable segments.

    `items[?type='CGST'].amount` → ['items', "[?type='CGST']", 'amount']
    `items[0].item_code`         → ['items', '[0]', 'item_code']
    `items[*].item_code`         → ['items', '[*]', 'item_code']
    `..hsn_code`                 → ['..', 'hsn_code']
    """
    segments: list[str] = []
    buf = ""
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == ".":
            if i + 1 < n and path[i + 1] == ".":
                if buf:
                    segments.append(buf)
                    buf = ""
                segments.append("..")
                i += 2
                continue
            if buf:
                segments.append(buf)
                buf = ""
            i += 1
            continue
        if ch == "[":
            if buf:
                segments.append(buf)
                buf = ""
            end = path.find("]", i)
            if end == -1:
                raise ValueError(f"Unclosed bracket in path: {path!r}")
            segments.append(path[i : end + 1])
            i = end + 1
            continue
        buf += ch
        i += 1
    if buf:
        segments.append(buf)
    return segments


def _apply_segment(nodes: Iterable[Any], segment: str) -> list[Any]:
    """Apply one segment to each current node, returning the flat list of
    resulting nodes."""
    result: list[Any] = []
    for node in nodes:
        if segment == "..":
            # Recursive descent — collect every nested dict/list.
            for sub in _walk_descendants(node):
                result.append(sub)
            continue
        if segment.startswith("["):
            if _WILDCARD_RE.fullmatch(segment):
                if isinstance(node, list):
                    result.extend(node)
                continue
            idx_match = _INDEX_RE.fullmatch(segment)
            if idx_match:
                idx = int(idx_match.group(1))
                if isinstance(node, list) and 0 <= idx < len(node):
                    result.append(node[idx])
                continue
            filt_match = _FILTER_RE.fullmatch(segment)
            if filt_match:
                fkey, fval = filt_match.group(1), filt_match.group(2)
                if isinstance(node, list):
                    for item in node:
                        if isinstance(item, dict) and str(item.get(fkey)) == fval:
                            result.append(item)
                continue
            raise ValueError(f"Unsupported bracket expression: {segment!r}")
        # Plain key.
        if isinstance(node, dict) and segment in node:
            result.append(node[segment])
    return result


def _walk_descendants(node: Any) -> Iterable[Any]:
    """Yield every dict/list descendant of `node`, including itself."""
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk_descendants(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_descendants(item)


def get_path(payload: Any, path: str) -> list[Any]:
    """Return all values at `path` within `payload`. Always returns a list,
    even for scalar single-target paths, because path semantics here can
    fan out (e.g. `items[].amount` returns one value per row)."""
    if not path:
        return [payload]
    nodes: list[Any] = [payload]
    for segment in _split_segments(path):
        nodes = _apply_segment(nodes, segment)
        if not nodes:
            return []
    return nodes


def get_first(payload: Any, path: str, default: Any = None) -> Any:
    """Return the first value at `path`, or `default` if no match."""
    values = get_path(payload, path)
    return values[0] if values else default


def sum_path(payload: Any, path: str) -> float:
    """Sum all numeric values at `path`. Non-numeric matches are skipped."""
    total = 0.0
    for v in get_path(payload, path):
        try:
            total += float(v)
        except TypeError, ValueError:
            continue
    return total


def filter_path(payload: Any, path: str, predicate: callable) -> list[Any]:
    """Return all values at `path` for which `predicate(value)` is truthy."""
    return [v for v in get_path(payload, path) if predicate(v)]
