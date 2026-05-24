"""Integration tests for §6.3.7 per-Company concurrency + the
crash-drift fix on the reclaim path (§6.3.9)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.exceptions import CompanyConcurrencyExceeded
from ecommerce_super.easyecom.queue import concurrency


class TestSemaphoreLifecycle(FrappeTestCase):
    COMPANY = "_Test Company Drift"

    def setUp(self) -> None:
        concurrency.reset(self.COMPANY)

    def tearDown(self) -> None:
        concurrency.reset(self.COMPANY)

    def test_acquire_release_balanced(self) -> None:
        self.assertEqual(concurrency.current_count(self.COMPANY), 0)
        with concurrency.company_concurrency_semaphore(self.COMPANY):
            self.assertEqual(concurrency.current_count(self.COMPANY), 1)
        self.assertEqual(concurrency.current_count(self.COMPANY), 0)

    def test_exhaustion_raises_and_decrements(self) -> None:
        """When the cap is exceeded, the increment is rolled back so the
        rejected acquire doesn't strand a slot."""
        # The default cap is 4 when no account is set up. Acquire 4, then
        # try a 5th in another with-block — should raise without holding.
        slots = []
        for _ in range(concurrency.DEFAULT_CAP):
            cm = concurrency.company_concurrency_semaphore(self.COMPANY)
            cm.__enter__()
            slots.append(cm)
        self.assertEqual(
            concurrency.current_count(self.COMPANY), concurrency.DEFAULT_CAP
        )

        with self.assertRaises(CompanyConcurrencyExceeded):
            with concurrency.company_concurrency_semaphore(self.COMPANY):
                self.fail("should have raised before yielding")
        # 5th try rolled back: count is still at cap, not cap+1.
        self.assertEqual(
            concurrency.current_count(self.COMPANY), concurrency.DEFAULT_CAP
        )

        for cm in slots:
            cm.__exit__(None, None, None)
        self.assertEqual(concurrency.current_count(self.COMPANY), 0)


class TestCurrentCountDoesNotRaise(FrappeTestCase):
    """The Py2-syntax bug at concurrency.py:85 caused current_count to
    raise on the first wrong-type cache value. The fix uses the
    correct `except (TypeError, ValueError)` tuple syntax."""

    COMPANY = "_Test Company Counter"

    def setUp(self) -> None:
        concurrency.reset(self.COMPANY)

    def tearDown(self) -> None:
        concurrency.reset(self.COMPANY)

    def test_count_on_unset_key_returns_zero(self) -> None:
        self.assertEqual(concurrency.current_count(self.COMPANY), 0)

    def test_count_handles_weird_cache_value(self) -> None:
        """Inject a non-int into the cache; current_count must return 0
        rather than raise. Pre-fix this would have hit the broken
        `except TypeError, ValueError:` clause."""
        frappe.cache().set_value(f"easyecom:concurrency:{self.COMPANY}", "not-an-int")
        # Must not raise.
        result = concurrency.current_count(self.COMPANY)
        self.assertEqual(result, 0)


class TestReclaimResetsSemaphore(FrappeTestCase):
    """§6.3.7 crash-drift fix: when a worker dies mid-job, its `finally`
    decrement never runs. The reclaim path must release one slot per
    reclaimed job so the counter doesn't permanently drift up."""

    COMPANY = "_Test Company Reclaim"

    def setUp(self) -> None:
        concurrency.reset(self.COMPANY)

    def tearDown(self) -> None:
        concurrency.reset(self.COMPANY)

    def test_release_slot_decrements_by_one(self) -> None:
        """Hold 3 slots; one worker 'crashes' (reclaim calls release_slot
        on its behalf); the two surviving workers release normally on
        exit. Final count is 0 — the drift is fully reconciled."""
        slots = []
        for _ in range(3):
            cm = concurrency.company_concurrency_semaphore(self.COMPANY)
            cm.__enter__()
            slots.append(cm)
        self.assertEqual(concurrency.current_count(self.COMPANY), 3)

        # Simulate the reclaim path releasing one crashed-worker's slot.
        # The crashed worker's `finally` never ran, so its with-block
        # exit is effectively skipped — drop one cm from `slots`.
        crashed = slots.pop()  # noqa: F841  (we never call __exit__ on it)
        new_count = concurrency.release_slot(self.COMPANY)
        self.assertEqual(new_count, 2)
        self.assertEqual(concurrency.current_count(self.COMPANY), 2)

        # The two surviving with-blocks now release normally.
        for cm in slots:
            cm.__exit__(None, None, None)
        # Drift fully reconciled: 3 acquires - 1 reclaim - 2 normal = 0.
        self.assertEqual(concurrency.current_count(self.COMPANY), 0)

    def test_release_slot_floors_at_zero(self) -> None:
        """release_slot on an empty counter must not go negative."""
        self.assertEqual(concurrency.current_count(self.COMPANY), 0)
        result = concurrency.release_slot(self.COMPANY)
        self.assertEqual(result, 0)
        self.assertEqual(concurrency.current_count(self.COMPANY), 0)
