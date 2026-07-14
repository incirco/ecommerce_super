"""Unit tests for the sandboxed-expression layer (sandbox.py).

The security model is the cornerstone of §5: malicious or disallowed
expressions must be rejected at compile time. These tests prove the
sandbox's compile-time rejection works for the documented attack vectors.
"""

from __future__ import annotations

import unittest

from ecommerce_super.easyecom.exceptions import FieldMappingCompileError
from ecommerce_super.easyecom.field_mapping import sandbox


class TestAllowedNamesSets(unittest.TestCase):
    """The three role-specific allow-lists must match the spec exactly.

    Updated to include _SAFE_BUILTINS ({int, float, round}) — the sandbox
    was widened to expose numeric-coercion builtins that safe_eval already
    exposes at runtime. Without matching the compile-time allowlist to
    the runtime surface, valid expressions like `int(round(x))` fail
    to save.
    """

    _SAFE_BUILTINS = frozenset({"int", "float", "round"})

    def test_condition_role(self) -> None:
        self.assertEqual(
            sandbox.ALLOWED_NAMES_CONDITION,
            frozenset({"source_doc", "source_payload"}) | self._SAFE_BUILTINS,
        )

    def test_computed_role(self) -> None:
        self.assertEqual(
            sandbox.ALLOWED_NAMES_COMPUTED,
            frozenset(
                {
                    "source_doc",
                    "source_payload",
                    "get_path",
                    "sum_path",
                    "filter_path",
                }
            ) | self._SAFE_BUILTINS,
        )

    def test_custom_python_role(self) -> None:
        self.assertEqual(
            sandbox.ALLOWED_NAMES_CUSTOM_PYTHON,
            frozenset(
                {
                    "source_doc",
                    "source_payload",
                    "get_path",
                    "sum_path",
                    "filter_path",
                    "value",
                }
            ) | self._SAFE_BUILTINS,
        )


class TestSandboxAccepts(unittest.TestCase):
    def test_simple_condition(self) -> None:
        sandbox.validate_expression(
            "source_doc.customer_type == 'B2B'",
            sandbox.ALLOWED_NAMES_CONDITION,
            rule_label="r",
        )

    def test_computed_with_helpers(self) -> None:
        sandbox.validate_expression(
            "source_doc.total + sum_path(source_doc, 'items[].tax')",
            sandbox.ALLOWED_NAMES_COMPUTED,
            rule_label="r",
        )

    def test_custom_python_with_value(self) -> None:
        sandbox.validate_expression(
            "value * 2 + 1",
            sandbox.ALLOWED_NAMES_CUSTOM_PYTHON,
            rule_label="r",
        )


class TestSandboxRejectsMaliciousNames(unittest.TestCase):
    """The MUST-REJECT set per the §5 SECURITY block."""

    def test_rejects_frappe_access(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "frappe.get_doc('User', 'x')",
                sandbox.ALLOWED_NAMES_CONDITION,
                rule_label="r",
            )

    def test_rejects_os_module(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "os.system('rm -rf /')",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_sys_module(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "sys.modules",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_subprocess(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "subprocess.run(['echo', 'x'])",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_requests(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "requests.get('http://evil.com')",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_open(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "open('/etc/passwd').read()",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )


class TestSandboxRejectsDynamicExecution(unittest.TestCase):
    def test_rejects_eval_call(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "eval('1+1')",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_exec_call(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "exec('x=1')",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_compile_call(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "compile('1', '<s>', 'eval')",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_dunder_import(self) -> None:
        """The headline attack: __import__('os').system(...).
        The spec specifically calls this out as the MUST-REJECT case."""
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "__import__('os').system('echo HACK')",
                sandbox.ALLOWED_NAMES_CUSTOM_PYTHON,
                rule_label="r",
            )


class TestSandboxRejectsDunderTraversal(unittest.TestCase):
    """The classic Python sandbox escape: walk __class__ → __bases__ → __subclasses__."""

    def test_rejects_dunder_class(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "source_doc.__class__",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_dunder_mro(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "source_doc.__class__.__mro__",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_dunder_subclasses(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "source_doc.__subclasses__()",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )

    def test_rejects_dunder_globals(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "source_doc.__globals__",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )


class TestRoleScoping(unittest.TestCase):
    """Each role has a STRICT allow-list; helpers visible to computed
    must not be visible to plain conditions."""

    def test_condition_role_cannot_use_helpers(self) -> None:
        """get_path is only available to computed and custom_python — not
        conditions. A condition that tries to call it must fail."""
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "get_path(source_doc, 'x') == 1",
                sandbox.ALLOWED_NAMES_CONDITION,
                rule_label="r",
            )

    def test_computed_role_cannot_use_value(self) -> None:
        """value is only available to custom_python, not computed."""
        with self.assertRaises(FieldMappingCompileError):
            sandbox.validate_expression(
                "value + source_doc.x",
                sandbox.ALLOWED_NAMES_COMPUTED,
                rule_label="r",
            )
