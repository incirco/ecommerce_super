"""EasyEcom Account controller.

Account-level configuration (SPEC §3.3). One record per client deployment.
Holds credentials and account-wide operational config.

Validation rules:
  - api_endpoint must be a sensible HTTPS URL (we allow http://localhost for
    sandbox/test purposes only; otherwise must be https)
  - rate_limit_tier is mandatory with no preset default (§3.3.2). The DocType
    JSON already enforces reqd:1; the controller additionally ensures we
    don't silently fall back to "Default" if the field is somehow empty.
  - max_throughput_per_sec must not exceed the tier ceiling (§3.3.4). FDE
    may set it lower; the integration clamps to the tier regardless.
  - webhook_enabled implies webhook_token is set.
  - "tier still Default at go-live" is a blocking onboarding condition
    (§3.10) — we surface a warning at save time when environment_badge =
    Production and rate_limit_tier = Default.
"""

from __future__ import annotations

import re
from typing import ClassVar

import frappe
from frappe import _
from frappe.model.document import Document

# Tier → (req_per_sec, burst, daily_quota) per SPEC §3.10
RATE_LIMIT_TIERS: dict[str, tuple[int, int, int]] = {
    "Default": (5, 10, 500),
    "Bronze": (5, 10, 50_000),
    "Silver": (20, 40, 200_000),
    "Gold": (30, 60, 300_000),
    "Diamond": (30, 60, 500_000),
}

_HTTPS_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(:\d+)?(/.*)?$")
_HTTP_LOCAL_RE = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/.*)?$")


