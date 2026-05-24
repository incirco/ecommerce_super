"""EasyEcom Field Mapping controller — parent of the ruleset (§5.2, §5.9, §5.12).

Responsibilities:

  1. validate(): require change_reason; stamp audit fields; run the
     compiler so a malformed/malicious ruleset fails at save time
     (§5.9.1, §5 SECURITY block). A FieldMappingCompileError surfaces as
     `frappe.throw` so the FDE sees the offending rule.

  2. before_save(): auto-increment version when something material changed.

  3. on_update(): drop the compiled-ruleset cache; snapshot the ruleset
     to EasyEcom Field Mapping Version (append-only, §5.12).

Deviation from §5: Configuration Audit (§28) row is NOT written — the
audit DocType is part of §28 which is not yet built. The Field Mapping
Version snapshot itself is the §5 audit record; §28 will add the
cross-DocType audit when its section is built.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from ecommerce_super.easyecom.exceptions import FieldMappingCompileError


class EasyEcomFieldMapping(Document):
    def validate(self) -> None:
        self._require_change_reason()
        self._stamp_audit_fields()
        self._compile_or_throw()

    def before_save(self) -> None:
        self._increment_version()

    def on_update(self) -> None:
        self._invalidate_compiled_cache()
        self._write_version_snapshot()

    # ----- Validation helpers -----

    def _require_change_reason(self) -> None:
        if not (self.change_reason or "").strip():
            frappe.throw(_("Change Reason is required on every Field Mapping save."))

    def _stamp_audit_fields(self) -> None:
        self.last_modified_by = frappe.session.user
        self.last_modified_at = now_datetime()

    def _compile_or_throw(self) -> None:
        """Run the compiler against the in-memory doc state. We can't use
        compile_ruleset(name) because that reads from DB and our changes
        aren't committed yet. Instead, we drop the cache (if any) and
        compile from this doc — which the compiler reads via frappe.get_doc
        after our save lands. So we do a structural pre-check here using
        the same validators the compiler uses.

        The trick: most compile errors are independent of save order
        (path syntax, transformer args, sandboxed expressions). For these
        we can run the validators directly off `self.rules` /
        `self.computed_fields`. Cross-DocType checks (composition target
        exists) we do via DB lookup of the named target.
        """
        # Lazy import to avoid circular at module load.
        from ecommerce_super.easyecom.field_mapping import (
            compiler,
            path as path_mod,
            sandbox,
            transformers,
        )

        label_prefix = f"EasyEcom Field Mapping {self.mapping_name or '<new>'!r}"

        try:
            # 1) Preconditions sandbox check.
            if (self.preconditions or "").strip():
                sandbox.validate_expression(
                    self.preconditions,
                    sandbox.ALLOWED_NAMES_CONDITION,
                    rule_label=f"{label_prefix} preconditions",
                )

            # 2) Computed-fields: unique names + sandbox.
            seen: dict[str, int] = {}
            for cf in self.computed_fields or []:
                cf_label = (
                    f"{label_prefix} computed_fields[{cf.idx}] ({cf.field_name!r})"
                )
                if not (cf.field_name or "").strip():
                    raise FieldMappingCompileError(
                        f"Computed field at {cf_label} has empty name",
                        parse_error="empty name",
                    )
                if cf.field_name in seen:
                    raise FieldMappingCompileError(
                        f"Duplicate computed-field name {cf.field_name!r} in {label_prefix}",
                        parse_error=f"duplicate name: {cf.field_name}",
                    )
                seen[cf.field_name] = cf.idx
                sandbox.validate_expression(
                    cf.expression or "",
                    sandbox.ALLOWED_NAMES_COMPUTED,
                    rule_label=cf_label,
                )

            # 3) Rules: paths, transformer args, conditions, computed refs,
            #    compose target existence (max-depth / cycle checks happen
            #    in compiler.compile_ruleset which is called post-save).
            for rule in self.rules or []:
                rule_label = (
                    f"{label_prefix} rules[{rule.idx}] "
                    f"({rule.erpnext_path!r} -> {rule.easyecom_path!r})"
                )
                path_mod.validate_path(
                    rule.erpnext_path or "", rule_label=rule_label + " [erpnext_path]"
                )
                path_mod.validate_path(
                    rule.easyecom_path or "", rule_label=rule_label + " [easyecom_path]"
                )

                args = self._coerce_args(rule.transform_args, rule_label)
                transformers.validate_transformer_args(
                    rule.transform_push, args, rule_label=rule_label + " [push]"
                )
                transformers.validate_transformer_args(
                    rule.transform_pull, args, rule_label=rule_label + " [pull]"
                )

                # Computed cross-ref.
                for side in ("transform_push", "transform_pull"):
                    if getattr(rule, side) == "computed":
                        ref = (args or {}).get("name")
                        if ref not in seen:
                            raise FieldMappingCompileError(
                                f"{rule_label} [{side}] references computed field "
                                f"{ref!r} which is not declared on this mapping",
                                parse_error=f"unknown computed: {ref}",
                            )

                # Compose target exists.
                if (
                    rule.transform_push == compiler.COMPOSE_NAME
                    or rule.transform_pull == compiler.COMPOSE_NAME
                ):
                    target = (args or {}).get("ruleset")
                    if not target:
                        raise FieldMappingCompileError(
                            f"{rule_label} uses 'compose' but transform_args.ruleset is empty",
                            parse_error="missing compose ruleset",
                        )
                    # Self-composition is allowed at the field level (cycle
                    # detection runs in compile_ruleset post-save).
                    if target != self.mapping_name and not frappe.db.exists(
                        "EasyEcom Field Mapping", target
                    ):
                        raise FieldMappingCompileError(
                            f"{rule_label} compose target {target!r} does not exist",
                            parse_error=f"missing compose target: {target}",
                        )

                if (rule.condition or "").strip():
                    sandbox.validate_expression(
                        rule.condition,
                        sandbox.ALLOWED_NAMES_CONDITION,
                        rule_label=rule_label + " [condition]",
                    )

        except FieldMappingCompileError as e:
            # Surface to the FDE via frappe.throw — Frappe shows this in the
            # save dialog and aborts the write transaction.
            frappe.throw(
                _(str(e)),
                exc=frappe.ValidationError,
                title=_("Field Mapping Compile Error"),
            )

    @staticmethod
    def _coerce_args(raw, rule_label):
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
                    f"transform_args in {rule_label} must be a JSON object",
                    parse_error=f"non-object args: {type(parsed).__name__}",
                )
            return parsed
        raise FieldMappingCompileError(
            f"transform_args in {rule_label} has unsupported type {type(raw).__name__}",
            parse_error=f"unsupported type: {type(raw).__name__}",
        )

    # ----- Versioning -----

    def _increment_version(self) -> None:
        """Auto-increment on every save per §5.12 ('Every Field Mapping
        save creates a Field Mapping Version snapshot') and §31.2.8
        ('version: Int — auto-increment on save')."""
        if self.is_new():
            self.version = 1
            return
        prior = self.get_doc_before_save()
        base = (prior.version if prior else self.version) or 0
        self.version = base + 1

    # ----- Cache + snapshot -----

    def _invalidate_compiled_cache(self) -> None:
        from ecommerce_super.easyecom.field_mapping import compiler

        compiler.invalidate_compiled_cache(self.name)

    def _write_version_snapshot(self) -> None:
        """Append a Field Mapping Version row capturing the current state.
        Append-only (§5.12). We skip if a row for this version already
        exists — `on_update` may fire twice in some flows."""
        existing = frappe.db.exists(
            "EasyEcom Field Mapping Version",
            {"parent_mapping": self.name, "version": self.version},
        )
        if existing:
            return

        snapshot = self.as_dict(no_default_fields=False)
        # Strip Frappe-internal mutable bookkeeping that doesn't belong in
        # the persisted snapshot (idx, parentfield, ...). Keep field
        # values verbatim — including child rows.
        snapshot.pop("modified", None)
        snapshot.pop("modified_by", None)

        ver = frappe.new_doc("EasyEcom Field Mapping Version")
        ver.parent_mapping = self.name
        ver.version = self.version
        ver.created_by = frappe.session.user
        ver.created_at = now_datetime()
        ver.change_reason = self.change_reason
        ver.snapshot_json = json.dumps(snapshot, default=str, sort_keys=True)
        ver.insert(ignore_permissions=True)


# ----- Whitelisted rollback -----


@frappe.whitelist()
def rollback_to_version(parent_mapping: str, version: int) -> str:
    """Restore the Field Mapping to a prior version snapshot.

    The restore creates a new save (and thus a new version) — it does NOT
    overwrite history. change_reason is set automatically to
    "Rollback to v<n>".

    Returns the new version number.
    """
    version = int(version)
    snap_name = frappe.db.get_value(
        "EasyEcom Field Mapping Version",
        {"parent_mapping": parent_mapping, "version": version},
        "name",
    )
    if not snap_name:
        frappe.throw(
            _("No Version v{0} snapshot for {1}").format(version, parent_mapping)
        )

    snap = frappe.get_doc("EasyEcom Field Mapping Version", snap_name)
    data = json.loads(snap.snapshot_json)

    doc = frappe.get_doc("EasyEcom Field Mapping", parent_mapping)

    # Restore parent scalar fields (except identity/audit).
    skip = {
        "name",
        "mapping_name",
        "doctype",
        "owner",
        "creation",
        "modified",
        "modified_by",
        "version",
        "rules",
        "computed_fields",
        "company_scope",
    }
    for k, v in data.items():
        if k in skip:
            continue
        doc.set(k, v)

    # Restore child tables.
    for table_field in ("rules", "computed_fields", "company_scope"):
        doc.set(table_field, [])
        for row in data.get(table_field, []) or []:
            row = {
                k: v
                for k, v in row.items()
                if k
                not in (
                    "name",
                    "owner",
                    "creation",
                    "modified",
                    "modified_by",
                    "parent",
                    "parentfield",
                    "parenttype",
                    "docstatus",
                )
            }
            doc.append(table_field, row)

    doc.change_reason = f"Rollback to v{version}"
    doc.save(ignore_permissions=True)
    return str(doc.version)


# ----- Whitelisted FDE-surface methods (§5.10) -----


@frappe.whitelist()
def show_computed_mapping(mapping_name: str) -> dict:
    """Return the effective compiled mapping (§5.10.2 'Show Computed Mapping').

    Used by the JS button on the detail view to show the FDE the full
    resolved ruleset including identity-default behaviour.
    """
    from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor

    ex = FieldMappingExecutor(mapping_name)
    return ex.show_computed_mapping()


@frappe.whitelist()
def test_mapping(mapping_name: str, sample: str, direction: str = "push") -> dict:
    """Run the ruleset against a pasted sample without persisting
    (§5.10.2 'Test Mapping action').

    `sample` is a JSON string (the dialog passes raw text). Returns
    {output, trace, errors} for the JS dialog to render side-by-side.
    """
    from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor

    try:
        parsed = json.loads(sample) if isinstance(sample, str) else sample
    except json.JSONDecodeError as e:
        frappe.throw(_("Sample is not valid JSON: {0}").format(e))

    ex = FieldMappingExecutor(mapping_name)
    return ex.test_with_sample(parsed, direction)


@frappe.whitelist()
def diff_against_version(mapping_name: str, version: int) -> dict:
    """Return a rule-by-rule diff between the current ruleset and a
    prior version's snapshot (§5.10.2 'Diff Against Version').

    Output shape: {added: [...rules], removed: [...rules], modified: [...rules]}
    where each list contains the rule dicts (no Frappe metadata).
    """
    version = int(version)
    snap_name = frappe.db.get_value(
        "EasyEcom Field Mapping Version",
        {"parent_mapping": mapping_name, "version": version},
        "name",
    )
    if not snap_name:
        frappe.throw(
            _("No Version v{0} snapshot for {1}").format(version, mapping_name)
        )

    snap = frappe.get_doc("EasyEcom Field Mapping Version", snap_name)
    snap_data = json.loads(snap.snapshot_json)
    snap_rules = _normalise_rules(snap_data.get("rules") or [])

    current = frappe.get_doc("EasyEcom Field Mapping", mapping_name)
    current_rules = _normalise_rules([r.as_dict() for r in current.rules or []])

    snap_by_key = {_rule_key(r): r for r in snap_rules}
    current_by_key = {_rule_key(r): r for r in current_rules}

    added = [r for k, r in current_by_key.items() if k not in snap_by_key]
    removed = [r for k, r in snap_by_key.items() if k not in current_by_key]
    modified = [
        {"from": snap_by_key[k], "to": current_by_key[k]}
        for k in current_by_key
        if k in snap_by_key and snap_by_key[k] != current_by_key[k]
    ]
    return {"added": added, "removed": removed, "modified": modified}


@frappe.whitelist()
def bulk_set_active(names: str, active: int) -> int:
    """List-view bulk action (§5.10.1) — flip `active` on many rulesets.

    `names` is a JSON-encoded list of Field Mapping names (the JS sends
    them serialised). Returns the number of records updated.
    """
    parsed = json.loads(names) if isinstance(names, str) else names
    active_int = 1 if int(active) else 0
    count = 0
    for n in parsed:
        if not frappe.db.exists("EasyEcom Field Mapping", n):
            continue
        doc = frappe.get_doc("EasyEcom Field Mapping", n)
        doc.active = active_int
        doc.change_reason = f"Bulk {'activate' if active_int else 'deactivate'}"
        doc.save(ignore_permissions=True)
        count += 1
    return count


@frappe.whitelist()
def export_to_json(names: str) -> str:
    """Export selected rulesets to a JSON string the FDE can paste into
    another environment's Import action (§5.10.1)."""
    parsed = json.loads(names) if isinstance(names, str) else names
    out = []
    for n in parsed:
        if not frappe.db.exists("EasyEcom Field Mapping", n):
            continue
        doc = frappe.get_doc("EasyEcom Field Mapping", n)
        out.append(doc.as_dict(no_default_fields=False))
    return json.dumps(out, default=str, indent=2)


# ----- Internal helpers for diff -----


_RULE_DIFF_FIELDS = (
    "erpnext_path",
    "easyecom_path",
    "transform_push",
    "transform_pull",
    "transform_args",
    "condition",
    "default_value",
    "validate_against",
    "required",
    "notes",
)


def _normalise_rules(rules: list) -> list:
    out = []
    for r in rules:
        if not isinstance(r, dict):
            r = dict(r)
        clean = {k: r.get(k) for k in _RULE_DIFF_FIELDS}
        out.append(clean)
    return out


def _rule_key(rule: dict) -> str:
    """Stable identity for diff matching. Two rules with the same
    erpnext_path+easyecom_path are considered 'the same rule' across
    versions, modified if other fields differ."""
    return f"{rule.get('erpnext_path')}​{rule.get('easyecom_path')}"
