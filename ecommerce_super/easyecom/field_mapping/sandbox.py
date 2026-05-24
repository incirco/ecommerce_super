"""Sandboxed evaluator for FDE-authored Field Mapping expressions.

Three places execute FDE-authored expressions (SPEC §5.5, §5.6, §5.7):
  - rule `condition`        → allowed names: source_doc / source_payload
  - computed-field `expression` → allowed names: source_doc, source_payload,
                                  get_path(), sum_path(), filter_path()
  - `custom_python` transformer → same as computed, plus `value`
                                  (the field value the transformer operates on)

The security model (per the §5 packet's SECURITY block):

1.  Two layers of defence.
    - **Compile time**: `validate_expression()` does an AST walk and rejects
      anything that references a name outside the documented allow-list,
      touches a dunder attribute, or contains import statements. This
      catches malicious expressions when the FDE saves the ruleset — not
      months later when a live record hits the rule.
    - **Run time**: `evaluate_expression()` invokes Frappe's
      `safe_eval()` — which strips builtins, blocks the walrus operator,
      and provides the framework's safe globals set. Defence in depth: a
      runtime expression that somehow bypassed compile-time validation
      still cannot import, open files, or call subprocess.

2.  We never write our own `eval`/`exec` wrapper or expose `__import__`,
    `open`, `os`, `sys`, `subprocess`. The injected globals at run time
    contain only the documented names — nothing else.

3.  Compile-time validation is mandatory before any expression reaches
    `evaluate_expression`. Callers (the compiler) must call
    `validate_expression()` first and surface `FieldMappingCompileError`
    to the FDE so the ruleset save fails before the broken expression
    can be persisted.
"""

from __future__ import annotations

import ast
from typing import Any, Callable

from frappe.utils.safe_exec import safe_eval

from ecommerce_super.easyecom.exceptions import FieldMappingCompileError

# ----- Allowed name sets per expression role (§5.5, §5.6, §5.7) -----


ALLOWED_NAMES_CONDITION: frozenset[str] = frozenset({"source_doc", "source_payload"})

ALLOWED_NAMES_COMPUTED: frozenset[str] = frozenset(
    {"source_doc", "source_payload", "get_path", "sum_path", "filter_path"}
)

# custom_python additionally exposes `value` (the current field value the
# transformer operates on). Same support helpers as computed fields.
ALLOWED_NAMES_CUSTOM_PYTHON: frozenset[str] = frozenset(
    {"source_doc", "source_payload", "get_path", "sum_path", "filter_path", "value"}
)

# Hard-blocklist of names — even if a future allow-list bug exposes one of
# these, the compile-time check still rejects. Defence in depth.
FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        # builtins that would let an expression reach beyond its sandbox
        "__import__",
        "__builtins__",
        "__loader__",
        "__spec__",
        "open",
        "exec",
        "eval",
        "compile",
        "globals",
        "locals",
        "vars",
        "dir",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "help",
        # modules that should never be reachable
        "os",
        "sys",
        "subprocess",
        "socket",
        "shutil",
        "importlib",
        "pickle",
        "marshal",
        "ctypes",
        "frappe",
        "requests",
        "urllib",
        "http",
        # attribute traversal escape vectors
        "__class__",
        "__bases__",
        "__subclasses__",
        "__mro__",
        "__dict__",
        "__globals__",
        "__getattribute__",
    }
)


# ----- Compile-time validation -----


def validate_expression(
    expression: str,
    allowed_names: frozenset[str],
    *,
    rule_label: str,
) -> None:
    """Validate an expression at COMPILE time (Field Mapping save).

    Walks the AST and rejects:
      - empty / non-parsable expressions
      - `import` / `from ... import` statements
      - dunder attribute access (anything starting and ending with `__`)
      - name references (`ast.Load`) not in `allowed_names`
      - explicitly forbidden names (defence in depth — should be subset of above)

    Raises:
      FieldMappingCompileError with `parse_error` describing the violation.
      Caller (the compiler) should surface this to the FDE so the ruleset
      save fails before the bad expression can be persisted.
    """
    if not expression or not expression.strip():
        raise FieldMappingCompileError(
            f"Empty expression in {rule_label}",
            parse_error="expression is empty",
        )

    code = expression.strip()
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError as e:
        raise FieldMappingCompileError(
            f"Syntax error in {rule_label}: {e.msg}",
            parse_error=f"{type(e).__name__}: {e}",
        ) from e

    for node in ast.walk(tree):
        # No import statements (defensive — `mode="eval"` should prevent
        # this at parse, but a deeply-nested string-eval trick could try).
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise FieldMappingCompileError(
                f"Import statement not allowed in {rule_label}",
                parse_error="import statements forbidden",
            )

        # No dunder attribute access — blocks __class__, __mro__, __subclasses__
        # escape chains.
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise FieldMappingCompileError(
                    f"Dunder attribute access ({node.attr}) not allowed in {rule_label}",
                    parse_error=f"dunder attribute: {node.attr}",
                )

        # Check name references — only the documented allow-list is permitted.
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
            if name in FORBIDDEN_NAMES:
                raise FieldMappingCompileError(
                    f"Forbidden name '{name}' in {rule_label}",
                    parse_error=f"forbidden name: {name}",
                )
            if name not in allowed_names:
                raise FieldMappingCompileError(
                    f"Name '{name}' not in allowed set {sorted(allowed_names)} for {rule_label}",
                    parse_error=f"disallowed name: {name}",
                )

        # Reject calls to dynamic-execution sinks even if the name check
        # didn't catch them (e.g. somehow exposed via globals).
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {
                "eval",
                "exec",
                "compile",
                "__import__",
            }:
                raise FieldMappingCompileError(
                    f"Call to {func.id}() not allowed in {rule_label}",
                    parse_error=f"forbidden call: {func.id}",
                )


# ----- Runtime evaluation -----


def evaluate_expression(
    expression: str,
    *,
    eval_globals: dict[str, Any],
) -> Any:
    """Evaluate a pre-validated expression with bounded globals.

    Pre-condition: `validate_expression()` MUST have been called at compile
    time for this expression with the appropriate `allowed_names` set.
    This is the run-time path; it uses Frappe's `safe_eval` for an
    additional layer of defence (zero builtins, blocked AST nodes, the
    framework's safe globals set).

    `eval_globals` should contain ONLY the documented names for the
    expression's role (e.g. {"source_doc": ..., "source_payload": ...}
    for a condition). The compiler is responsible for assembling this dict.
    """
    return safe_eval(expression.strip(), eval_globals=dict(eval_globals))


# ----- Helpers exposed to computed/custom_python expressions -----


def build_path_helpers() -> dict[str, Callable]:
    """Return the get_path / sum_path / filter_path helpers as a dict
    ready to be merged into eval_globals.

    Kept here (rather than re-imported in every call site) so the binding
    is centralised — a future change to which helpers we expose is one
    edit, not a sweep across the engine.
    """
    from ecommerce_super.easyecom.field_mapping import path as _path

    return {
        "get_path": _path.get_path,
        "sum_path": _path.sum_path,
        "filter_path": _path.filter_path,
    }