class EasyEcomAccount(Document):
    REDACTED_FIELDS: ClassVar[tuple[str, ...]] = (
        "x_api_key",
        "email",
        "password",
        "webhook_token",
    )

    def validate(self) -> None:
        self._validate_api_endpoint()
        self._validate_rate_limit_tier()
        self._clamp_throughput_to_tier()
        self._validate_webhook_config()
        self._warn_if_default_tier_in_production()
        self._update_webhook_endpoint_display()
        self._validate_single_enabled_account()

    def _validate_single_enabled_account(self) -> None:
        """§8.1 / Stage-6-audit #11 — at most one EasyEcom Account may
        be enabled at a time.

        Multi-Account deployments aren't supported by the integration:
        the §8d push code's _account_with_auto_push_enabled and
        _resolve_account already refuse ambiguity at the runtime layer,
        but a fresh deployment could silently land two enabled rows in
        the DB before any push fires. This validate is the DocType-
        level belt-and-suspenders: a save that would create a second
        enabled Account fails loudly, with a message that tells the
        FDE which other row is the conflict.

        Disabled accounts (enabled=0) are unconstrained — keeping
        historical / staging-environment rows around is fine."""
        if not self.enabled:
            return
        other = frappe.db.sql(
            """SELECT name FROM `tabEasyEcom Account`
               WHERE enabled = 1 AND name != %s
               LIMIT 1""",
            (self.name or "",),
            as_dict=True,
        )
        if other:
            frappe.throw(
                _(
                    "Another EasyEcom Account ({0}) is already enabled. "
                    "Disable it first, then enable this one. "
                    "Multi-Account deployments aren't supported by §8.1."
                ).format(other[0]["name"]),
                title=_("Single-Account Constraint"),
            )

    def _validate_api_endpoint(self) -> None:
        if not self.api_endpoint:
            return
        url = self.api_endpoint.strip()
        if not (_HTTPS_RE.match(url) or _HTTP_LOCAL_RE.match(url)):
            frappe.throw(
                _(
                    "API Endpoint must be an https:// URL (or http://localhost for sandbox)."
                ),
                title=_("Invalid API Endpoint"),
            )
        # Strip trailing slash so client code can append paths predictably.
        self.api_endpoint = url.rstrip("/")

    def _validate_rate_limit_tier(self) -> None:
        if not self.rate_limit_tier:
            frappe.throw(
                _(
                    "Rate Limit Tier is mandatory — set it to the tier EasyEcom has assigned to this api_key."
                ),
                title=_("Rate Limit Tier Required"),
            )
        if self.rate_limit_tier not in RATE_LIMIT_TIERS:
            frappe.throw(
                _("Rate Limit Tier {0} is not recognised.").format(self.rate_limit_tier)
            )

    def _clamp_throughput_to_tier(self) -> None:
        if not self.rate_limit_tier or self.rate_limit_tier not in RATE_LIMIT_TIERS:
            return
        tier_rate, _burst, _quota = RATE_LIMIT_TIERS[self.rate_limit_tier]
        if not self.max_throughput_per_sec:
            self.max_throughput_per_sec = tier_rate
            return
        if self.max_throughput_per_sec > tier_rate:
            frappe.msgprint(
                _(
                    "Max Throughput {0}/s exceeds the {1} tier ceiling of {2}/s. "
                    "Clamping to tier ceiling — the integration enforces this at runtime regardless."
                ).format(self.max_throughput_per_sec, self.rate_limit_tier, tier_rate),
                title=_("Throughput Clamped"),
                indicator="orange",
            )
            self.max_throughput_per_sec = tier_rate

    def _validate_webhook_config(self) -> None:
        if not self.webhook_enabled:
            return
        # webhook_token is a Password field; check via has_password to avoid
        # actually decrypting the value (the strict no-readback rule, §3.7.3).
        if not self._has_credential("webhook_token"):
            frappe.throw(
                _("Webhook Token is required when Webhooks Enabled is checked."),
                title=_("Webhook Token Required"),
            )

    def _warn_if_default_tier_in_production(self) -> None:
        if self.environment_badge == "Production" and self.rate_limit_tier == "Default":
            frappe.msgprint(
                _(
                    "The Default tier's 500-call daily quota is intended for onboarding/testing only. "
                    "Putting a Default-tier api_key into Production is a blocking onboarding condition (§3.10). "
                    "Ask EasyEcom to upgrade the tier before go-live."
                ),
                title=_("Default Tier in Production"),
                indicator="red",
            )

    def _update_webhook_endpoint_display(self) -> None:
        """Compute the webhook URL the FDE registers in EasyEcom."""
        site_url = frappe.utils.get_url()
        path = "/api/method/ecommerce_super.easyecom.api.webhook.receive"
        self.webhook_endpoint_url_display = f"{site_url}{path}"

    def _has_credential(self, fieldname: str) -> bool:
        """Return True if the credential field has a stored value.

        For freshly-entered values on this save, `self.<fieldname>` carries
        the raw string. For already-saved docs Frappe masks unchanged
        Password fields as "*****" in the save payload, so we fall through
        to a get_decrypted_password lookup. The lookup transiently
        materialises plaintext in memory; we never return or log it.
        """
        raw = self.get(fieldname)
        if raw and not str(raw).startswith("*"):
            return True
        if self.is_new():
            return False
        try:
            return bool(
                frappe.utils.password.get_decrypted_password(
                    self.doctype, self.name, fieldname, raise_exception=False
                )
            )
        except Exception:
            return False

    # ----- Utility methods used by EasyEcomClient (server-side only) -----

    def get_credentials_for_client(self) -> dict[str, str]:
        """Materialise credentials ONLY for the EasyEcomClient at the moment
        of building an outbound request. Caller must never log the return
        value or write it to any document field.

        SPEC §3.7.3: "The decrypted value is materialised only transiently
        inside the EasyEcomClient at the moment of building an outbound
        request, and is never written to a response, a log, a return value,
        or a document field."
        """
        return {
            "api_key": frappe.utils.password.get_decrypted_password(
                self.doctype, self.name, "x_api_key"
            ),
            "email": frappe.utils.password.get_decrypted_password(
                self.doctype, self.name, "email"
            ),
            "password": frappe.utils.password.get_decrypted_password(
                self.doctype, self.name, "password"
            ),
        }

    def get_webhook_token(self) -> str | None:
        """Return the decrypted webhook token for constant-time comparison
        inside the webhook receiver. Same caveats as get_credentials_for_client."""
        if not self.webhook_enabled:
            return None
        return frappe.utils.password.get_decrypted_password(
            self.doctype, self.name, "webhook_token", raise_exception=False
        )
