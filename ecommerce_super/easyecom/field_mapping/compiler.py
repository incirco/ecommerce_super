"""Field Mapping compiler (SPEC §5.9.1, §31.4.2).

Reads an EasyEcom Field Mapping doc, validates every rule / computed
field / condition / path at *compile time*, and returns a CompiledRuleset
ready for the executor to consume.

Compile-time rejection is the cornerstone of the security model
(SPEC §5 SECURITY block): a malicious or malformed expression must fail
at save, not at runtime against live records. The compiler is the call
site that runs the sandbox's `validate_expression()` and the
transformers' args-contract validators.

Public API (mirrors §31.4.2):

  compile_ruleset(mapping_name)            -> CompiledRuleset
  invalidate_compiled_cache(mapping_name)  -> None

The cache is `frappe.cache()` (Frappe v16 Caffeine). Cached values are
picklable (no Callables stored); the executor resolves transformer
functions via the registry at run time, which is a cheap dict lookup.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import frappe

from ecommerce_super.easyecom.exceptions import FieldMappingCompileError
from ecommerce_super.easyecom.field_mapping import path as path_mod
from ecommerce_super.easyecom.field_mapping import sandbox, transformers

# Per §5.8: composition cannot recurse infinitely.
MAX_COMPOSITION_DEPTH = 5

# Reserved sentinel — composition is handled by the executor.
COMPOSE_NAME = "compose"

_CACHE_KEY_PREFIX = "ecs:fm:compiled"


# ----- Compiled value objects -----


@dataclass
class CompiledRule:
    """One Field Mapping Rule (§5.3), validated and frozen."""

    idx: int  # 1-based index in the parent's rules table (for FDE error msgs)
    erpnext_path: str
    easyecom_path: str
    transform_push: str
    transform_pull: str
    transform_args: dict
    condition: str | None
    default_value: Any
    validate_against: str | None
    required: bool
    notes: str | None
    push_has_iteration: bool
    pull_has_iteration: bool


@dataclass
class CompiledComputed:
    """One Computed Field (§5.6), validated and frozen."""

    field_name: str
    expression: str
    output_type: str
    cache_per_record: bool


@dataclass
class CompiledRuleset:
    """The full ruleset, in a form the executor can apply without
    re-validating anything. Picklable; no Callables stored."""

    mapping_name: str
    entity_type: str
    direction: str  # "Push" | "Pull" | "Bidirectional"
    active: bool
    missing_field_policy: str
    company_scope: list[str]  # empty = all
    preconditions: str | None
    version: int
    rules: list[CompiledRule] = field(default_factory=list)
    computed_fields: dict[str, CompiledComputed] = field(default_factory=dict)
    composed_ruleset_names: list[str] = field(default_factory=list)


# ----- Public API -----


def compile_ruleset(
    mapping_name: str, *, _seen: set[str] | None = None, _depth: int = 0
) -> CompiledRuleset:
    """Compile a Field Mapping by name, with caching.

    On cache hit, returns the cached CompiledRuleset. On miss, loads the
    doc, validates every component, and stores. Compilation failure raises
    FieldMappingCompileError (caller surfaces to the FDE).

    `_seen` and `_depth` are internal — they implement composition cycle
    detection and the max-depth bound (§5.8). Callers should never pass
    them.
    """
    if _depth > MAX_COMPOSITION_DEPTH:
        raise FieldMappingCompileError(
            f"Composition depth exceeded {MAX_COMPOSITION_DEPTH} starting at {mapping_name!r}",
            parse_error=f"max composition depth {MAX_COMPOSITION_DEPTH} exceeded",
        )

    _seen = _seen or set()
    if mapping_name in _seen:
        raise FieldMappingCompileError(
            f"Circular composition detected: {' -> '.join(sorted(_seen))} -> {mapping_name}",
            parse_error="circular composition",
        )

    cached = _cache_get(mapping_name)
    if cached is not None and _depth == 0:
        # Cache used only for top-level compile. Composition recursion
        # always re-validates (cheap, and ensures circular detection on
        # the live state every time).
        return cached

    if not frappe.db.exists("EasyEcom Field Mapping", mapping_name):
        raise FieldMappingCompileError(
            f"EasyEcom Field Mapping {mapping_name!r} does not exist",
            parse_error="not found",
        )

    doc = frappe.get_doc("EasyEcom Field Mapping", mapping_name)
    compiled = _compile_doc(doc, _seen=_seen | {mapping_name}, _depth=_depth)

    if _depth == 0:
        _cache_set(mapping_name, compiled)
    return compiled


def invalidate_compiled_cache(mapping_name: str) -> None:
    """Drop the cached compile of `mapping_name`. Called by the parent's
    `on_update` hook on every save (Phase G), and by the executor when it
    detects a version drift (Phase F)."""
    frappe.cache().delete_value(_cache_key(mapping_name))


def invalidate_all() -> None:
    """Drop ALL cached compiled rulesets. Useful in tests and on
    `bench clear-cache`. Walks the Field Mapping list to know the keys —
    Frappe's cache doesn't support prefix-delete portably."""
    names = frappe.get_all("EasyEcom Field Mapping", pluck="name")
    for n in names:
        invalidate_compiled_cache(n)


# ----- Internals -----


def _compile_doc(doc, *, _seen: set[str], _depth: int) -> CompiledRuleset:
    label_prefix = f"EasyEcom Field Mapping {doc.name!r}"

    # 1) Preconditions: validate the sandbox if non-empty.
    preconditions = (doc.preconditions or "").strip() or None
    if preconditions:
        sandbox.validate_expression(
            preconditions,
            sandbox.ALLOWED_NAMES_CONDITION,
            rule_label=f"{label_prefix} preconditions",
        )

    # 2) Computed fields: validate names unique + expressions.
    computed_fields: dict[str, CompiledComputed] = {}
    for cf in doc.computed_fields or []:
        cf_label = f"{label_prefix} computed_fields[{cf.idx}] ({cf.field_name!r})"
        if not (cf.field_name or "").strip():
            raise FieldMappingCompileError(
                f"Computed field at {cf_label} has empty name",
                parse_error="empty name",
            )
        if cf.field_name in computed_fields:
            raise FieldMappingCompileError(
                f"Duplicate computed-field name {cf.field_name!r} in {label_prefix}",
                parse_error=f"duplicate name: {cf.field_name}",
            )
        sandbox.validate_expression(
            cf.expression or "",
            sandbox.ALLOWED_NAMES_COMPUTED,
            rule_label=cf_label,
        )
        computed_fields[cf.field_name] = CompiledComputed(
            field_name=cf.field_name,
            expression=cf.expression.strip(),
            output_type=cf.output_type,
            cache_per_record=bool(cf.cache_per_record),
        )

    # 3) Rules: validate paths, transformers, conditions, validate cross-refs.
    compiled_rules: list[CompiledRule] = []
    composed_names: list[str] = []
    for rule in doc.rules or []:
        rule_label = (
            f"{label_prefix} rules[{rule.idx}] "
            f"({rule.erpnext_path!r} -> {rule.easyecom_path!r})"
        )

        # Path syntax (both sides).
        path_mod.validate_path(
            rule.erpnext_path or "", rule_label=rule_label + " [erpnext_path]"
        )
        path_mod.validate_path(
            rule.easyecom_path or "", rule_label=rule_label + " [easyecom_path]"
        )

        # transform_args may be a dict or a JSON string (the Frappe JSON
        # fieldtype surfaces as a Python object usually, but tests build
        # the row in either shape).
        args = _coerce_args(rule.transform_args, rule_label)

        # Transformer args contract (each side).
        transformers.validate_transformer_args(
            rule.transform_push, args, rule_label=rule_label + " [transform_push]"
        )
        transformers.validate_transformer_args(
            rule.transform_pull, args, rule_label=rule_label + " [transform_pull]"
        )

        # Computed-transformer cross-ref check.
        for side in ("transform_push", "transform_pull"):
            if getattr(rule, side) == "computed":
                ref = (args or {}).get("name")
                if ref not in computed_fields:
                    raise FieldMappingCompileError(
                        f"{rule_label} [{side}] references computed field {ref!r} "
                        f"which is not declared on this mapping",
                        parse_error=f"unknown computed: {ref}",
                    )

        # Composition: target ruleset exists, direction is compatible,
        # and recurse to detect cycles / over-depth.
        if rule.transform_push == COMPOSE_NAME or rule.transform_pull == COMPOSE_NAME:
            target = (args or {}).get("ruleset")
            if not target:
                raise FieldMappingCompileError(
                    f"{rule_label} uses 'compose' but transform_args.ruleset is empty",
                    parse_error="missing compose ruleset",
                )
            # Recursive compile — this checks existence, cycles, and depth.
            compile_ruleset(target, _seen=_seen, _depth=_depth + 1)
            composed_names.append(target)

        # Condition (per-rule).
        condition = (rule.condition or "").strip() or None
        if condition:
            sandbox.validate_expression(
                condition,
                sandbox.ALLOWED_NAMES_CONDITION,
                rule_label=rule_label + " [condition]",
            )

        compiled_rules.append(
            CompiledRule(
                idx=rule.idx,
                erpnext_path=rule.erpnext_path.strip(),
                easyecom_path=rule.easyecom_path.strip(),
                transform_push=rule.transform_push,
                transform_pull=rule.transform_pull,
                transform_args=args,
                condition=condition,
                default_value=rule.default_value,
                validate_against=(rule.validate_against or "").strip() or None,
                required=bool(rule.required),
                notes=rule.notes,
                push_has_iteration=path_mod.path_has_iteration(rule.erpnext_path),
                pull_has_iteration=path_mod.path_has_iteration(rule.easyecom_path),
            )
        )

    company_scope = [r.company for r in (doc.company_scope or []) if r.company]

    return CompiledRuleset(
        mapping_name=doc.name,
        entity_type=doc.entity_type,
        direction=doc.direction,
        active=bool(doc.active),
        missing_field_policy=doc.missing_field_policy or "Permissive",
        company_scope=company_scope,
        preconditions=preconditions,
        version=int(doc.version or 0),
        rules=compiled_rules,
        computed_fields=computed_fields,
        composed_ruleset_names=composed_names,
    )


def _coerce_args(raw: Any, rule_label: str) -> dict:
    """Normalise transform_args to a dict. Empty/None → {}; JSON string → dict.
    Raises FieldMappingCompileError on bad JSON or non-dict shape."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise FieldMappingCompileError(
                f"transform_args in {rule_label} is not valid JSON: {e}",
                parse_error=f"JSON decode: {e}",
            ) from e
        if not isinstance(parsed, dict):
            raise FieldMappingCompileError(
                f"transform_args in {rule_label} must be a JSON object, got {type(parsed).__name__}",
                parse_error=f"non-object args: {type(parsed).__name__}",
            )
        return parsed
    raise FieldMappingCompileError(
        f"transform_args in {rule_label} has unsupported type {type(raw).__name__}",
        parse_error=f"unsupported type: {type(raw).__name__}",
    )


