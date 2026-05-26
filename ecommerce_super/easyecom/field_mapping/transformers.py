"""Closed transformer vocabulary for Field Mapping rules (SPEC §5.5).

A transformer is a small, named function applied to a single field value
during ruleset execution. Each transformer is paired with an args-contract
validator the compiler calls at ruleset save time (§5.9.1) — bad args fail
the save, not the execution.

The vocabulary is closed by design (CLAUDE.md anti-pattern: "Bypassing the
Field Mapping engine"). `custom_python` is the FDE escape hatch; everything
else is a documented, validated primitive.

Each transformer has the signature:
    fn(value, *, args, context) -> transformed_value

`context` is a TransformContext dataclass holding the source doc/payload,
direction, company, and the resolved computed-field values. Transformers
that don't need context (most of them) ignore it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable

import frappe
from frappe.utils import get_datetime

from ecommerce_super.easyecom.exceptions import (
    FieldMappingCompileError,
    FieldMappingRuleError,
)
from ecommerce_super.easyecom.field_mapping import sandbox

# ----- Execution context passed to each transformer call -----


@dataclass
class TransformContext:
    """Per-call context for a transformer.

    The executor builds this once per record-level invocation and passes it
    into each transformer call. Most transformers ignore most fields; the
    few that need context (lookup_id, computed, custom_python, conditional_constant)
    document which fields they read.
    """

    direction: str  # "push" | "pull"
    source_doc: Any = None  # ERPNext document (push) or None (pull)
    source_payload: dict | None = None  # EE payload dict (pull) or None (push)
    company: str | None = None  # Company name for scoped lookups
    computed_values: dict[str, Any] = field(default_factory=dict)
    rule_label: str = "<unknown>"  # For error messages


# ----- Args-contract sentinel & helpers -----


class _NoArgs:
    """Marker: transformer takes no args. Validator rejects any non-empty dict."""


def _require_keys(args: dict, required: list[str], *, transformer: str) -> None:
    missing = [k for k in required if k not in args]
    if missing:
        raise FieldMappingCompileError(
            f"Transformer {transformer!r} missing required arg(s): {missing}",
            parse_error=f"missing args: {missing}",
        )


def _require_no_args(args: dict | None, *, transformer: str) -> None:
    """No-args transformers are tolerant: they ignore any args present.

    This matters because a single rule has one `transform_args` field
    shared between transform_push and transform_pull (§5.3). When the
    two directions use different transformers — e.g. push=computed
    (needs {name}), pull=identity (needs nothing) — the args are
    populated for the push side. The pull-side identity validator
    must not reject them. Each transformer's function reads only the
    keys it needs.
    """
    return


# ----- Identity / boolean / string -----


def _t_identity(value: Any, *, args: dict | None, context: TransformContext) -> Any:
    return value


def _t_bool_to_yn(value: Any, *, args: dict | None, context: TransformContext) -> str:
    if value is None:
        return "N"
    if isinstance(value, bool):
        return "Y" if value else "N"
    # Frappe Check fields surface as int 0/1; treat truthy generically.
    return "Y" if value else "N"


def _t_yn_to_bool(
    value: Any, *, args: dict | None, context: TransformContext
) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        normalised = value.strip().upper()
        if normalised in ("Y", "YES", "TRUE", "1"):
            return True
        if normalised in ("N", "NO", "FALSE", "0"):
            return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raise FieldMappingRuleError(
        f"yn_to_bool: cannot convert {value!r} to bool",
        transform="yn_to_bool",
        source_value=value,
    )


def _t_str_lower(
    value: Any, *, args: dict | None, context: TransformContext
) -> str | None:
    if value is None:
        return None
    return str(value).lower()


def _t_str_upper(
    value: Any, *, args: dict | None, context: TransformContext
) -> str | None:
    if value is None:
        return None
    return str(value).upper()


def _t_str_strip(
    value: Any, *, args: dict | None, context: TransformContext
) -> str | None:
    if value is None:
        return None
    return str(value).strip()


# ----- Date / datetime -----


_PYTHON_FORMAT_MAP = {
    "YYYY": "%Y",
    "MM": "%m",
    "DD": "%d",
    "HH": "%H",
    "mm": "%M",
    "ss": "%S",
}


def _convert_date_format(fmt: str) -> str:
    """Translate the FDE-friendly format ('YYYY-MM-DD') to Python strftime
    tokens ('%Y-%m-%d'). Order matters — longer tokens first."""
    out = fmt
    # Sort by length descending so YYYY beats YY, MM beats M, etc.
    for token in sorted(_PYTHON_FORMAT_MAP.keys(), key=len, reverse=True):
        out = out.replace(token, _PYTHON_FORMAT_MAP[token])
    return out


def _t_date_format(value: Any, *, args: dict, context: TransformContext) -> str | None:
    if value is None or value == "":
        return None
    src_fmt = _convert_date_format(args["from"])
    dst_fmt = _convert_date_format(args["to"])
    if isinstance(value, (datetime, date)):
        dt = value
    else:
        try:
            dt = datetime.strptime(str(value), src_fmt)
        except ValueError as e:
            raise FieldMappingRuleError(
                f"date_format: {value!r} does not match {args['from']!r}",
                transform="date_format",
                source_value=value,
            ) from e
    return dt.strftime(dst_fmt)


def _t_datetime_to_iso(
    value: Any, *, args: dict | None, context: TransformContext
) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        # Frappe surfaces datetimes as strings; coerce via get_datetime.
        try:
            value = get_datetime(value)
        except Exception as e:  # noqa: BLE001 — get_datetime raises generic Exception
            raise FieldMappingRuleError(
                f"datetime_to_iso: cannot parse {value!r}",
                transform="datetime_to_iso",
                source_value=value,
            ) from e
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise FieldMappingRuleError(
        f"datetime_to_iso: unsupported type {type(value).__name__}",
        transform="datetime_to_iso",
        source_value=value,
    )


def _t_iso_to_datetime(
    value: Any, *, args: dict | None, context: TransformContext
) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        # ISO 8601: handle trailing 'Z' (Python <3.11 quirk persists in libs).
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError as e:
        raise FieldMappingRuleError(
            f"iso_to_datetime: cannot parse {value!r}",
            transform="iso_to_datetime",
            source_value=value,
        ) from e


# ----- Type coercion -----


def _t_int_to_str(
    value: Any, *, args: dict | None, context: TransformContext
) -> str | None:
    if value is None:
        return None
    return str(int(value))


def _t_str_to_int(
    value: Any, *, args: dict | None, context: TransformContext
) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as e:
        raise FieldMappingRuleError(
            f"str_to_int: cannot convert {value!r}",
            transform="str_to_int",
            source_value=value,
        ) from e


def _t_float_to_str(
    value: Any, *, args: dict | None, context: TransformContext
) -> str | None:
    if value is None:
        return None
    return str(float(value))


def _t_str_to_float(
    value: Any, *, args: dict | None, context: TransformContext
) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as e:
        raise FieldMappingRuleError(
            f"str_to_float: cannot convert {value!r}",
            transform="str_to_float",
            source_value=value,
        ) from e


# ----- Currency (INR rupees ↔ paise — the EE convention) -----


def _t_currency_to_paise(
    value: Any, *, args: dict | None, context: TransformContext
) -> int | None:
    if value is None or value == "":
        return None
    try:
        rupees = Decimal(str(value))
    except (TypeError, InvalidOperation) as e:
        raise FieldMappingRuleError(
            f"currency_to_paise: cannot parse {value!r} as decimal",
            transform="currency_to_paise",
            source_value=value,
        ) from e
    # Multiply by 100, round half-up to whole paise (no fractional paise on the wire).
    paise = (rupees * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(paise)


def _t_paise_to_currency(
    value: Any, *, args: dict | None, context: TransformContext
) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        paise = Decimal(str(value))
    except (TypeError, InvalidOperation) as e:
        raise FieldMappingRuleError(
            f"paise_to_currency: cannot parse {value!r} as decimal",
            transform="paise_to_currency",
            source_value=value,
        ) from e
    return (paise / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ----- Frappe lookups (need DB access; lazy frappe import already at module top) -----


def _t_lookup_id(value: Any, *, args: dict, context: TransformContext) -> Any:
    """Resolve a Frappe DocType `name` to its EE-side ID stored on a custom field.

    args:
      doctype       — the Frappe DocType to look up
      target_field  — the field on that doc whose value we return
    """
    if value is None or value == "":
        return None
    doctype = args["doctype"]
    target_field = args["target_field"]
    ee_id = frappe.db.get_value(doctype, value, target_field)
    if ee_id is None:
        raise FieldMappingRuleError(
            f"lookup_id: no {doctype} {value!r} or its {target_field} is empty",
            transform="lookup_id",
            source_value=value,
        )
    return ee_id


def _t_reverse_lookup_id(value: Any, *, args: dict, context: TransformContext) -> Any:
    """EE ID → Frappe document `name`.

    args:
      doctype       — the Frappe DocType to search
      source_field  — the field on the doc whose value matches `value`
    """
    if value is None or value == "":
        return None
    doctype = args["doctype"]
    source_field = args["source_field"]
    name = frappe.db.get_value(doctype, {source_field: value}, "name")
    if name is None:
        raise FieldMappingRuleError(
            f"reverse_lookup_id: no {doctype} with {source_field}={value!r}",
            transform="reverse_lookup_id",
            source_value=value,
        )
    return name


# ----- Enum / conditional constant -----


def _t_enum_map(value: Any, *, args: dict, context: TransformContext) -> Any:
    """Translate one enum value to another.

    args:
      map     — dict of {source_value: target_value}
      default — value to use when source_value is not in `map`
                (omit to raise on miss)
    """
    mapping = args["map"]
    key = value
    if key in mapping:
        return mapping[key]
    if "default" in args:
        return args["default"]
    raise FieldMappingRuleError(
        f"enum_map: {value!r} not in map and no default provided",
        transform="enum_map",
        source_value=value,
    )


def _t_conditional_constant(
    value: Any, *, args: dict, context: TransformContext
) -> Any:
    """Output a constant chosen by the first matching condition.

    args:
      conditions — list of {when: <sandboxed expression>, then: <constant>}
      default    — value when no condition matches (omit to raise)

    Each `when` expression is evaluated with the condition allow-list
    (source_doc / source_payload). The expressions are validated at
    compile time; here we just call evaluate_expression.
    """
    conditions = args["conditions"]
    eval_globals = _eval_globals_for_condition(context)
    for cond in conditions:
        expr = cond["when"]
        if sandbox.evaluate_expression(expr, eval_globals=eval_globals):
            return cond["then"]
    if "default" in args:
        return args["default"]
    raise FieldMappingRuleError(
        "conditional_constant: no condition matched and no default provided",
        transform="conditional_constant",
        source_value=value,
    )


# ----- Computed-field reference / custom Python -----


def _t_computed(value: Any, *, args: dict, context: TransformContext) -> Any:
    """Return a computed field's resolved value.

    args:
      name — the computed-field name declared on the parent ruleset

    The compiler validates `name` exists in the ruleset's computed_fields
    table. The executor resolves all computed fields before applying rules
    (§5.9.2 step 3), so by the time this fires, `context.computed_values`
    holds the answer.
    """
    name = args["name"]
    if name not in context.computed_values:
        raise FieldMappingRuleError(
            f"computed: '{name}' was not resolved before this rule fired "
            "(compiler should have caught this)",
            transform="computed",
            source_value=value,
        )
    return context.computed_values[name]


def _t_custom_python(value: Any, *, args: dict, context: TransformContext) -> Any:
    """Sandboxed Python expression — the escape hatch (§5.5).

    args:
      expression — a Python expression. Allowed names: source_doc,
                   source_payload, get_path, sum_path, filter_path, value.

    Pre-validated at compile time by sandbox.validate_expression() with
    ALLOWED_NAMES_CUSTOM_PYTHON. Defence in depth: safe_eval at runtime.
    """
    expression = args["expression"]
    eval_globals = _eval_globals_for_custom_python(value, context)
    return sandbox.evaluate_expression(expression, eval_globals=eval_globals)


# ----- Internal: assemble eval_globals per role -----


def _eval_globals_for_condition(context: TransformContext) -> dict[str, Any]:
    return {
        "source_doc": context.source_doc,
        "source_payload": context.source_payload,
    }


def _eval_globals_for_computed(context: TransformContext) -> dict[str, Any]:
    g: dict[str, Any] = {
        "source_doc": context.source_doc,
        "source_payload": context.source_payload,
    }
    g.update(sandbox.build_path_helpers())
    return g


def _eval_globals_for_custom_python(
    value: Any, context: TransformContext
) -> dict[str, Any]:
    g = _eval_globals_for_computed(context)
    g["value"] = value
    return g


# ----- Args-contract validators (called at compile time) -----


def _validate_no_args(name: str, args: dict | None) -> None:
    _require_no_args(args, transformer=name)


def _validate_date_format(name: str, args: dict | None) -> None:
    if not args:
        raise FieldMappingCompileError(
            f"Transformer 'date_format' requires args {{from, to}}",
            parse_error="missing args",
        )
    _require_keys(args, ["from", "to"], transformer=name)
    for k in ("from", "to"):
        if not isinstance(args[k], str) or not args[k].strip():
            raise FieldMappingCompileError(
                f"date_format arg {k!r} must be a non-empty string",
                parse_error=f"bad {k}: {args[k]!r}",
            )


def _validate_lookup_id(name: str, args: dict | None) -> None:
    if not args:
        raise FieldMappingCompileError(
            "Transformer 'lookup_id' requires args {doctype, target_field}",
            parse_error="missing args",
        )
    _require_keys(args, ["doctype", "target_field"], transformer=name)


def _validate_reverse_lookup_id(name: str, args: dict | None) -> None:
    if not args:
        raise FieldMappingCompileError(
            "Transformer 'reverse_lookup_id' requires args {doctype, source_field}",
            parse_error="missing args",
        )
    _require_keys(args, ["doctype", "source_field"], transformer=name)


def _validate_enum_map(name: str, args: dict | None) -> None:
    if not args:
        raise FieldMappingCompileError(
            "Transformer 'enum_map' requires args {map, default?}",
            parse_error="missing args",
        )
    _require_keys(args, ["map"], transformer=name)
    if not isinstance(args["map"], dict):
        raise FieldMappingCompileError(
            "enum_map arg 'map' must be a dict",
            parse_error=f"bad map type: {type(args['map']).__name__}",
        )


def _validate_conditional_constant(name: str, args: dict | None) -> None:
    if not args:
        raise FieldMappingCompileError(
            "Transformer 'conditional_constant' requires args {conditions, default?}",
            parse_error="missing args",
        )
    _require_keys(args, ["conditions"], transformer=name)
    conditions = args["conditions"]
    if not isinstance(conditions, list) or not conditions:
        raise FieldMappingCompileError(
            "conditional_constant arg 'conditions' must be a non-empty list",
            parse_error="bad conditions",
        )
    for i, cond in enumerate(conditions):
        if not isinstance(cond, dict) or "when" not in cond or "then" not in cond:
            raise FieldMappingCompileError(
                f"conditional_constant conditions[{i}] must have 'when' and 'then' keys",
                parse_error=f"bad condition at index {i}",
            )
        # Validate the embedded `when` expression with condition allow-list.
        sandbox.validate_expression(
            cond["when"],
            sandbox.ALLOWED_NAMES_CONDITION,
            rule_label=f"conditional_constant.conditions[{i}].when",
        )


def _validate_computed(name: str, args: dict | None) -> None:
    if not args:
        raise FieldMappingCompileError(
            "Transformer 'computed' requires args {name}",
            parse_error="missing args",
        )
    _require_keys(args, ["name"], transformer=name)
    # The compiler additionally verifies the named computed field exists
    # on the parent ruleset (cross-table check). That lives in compiler.py.


def _validate_custom_python(name: str, args: dict | None) -> None:
    if not args:
        raise FieldMappingCompileError(
            "Transformer 'custom_python' requires args {expression}",
            parse_error="missing args",
        )
    _require_keys(args, ["expression"], transformer=name)
    sandbox.validate_expression(
        args["expression"],
        sandbox.ALLOWED_NAMES_CUSTOM_PYTHON,
        rule_label="custom_python.expression",
    )


# ----- Registry -----


# Each entry: (function, args_validator)
TRANSFORMERS: dict[str, tuple[Callable, Callable[[str, dict | None], None]]] = {
    "identity": (_t_identity, _validate_no_args),
    "bool_to_yn": (_t_bool_to_yn, _validate_no_args),
    "yn_to_bool": (_t_yn_to_bool, _validate_no_args),
    "str_lower": (_t_str_lower, _validate_no_args),
    "str_upper": (_t_str_upper, _validate_no_args),
    "str_strip": (_t_str_strip, _validate_no_args),
    "date_format": (_t_date_format, _validate_date_format),
    "datetime_to_iso": (_t_datetime_to_iso, _validate_no_args),
    "iso_to_datetime": (_t_iso_to_datetime, _validate_no_args),
    "int_to_str": (_t_int_to_str, _validate_no_args),
    "str_to_int": (_t_str_to_int, _validate_no_args),
    "float_to_str": (_t_float_to_str, _validate_no_args),
    "str_to_float": (_t_str_to_float, _validate_no_args),
    "currency_to_paise": (_t_currency_to_paise, _validate_no_args),
    "paise_to_currency": (_t_paise_to_currency, _validate_no_args),
    "lookup_id": (_t_lookup_id, _validate_lookup_id),
    "reverse_lookup_id": (_t_reverse_lookup_id, _validate_reverse_lookup_id),
    "enum_map": (_t_enum_map, _validate_enum_map),
    "conditional_constant": (_t_conditional_constant, _validate_conditional_constant),
    "computed": (_t_computed, _validate_computed),
    "custom_python": (_t_custom_python, _validate_custom_python),
    # `compose` is handled by the executor itself (§5.8), not as a value-level
    # transformer. It appears in transform_push/transform_pull as a reserved
    # name; the compiler routes it to the composition path rather than
    # looking it up here. Sentinel registration just so name-resolution
    # doesn't 'unknown transformer' on it.
    "compose": (None, None),  # type: ignore[dict-item]
}


# ----- Public API -----


def get_transformer(name: str) -> Callable:
    """Return the transformer callable for `name`. Raises if unknown.

    Compose is intentionally not callable here — the executor owns composition.
    """
    if name not in TRANSFORMERS:
        raise FieldMappingCompileError(
            f"Unknown transformer {name!r}; valid set: {sorted(TRANSFORMERS.keys())}",
            parse_error=f"unknown transformer: {name}",
        )
    fn, _ = TRANSFORMERS[name]
    if fn is None:
        raise FieldMappingCompileError(
            f"Transformer {name!r} is a reserved sentinel and not directly callable",
            parse_error=f"non-callable transformer: {name}",
        )
    return fn


def validate_transformer_args(name: str, args: dict | None, *, rule_label: str) -> None:
    """Compile-time args-contract check. Raises FieldMappingCompileError.

    Caller (the compiler) wraps the rule_label in to make the error point at
    the offending rule.
    """
    # An empty/null transform on one direction means "this rule is
    # one-way; skip it on this direction at run time" (handled in
    # the executor). The compiler accepts it without lookup.
    if not name:
        return
    if name == "compose":
        # Composition is validated by the compiler (max-depth, target ruleset
        # exists, direction matches) — not here.
        if not args or "ruleset" not in args:
            raise FieldMappingCompileError(
                f"'compose' in {rule_label} requires args {{ruleset}}",
                parse_error="missing 'ruleset' arg",
            )
        return

    if name not in TRANSFORMERS:
        raise FieldMappingCompileError(
            f"Unknown transformer {name!r} in {rule_label}; valid set: {sorted(TRANSFORMERS.keys())}",
            parse_error=f"unknown transformer: {name}",
        )
    _, validator = TRANSFORMERS[name]
    if validator is None:
        return
    try:
        validator(name, args)
    except FieldMappingCompileError as e:
        # Re-raise with the rule label for the FDE.
        raise FieldMappingCompileError(
            f"{e.args[0]} (in {rule_label})",
            rule_index=e.rule_index,
            parse_error=e.parse_error,
        ) from e


def apply_transformer(
    name: str,
    value: Any,
    *,
    args: dict | None,
    context: TransformContext,
) -> Any:
    """Run the named transformer on `value`. Raises FieldMappingRuleError on
    runtime failure (caught by the executor and surfaced per §5.9.3)."""
    fn = get_transformer(name)
    return fn(value, args=args or {}, context=context)


def all_transformer_names() -> list[str]:
    """Names suitable for a DocType Select field's options. Excludes the
    `compose` sentinel from non-debug listings? No — FDEs need it in the
    dropdown because it's a real choice on rules. Keep it in."""
    return sorted(TRANSFORMERS.keys())


__all__ = [
    "TransformContext",
    "TRANSFORMERS",
    "get_transformer",
    "validate_transformer_args",
    "apply_transformer",
    "all_transformer_names",
]
