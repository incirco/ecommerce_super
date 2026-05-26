"""FieldMappingExecutor — translates between ERPNext docs and EasyEcom payloads (§5.9, §31.4.2).

Public surface (matches §31.4.2):

  FieldMappingExecutor(mapping_name, company=None)
    .push(source_doc)              -> EE payload dict
    .pull(source_payload)          -> ERPNext field dict
    .show_computed_mapping()       -> effective mapping incl. identity defaults
    .test_with_sample(sample, dir) -> {output, trace}

Application order (§5.9.2):
  1. Preconditions checked; if false → SyncPreconditionError
  2. Composed child rulesets resolved (recursive, max depth 5)
  3. Computed fields evaluated (dependency-order — naive: declaration order;
     compiler rejects forward refs in cross-table check)
  4. Each rule applied in declaration order; last write wins for same target
  5. Identity defaults applied in Permissive mode for unmapped source fields
  6. Required-rule check raises on missing
  7. validate_against checked against Frappe DocType

Error handling (§5.9.3):
  - Rule failure → FieldMappingRuleError carrying rule_id, paths, transform,
    source_value, reason. Caller (a flow) catches and writes a Sync Record /
    Integration Discrepancy.
  - Per-record isolation in batches lives in the caller's loop, not here.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.exceptions import (
    FieldMappingMissingRequiredError,
    FieldMappingRuleError,
    FieldMappingValidationError,
    SyncPreconditionError,
)
from ecommerce_super.easyecom.field_mapping import (
    compiler,
    path as path_mod,
    sandbox,
    transformers,
)
from ecommerce_super.easyecom.field_mapping.compiler import (
    CompiledRule,
    CompiledRuleset,
)

# Directions surfaced to the public API. "push" / "pull" lower-cased to
# match the Python convention; the DocType field uses "Push" / "Pull"
# but the executor is internally lowercase.
DIRECTION_PUSH = "push"
DIRECTION_PULL = "pull"


class FieldMappingExecutor:
    def __init__(self, mapping_name: str, company: str | None = None) -> None:
        self.mapping_name = mapping_name
        self.company = company
        self.compiled: CompiledRuleset = compiler.compile_ruleset(mapping_name)
        self._validate_scope()

    # ----- Public API -----

    def push(self, source_doc: Any) -> dict:
        """ERPNext document → EasyEcom payload dict."""
        if not self.compiled.active:
            return {}
        self._check_preconditions(source_doc=source_doc, source_payload=None)
        return self._apply(
            direction=DIRECTION_PUSH,
            source_doc=source_doc,
            source_payload=None,
            collect_trace=False,
        )["output"]

    def pull(self, source_payload: dict) -> dict:
        """EasyEcom payload dict → ERPNext field dict."""
        if not self.compiled.active:
            return {}
        self._check_preconditions(source_doc=None, source_payload=source_payload)
        return self._apply(
            direction=DIRECTION_PULL,
            source_doc=None,
            source_payload=source_payload,
            collect_trace=False,
        )["output"]

    def show_computed_mapping(self) -> dict:
        """Returns the effective mapping declaration including identity
        defaults that would be inferred per the missing_field_policy.

        For now this returns the compiled ruleset structure as a dict;
        Phase H's UI wraps it with FDE-friendly annotations.
        """
        return compiler.to_dict(self.compiled)

    def test_with_sample(self, sample: Any, direction: str) -> dict:
        """Apply the ruleset to a sample without persisting. Returns
        {output, trace, errors} — trace is a per-rule list of:
          {rule_idx, erpnext_path, easyecom_path, transform, source_value,
           result, skipped_reason}"""
        direction = direction.lower()
        if direction not in (DIRECTION_PUSH, DIRECTION_PULL):
            frappe.throw(f"Unknown direction {direction!r}; expected 'push' or 'pull'.")
        if direction == DIRECTION_PUSH:
            return self._apply(
                direction=DIRECTION_PUSH,
                source_doc=sample,
                source_payload=None,
                collect_trace=True,
            )
        return self._apply(
            direction=DIRECTION_PULL,
            source_doc=None,
            source_payload=sample,
            collect_trace=True,
        )

    # ----- Internals -----

    def _validate_scope(self) -> None:
        """If the ruleset declares a company_scope and the caller passed a
        company, ensure the company is allowed. Empty scope = all
        companies; no caller-company = no scope check (the executor is
        company-agnostic in that case)."""
        if not self.compiled.company_scope:
            return
        if not self.company:
            return
        if self.company not in self.compiled.company_scope:
            frappe.throw(
                f"Field Mapping {self.mapping_name!r} is not scoped to company "
                f"{self.company!r} (allowed: {self.compiled.company_scope})"
            )

    def _check_preconditions(
        self, *, source_doc: Any, source_payload: dict | None
    ) -> None:
        if not self.compiled.preconditions:
            return
        eval_globals = {"source_doc": source_doc, "source_payload": source_payload}
        result = sandbox.evaluate_expression(
            self.compiled.preconditions, eval_globals=eval_globals
        )
        if not result:
            raise SyncPreconditionError(
                f"Preconditions failed for Field Mapping {self.mapping_name!r}",
                precondition=self.compiled.preconditions,
            )

    def _apply(
        self,
        *,
        direction: str,
        source_doc: Any,
        source_payload: dict | None,
        collect_trace: bool,
    ) -> dict:
        output: dict = {}
        trace: list[dict] = [] if collect_trace else []
        errors: list[dict] = []

        # 1) Resolve computed fields (declaration order — compiler rejects
        # forward refs via the cross-table check). Stored on a context
        # passed to every transformer call.
        context = transformers.TransformContext(
            direction=direction,
            source_doc=source_doc,
            source_payload=source_payload,
            company=self.company,
            rule_label=self.mapping_name,
        )
        self._resolve_computed_fields(context, direction=direction)

        # 2) Apply rules in declaration order.
        for rule in self.compiled.rules:
            try:
                self._apply_rule(
                    rule,
                    direction=direction,
                    context=context,
                    output=output,
                    trace=trace if collect_trace else None,
                )
            except FieldMappingRuleError as e:
                if collect_trace:
                    errors.append(_error_to_dict(rule, e))
                    continue
                raise

        # 3) Required check (rules with required=1 must have produced a value).
        # get_first returns the scalar at the path (get_path returns a list
        # wrapper, which never matches None directly).
        for rule in self.compiled.rules:
            if not rule.required:
                continue
            target_path = _target_path(rule, direction)
            produced = path_mod.get_first(output, target_path)
            if produced in (None, "", []):
                raise FieldMappingMissingRequiredError(
                    f"Required rule {rule.idx} on {self.mapping_name!r} "
                    f"produced no value at {target_path!r}",
                    rule_id=str(rule.idx),
                    field_name=target_path,
                )

        # 4) validate_against on rules that declare it. get_first because
        # the path may resolve to a scalar (most rules) or a list (iteration).
        for rule in self.compiled.rules:
            if not rule.validate_against:
                continue
            target_path = _target_path(rule, direction)
            value = path_mod.get_first(output, target_path)
            if value in (None, ""):
                continue
            if isinstance(value, list):
                for v in value:
                    self._validate_against(rule, v)
            else:
                self._validate_against(rule, value)

        result = {"output": output}
        if collect_trace:
            result["trace"] = trace
            result["errors"] = errors
        return result

    def _resolve_computed_fields(
        self, context: transformers.TransformContext, *, direction: str
    ) -> None:
        """Evaluate only the computed fields referenced by a `computed`
        transformer used in the active direction. A computed field that
        is push-only (references source_doc) won't be evaluated during
        pull, even if it's declared on the same ruleset."""
        referenced = self._computed_referenced_in(direction)
        for cf in self.compiled.computed_fields.values():
            if cf.field_name not in referenced:
                continue
            eval_globals = {
                "source_doc": context.source_doc,
                "source_payload": context.source_payload,
            }
            eval_globals.update(sandbox.build_path_helpers())
            try:
                value = sandbox.evaluate_expression(
                    cf.expression, eval_globals=eval_globals
                )
            except (
                Exception
            ) as e:  # noqa: BLE001 — safe_eval surfaces as plain Exception
                raise FieldMappingRuleError(
                    f"Computed field {cf.field_name!r} evaluation failed: {e}",
                    rule_id=cf.field_name,
                    transform="computed",
                ) from e
            context.computed_values[cf.field_name] = value

    def _computed_referenced_in(self, direction: str) -> set[str]:
        names: set[str] = set()
        for rule in self.compiled.rules:
            if _transform_for(rule, direction) == "computed":
                ref = (rule.transform_args or {}).get("name")
                if ref:
                    names.add(ref)
        return names

    def _apply_rule(
        self,
        rule: CompiledRule,
        *,
        direction: str,
        context: transformers.TransformContext,
        output: dict,
        trace: list[dict] | None,
    ) -> None:
        # Condition (per-rule, applied to source). A condition written
        # for one direction (e.g. references source_doc) may error on the
        # other direction; treat any error as falsy → skip the rule. The
        # compile-time sandbox already rejected disallowed names, so a
        # runtime error here is a benign "wrong direction" not a security
        # issue.
        if rule.condition:
            try:
                cond_true = bool(
                    sandbox.evaluate_expression(
                        rule.condition,
                        eval_globals={
                            "source_doc": context.source_doc,
                            "source_payload": context.source_payload,
                        },
                    )
                )
            except Exception:  # noqa: BLE001 — see comment above
                cond_true = False
            if not cond_true:
                if trace is not None:
                    trace.append(_trace_skip(rule, "condition false"))
                return

        # Compose: handled separately (delegates to a child executor).
        transform_name = _transform_for(rule, direction)
        # A null/empty transform name on one direction means "skip this
        # rule for that direction" - the rule is one-way. Used when
        # a field has a meaningful pull mapping but no push contract
        # (e.g. cpId: EE returns it on GetProductMaster but rejects
        # it on UpdateMasterProduct - the field is read-only per EE's
        # write contract).
        if not transform_name:
            if trace is not None:
                trace.append(_trace_skip(rule, f"no transform for direction={direction}"))
            return
        if transform_name == compiler.COMPOSE_NAME:
            self._apply_compose(
                rule, direction=direction, context=context, output=output, trace=trace
            )
            return

        # Source path / iteration.
        source_path = _source_path(rule, direction)
        target_path = _target_path(rule, direction)
        source_root = (
            context.source_doc
            if direction == DIRECTION_PUSH
            else context.source_payload
        )

        if (
            rule.push_has_iteration
            if direction == DIRECTION_PUSH
            else rule.pull_has_iteration
        ):
            values = path_mod.get_path(source_root, source_path) or []
            if not isinstance(values, list):
                values = [values]
            transformed = []
            for v in values:
                t = self._run_transformer(
                    rule,
                    transform_name,
                    v,
                    context=context,
                )
                transformed.append(t)
            _set_iterated(output, target_path, transformed)
            if trace is not None:
                trace.append(
                    _trace_apply(rule, source_path, target_path, values, transformed)
                )
            return

        # Scalar path. get_path always returns a list; use get_first.
        value = path_mod.get_first(source_root, source_path)
        if value is None:
            value = self._apply_default_policy(rule)
            if value is None:
                # missing_field_policy == Drop → don't emit anything.
                if self.compiled.missing_field_policy == "Drop":
                    if trace is not None:
                        trace.append(_trace_skip(rule, "missing source, policy Drop"))
                    return
                if self.compiled.missing_field_policy == "Strict" and not rule.required:
                    raise FieldMappingMissingRequiredError(
                        f"Missing source value at {source_path!r} "
                        f"(Strict policy, rule {rule.idx})",
                        rule_id=str(rule.idx),
                        field_name=source_path,
                    )

        result = self._run_transformer(
            rule,
            transform_name,
            value,
            context=context,
        )
        path_mod.set_path(output, target_path, result)
        if trace is not None:
            trace.append(_trace_apply(rule, source_path, target_path, value, result))

    def _run_transformer(
        self,
        rule: CompiledRule,
        transform_name: str,
        value: Any,
        *,
        context: transformers.TransformContext,
    ) -> Any:
        try:
            return transformers.apply_transformer(
                transform_name,
                value,
                args=rule.transform_args,
                context=context,
            )
        except FieldMappingRuleError as e:
            # Re-raise with full rule context.
            raise FieldMappingRuleError(
                str(e),
                rule_id=str(rule.idx),
                erpnext_path=rule.erpnext_path,
                easyecom_path=rule.easyecom_path,
                transform=transform_name,
                source_value=value,
            ) from e

    def _apply_compose(
        self,
        rule: CompiledRule,
        *,
        direction: str,
        context: transformers.TransformContext,
        output: dict,
        trace: list[dict] | None,
    ) -> None:
        child_name = (rule.transform_args or {}).get("ruleset")
        if not child_name:
            raise FieldMappingRuleError(
                f"compose rule {rule.idx} has no transform_args.ruleset",
                rule_id=str(rule.idx),
                transform="compose",
            )
        child = FieldMappingExecutor(child_name, company=self.company)
        source_path = _source_path(rule, direction)
        target_path = _target_path(rule, direction)
        source_root = (
            context.source_doc
            if direction == DIRECTION_PUSH
            else context.source_payload
        )
        # get_first returns the raw list at source_path (get_path would
        # wrap it in another list because the value itself is iterable).
        rows = path_mod.get_first(source_root, source_path, default=[]) or []
        if not isinstance(rows, list):
            rows = [rows]
        outputs = []
        for row in rows:
            if direction == DIRECTION_PUSH:
                outputs.append(child.push(row))
            else:
                outputs.append(child.pull(row))
        # Compose target is the raw list at target_path — write directly,
        # not via _set_iterated (which expects per-row iteration markers).
        path_mod.set_path(output, target_path, outputs)
        if trace is not None:
            trace.append(_trace_apply(rule, source_path, target_path, rows, outputs))

    def _apply_default_policy(self, rule: CompiledRule) -> Any:
        if rule.default_value not in (None, ""):
            return rule.default_value
        return None

    def _validate_against(self, rule: CompiledRule, value: Any) -> None:
        doctype = rule.validate_against
        if not frappe.db.exists(doctype, value):
            raise FieldMappingValidationError(
                f"validate_against failed: {doctype} {value!r} does not exist "
                f"(rule {rule.idx})",
                rule_id=str(rule.idx),
                validate_against=doctype,
                invalid_value=value,
            )