# ----- Cache helpers -----


def _cache_key(mapping_name: str) -> str:
    return f"{_CACHE_KEY_PREFIX}:{mapping_name}"


def _cache_get(mapping_name: str) -> CompiledRuleset | None:
    raw = frappe.cache().get_value(_cache_key(mapping_name))
    if raw is None:
        return None
    if isinstance(raw, CompiledRuleset):
        return raw
    # Frappe caches sometimes return JSON-serialised data; rehydrate.
    if isinstance(raw, dict):
        return _from_dict(raw)
    return None


def _cache_set(mapping_name: str, compiled: CompiledRuleset) -> None:
    frappe.cache().set_value(_cache_key(mapping_name), compiled)


def _from_dict(d: dict) -> CompiledRuleset:
    rules = [CompiledRule(**r) for r in d.get("rules", [])]
    computed = {
        k: CompiledComputed(**v) for k, v in (d.get("computed_fields") or {}).items()
    }
    base = {k: v for k, v in d.items() if k not in ("rules", "computed_fields")}
    return CompiledRuleset(rules=rules, computed_fields=computed, **base)


def to_dict(compiled: CompiledRuleset) -> dict:
    """Public helper for snapshot/serialisation (used by Field Mapping
    Version, Test Mapping action, FDE tools)."""
    out = asdict(compiled)
    return out


__all__ = [
    "CompiledRule",
    "CompiledComputed",
    "CompiledRuleset",
    "MAX_COMPOSITION_DEPTH",
    "compile_ruleset",
    "invalidate_compiled_cache",
    "invalidate_all",
    "to_dict",
]
