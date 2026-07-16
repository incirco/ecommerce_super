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
import frappe.utils.password  # noqa: F401  — referenced via qualified path below
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
        # gh#148 — pre-flight config checklist. Skipped for:
        #   - Disabled accounts (silent-inert path preserved).
        #   - Test runs (fixture Accounts don't have real Live Locations
        #     + GSTIN + Warehouse setup — the check would spuriously
        #     block every test fixture. FDEs on real sites still get
        #     the check; the whitelisted `config_check` endpoint runs
        #     regardless of test-mode.)
        # For enabled accounts on real sites, hard-blockers throw;
        # soft warnings post as a timeline Comment on the doc after save.
        if self.enabled and not getattr(frappe.flags, "in_test", False):
            self._run_pre_flight_config_checks()

    def after_insert(self) -> None:
        """Belt-and-suspenders Password-field encryption.

        Frappe v15/v16's auto-encrypt-on-insert pass is INCONSISTENT
        for Password-typed fields whose fieldname collides with
        reserved names (specifically `email` — auto-encryption skips
        it; `password` + `x_api_key` + `webhook_token` are encrypted
        normally). Form-side saves DO encrypt all four correctly, so
        the FDE's create-via-desk path works; the failure mode shows
        up on programmatic creates (scripts, fixtures, factories,
        bench execute) — the resulting Account has no `email` row in
        __Auth, and every subsequent EasyEcomClient call fails with
        'Password not found for EasyEcom Account ... email'.

        Live-found on the blank-site smoke 2026-05-27 (cold-start
        bring-up of smoke-test.local). The same gotcha would bite
        any future fixture-driven onboarding or any scripted
        multi-site provisioning, so the controller closes the gap
        unconditionally.

        Idempotent: only writes when the on-doc plaintext value is
        a non-empty string AND differs from what's already encrypted
        in __Auth. Existing accounts on FrappeCloud staging are
        unaffected.
        """
        from frappe.utils.password import (
            get_decrypted_password,
            set_encrypted_password,
        )

        for field in self.REDACTED_FIELDS:
            plaintext = self.get(field) or ""
            if not isinstance(plaintext, str) or not plaintext.strip():
                continue
            # Skip if the value on the doc is the redacted-asterisks
            # mask (Frappe's read-back of a Password field returns
            # the mask, not the plaintext).
            if plaintext.startswith("*") and all(c == "*" for c in plaintext):
                continue
            try:
                stored = get_decrypted_password(
                    "EasyEcom Account", self.name, field
                )
            except Exception:
                stored = None
            if stored == plaintext:
                continue
            set_encrypted_password(
                "EasyEcom Account", self.name, plaintext, field
            )

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

    def _run_pre_flight_config_checks(self) -> None:
        """gh#148 — checklist that runs when this Account is enabled.

        Hard blockers throw with a clear title/message pointing at the
        specific gap (FDE fixes and re-saves). Soft warnings surface as
        a timeline Comment on the Account so they're visible on the
        form but don't refuse the save.

        Called from `validate()` only when `self.enabled == 1`. Also
        callable read-only via the module-level `config_check()` API
        so the workspace shortcut can preview the checklist without
        needing to save.
        """
        blockers, warnings = _collect_pre_flight_findings(self)
        if blockers:
            # Assemble the throw with every blocker so the FDE sees the
            # full list in one save cycle, not one-fix-at-a-time.
            body = "\n".join(f"- {b['message']}" for b in blockers)
            frappe.throw(
                _(
                    "EasyEcom Account cannot be enabled until the "
                    "following are fixed:\n\n{0}"
                ).format(body),
                title=_("Pre-flight Config Check Failed"),
            )
        # Soft warnings — surface as a Comment on save (skipped on
        # is_new because the doc doesn't have a name to attach to yet;
        # will fire on the immediately-following save).
        if warnings and not self.is_new():
            body = "\n".join(f"- {w['message']}" for w in warnings)
            self.add_comment(
                "Comment",
                text=_(
                    "gh#148 pre-flight — soft warnings (integration still "
                    "enabled, but review these):\n\n{0}"
                ).format(body),
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


# ============================================================
# gh#148 — pre-flight config validation
# ============================================================
#
# Two entry points, one logic path:
#   1. validate() calls _run_pre_flight_config_checks() on save when
#      the account is enabled. Blockers throw; warnings post as
#      Comments.
#   2. config_check() is a @whitelist'd read-only endpoint that a
#      workspace shortcut / JS button can call to preview the same
#      checklist without saving. Returns a structured dict.
#
# Both funnel through _collect_pre_flight_findings(doc) which returns
# `(blockers, warnings)` where each entry is
# `{"category": str, "message": str}`.

_OPERATIONAL_WORKFLOW_STATE = "Live"


def _collect_pre_flight_findings(
    account: "EasyEcomAccount",
) -> tuple[list[dict], list[dict]]:
    """Return `(blockers, warnings)` for the given Account.

    Reads only — no writes, no throws. Safe to call from both save
    validation and the read-only preview endpoint.
    """
    blockers: list[dict] = []
    warnings: list[dict] = []

    live_locations = _fetch_live_locations()

    # --- Hard blockers ---

    if not live_locations:
        blockers.append({
            "category": "location",
            "message": (
                "No Live EasyEcom Location — integration will be inert. "
                "Set at least one Location to workflow_state=Live with "
                "a mapped Frappe Company and Warehouse before enabling."
            ),
        })

    for company in _unique_companies(live_locations):
        if not company:
            blockers.append({
                "category": "company",
                "message": (
                    "One or more Live Locations have no Frappe Company set. "
                    "Every Live Location must resolve to a Company before "
                    "the account can push."
                ),
            })
            continue
        gstin = _company_gstin(company)
        if not gstin:
            blockers.append({
                "category": "company",
                "message": (
                    f"Company {company!r} has no GSTIN. Set the Company's "
                    "GSTIN (via India Compliance's Company form) before "
                    "enabling — required for all GST invoicing."
                ),
            })

    b2b_module = getattr(account, "ecs_b2b_module", None) or ""
    if b2b_module and not account._has_credential("gsp_basic_auth_secret"):
        blockers.append({
            "category": "gsp",
            "message": (
                f"ecs_b2b_module is set to {b2b_module!r} but "
                "gsp_basic_auth_secret is empty. Set the secret before "
                "enabling — Custom GSP inbound calls will 401 without it."
            ),
        })

    if getattr(account, "gsp_mint_einvoice", 0):
        if not _india_compliance_installed():
            blockers.append({
                "category": "ic",
                "message": (
                    "gsp_mint_einvoice=1 requires the India Compliance app "
                    "to be installed on this site. Install it via "
                    "`bench get-app india_compliance` + `bench install-app "
                    "india_compliance` before enabling."
                ),
            })

    # --- Soft warnings ---

    for loc in live_locations:
        wh = loc.get("mapped_warehouse")
        if not wh:
            warnings.append({
                "category": "warehouse",
                "message": (
                    f"Live Location {loc['name']!r} has no mapped_warehouse. "
                    "Set the Warehouse link before pushing orders — Gate 0 "
                    "will silently skip SOs whose set_warehouse doesn't map."
                ),
            })
            continue
        if not _warehouse_has_state(wh):
            warnings.append({
                "category": "warehouse",
                "message": (
                    f"Warehouse {wh!r} (Location {loc['name']!r}) has no "
                    "state — required for GST place-of-supply computation."
                ),
            })
        if not _warehouse_has_ecs_ee_location_fk(wh):
            warnings.append({
                "category": "warehouse",
                "message": (
                    f"Warehouse {wh!r} (Location {loc['name']!r}) has no "
                    "ecs_ee_location FK — half-mapped state. See #141 for "
                    "the SO-side detector that catches this at push time."
                ),
            })

    unmapped_customer_count = _count_customers_without_ee_c_id()
    if unmapped_customer_count > 0:
        warnings.append({
            "category": "customer",
            "message": (
                f"{unmapped_customer_count} EasyEcom Customer Map row(s) "
                "have no ecs_ee_c_id populated. B2B push will refuse those "
                "customers with a resolution error — backfill via §8e or "
                "manual set before pushing."
            ),
        })

    if _item_map_count() == 0:
        warnings.append({
            "category": "item",
            "message": (
                "Zero EasyEcom Item Map rows — no items are synced to EE. "
                "The integration will be inert until items are pushed "
                "(via §8d Item Push) or pulled (via §8d Item Pull)."
            ),
        })

    return blockers, warnings


# --- Data-access helpers (broken out so tests can mock cleanly) ---


def _fetch_live_locations() -> list[dict]:
    """Return list of Live EasyEcom Locations with the fields the
    pre-flight checks need."""
    return frappe.get_all(
        "EasyEcom Location",
        filters={"workflow_state": _OPERATIONAL_WORKFLOW_STATE},
        fields=["name", "frappe_company", "mapped_warehouse"],
        limit=200,  # bounded — sites with 200+ Live locations are extreme
    )


def _unique_companies(live_locations: list[dict]) -> list[str]:
    """Return the unique set of Frappe Companies across Live Locations,
    preserving order of first appearance. Empty companies included so
    caller can flag "location without company assignment"."""
    seen: list[str] = []
    for loc in live_locations:
        c = loc.get("frappe_company")
        if c not in seen:
            seen.append(c)
    return seen


def _company_gstin(company: str) -> str | None:
    """Return the Company's GSTIN via India Compliance's field.

    India Compliance stores GSTIN at the Company Address level (not on
    Company directly). Preferred lookup: the primary billing Address
    for this Company where gstin is populated. If IC isn't installed
    or no address matches, return None (caller flags as blocker).
    """
    if not company:
        return None
    # Try the standard IC-style lookup first: Address linked to this
    # Company via Dynamic Link with gstin populated.
    result = frappe.db.sql(
        """SELECT a.gstin
           FROM `tabAddress` a
           JOIN `tabDynamic Link` dl ON dl.parent = a.name
               AND dl.parenttype = 'Address'
           WHERE dl.link_doctype = 'Company'
             AND dl.link_name = %s
             AND a.gstin IS NOT NULL AND a.gstin != ''
           LIMIT 1""",
        (company,),
        as_dict=True,
    )
    if result:
        return (result[0].get("gstin") or "").strip() or None
    return None


def _warehouse_has_state(warehouse: str) -> bool:
    """Warehouse has a linked Address with state populated."""
    if not warehouse:
        return False
    result = frappe.db.sql(
        """SELECT a.state
           FROM `tabAddress` a
           JOIN `tabDynamic Link` dl ON dl.parent = a.name
               AND dl.parenttype = 'Address'
           WHERE dl.link_doctype = 'Warehouse'
             AND dl.link_name = %s
             AND a.state IS NOT NULL AND a.state != ''
           LIMIT 1""",
        (warehouse,),
        as_dict=True,
    )
    return bool(result)


def _warehouse_has_ecs_ee_location_fk(warehouse: str) -> bool:
    """The `Warehouse.ecs_ee_location` Custom Field must be populated
    for §11 Gate 0 to fire. Half-mapping (Warehouse exists but FK
    empty) is the exact silent-inert failure mode gh#162 fixed on the
    SO side."""
    if not warehouse:
        return False
    if not frappe.db.has_column("Warehouse", "ecs_ee_location"):
        return False  # column absent → treat as unmapped
    return bool(frappe.db.get_value("Warehouse", warehouse, "ecs_ee_location"))


def _count_customers_without_ee_c_id() -> int:
    """Count of EasyEcom Customer Map rows where ecs_ee_c_id is empty.
    Bounded via count query — never materialises the list."""
    if not frappe.db.has_column("EasyEcom Customer Map", "ee_c_id"):
        return 0
    return frappe.db.count(
        "EasyEcom Customer Map",
        filters=[["ee_c_id", "in", ["", None]]],
    )


def _item_map_count() -> int:
    return frappe.db.count("EasyEcom Item Map")


def _india_compliance_installed() -> bool:
    """India Compliance app must be installed on the site for e-invoice
    minting to work. Check via frappe.get_installed_apps."""
    try:
        return "india_compliance" in frappe.get_installed_apps()
    except Exception:
        return False


@frappe.whitelist()
def config_check(account: str) -> dict:
    """Read-only preview of the pre-flight checklist.

    Callable from a workspace shortcut / JS button on the Account
    form so the FDE can preview blockers + warnings without saving.
    Returns:
        {
            "account": <name>,
            "enabled": <int>,
            "blockers": [{"category", "message"}, ...],
            "warnings": [{"category", "message"}, ...],
        }
    """
    if not frappe.has_permission("EasyEcom Account", "read", doc=account):
        frappe.throw(_("Not permitted to read EasyEcom Account {0}").format(account))
    doc = frappe.get_doc("EasyEcom Account", account)
    blockers, warnings = _collect_pre_flight_findings(doc)
    return {
        "account": account,
        "enabled": int(doc.enabled or 0),
        "blockers": blockers,
        "warnings": warnings,
    }