# ----- Helpers -----


def _transform_for(rule: CompiledRule, direction: str) -> str:
    return rule.transform_push if direction == DIRECTION_PUSH else rule.transform_pull


def _source_path(rule: CompiledRule, direction: str) -> str:
    return rule.erpnext_path if direction == DIRECTION_PUSH else rule.easyecom_path


def _target_path(rule: CompiledRule, direction: str) -> str:
    return rule.easyecom_path if direction == DIRECTION_PUSH else rule.erpnext_path


def _set_iterated(output: dict, target_path: str, values: list) -> None:
    """Set a list at the iteration-marker target path. The target shape
    becomes `parent.array[N].field` per index. For a simple `items[].x`
    target, we project to {items: [{x: v}, ...]}; for a target that ends
    in `[]` directly, we project the raw list.
    """
    segments = _split_target(target_path)
    # If the target is purely iterative (e.g. `items[]` with no trailing field),
    # set as raw list.
    if segments and segments[-1] in ("[]", "[*]"):
        # Strip the trailing iteration marker; write the list at the parent path.
        parent_path = _join_segments(segments[:-1])
        if not parent_path:
            # Special case: target is exactly `[]` — replace output wholesale.
            output.clear()
            if isinstance(values, list):
                output["__list__"] = values
            return
        path_mod.set_path(output, parent_path, values)
        return

    # Otherwise the target path embeds an iteration marker mid-path —
    # e.g. `items[].sku`. We iterate values and project per index.
    for i, v in enumerate(values):
        idx_path = target_path.replace("[]", f"[{i}]", 1).replace("[*]", f"[{i}]", 1)
        path_mod.set_path(output, idx_path, v)


def _split_target(target_path: str) -> list[str]:
    # Re-use the foundation splitter via path_mod.
    from ecommerce_super.easyecom.utils.jsonpath import _split_segments

    return _split_segments(target_path)


def _join_segments(segments: list[str]) -> str:
    out = ""
    for seg in segments:
        if seg.startswith("["):
            out += seg
        else:
            out = f"{out}.{seg}" if out else seg
    return out


# ----- Trace helpers (test_with_sample) -----


def _trace_apply(
    rule: CompiledRule,
    source_path: str,
    target_path: str,
    source_value: Any,
    result: Any,
) -> dict:
    return {
        "rule_idx": rule.idx,
        "source_path": source_path,
        "target_path": target_path,
        "source_value": source_value,
        "result": result,
        "skipped": False,
    }


def _trace_skip(rule: CompiledRule, reason: str) -> dict:
    return {
        "rule_idx": rule.idx,
        "skipped": True,
        "skipped_reason": reason,
    }


def _error_to_dict(rule: CompiledRule, e: FieldMappingRuleError) -> dict:
    return {
        "rule_idx": rule.idx,
        "error": str(e),
        "rule_id": getattr(e, "rule_id", None),
        "erpnext_path": getattr(e, "erpnext_path", None),
        "easyecom_path": getattr(e, "easyecom_path", None),
        "transform": getattr(e, "transform", None),
        "source_value": getattr(e, "source_value", None),
    }


__all__ = ["FieldMappingExecutor", "DIRECTION_PUSH", "DIRECTION_PULL"]
