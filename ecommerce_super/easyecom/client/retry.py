"""HTTP retry policy with exponential back-off + jitter.

SPEC §3.6:
  - 429 (rate limit) and 5xx → back off, max 6 retries, max delay 60s
  - Connection errors → same back-off
  - 401 → re-authenticate once, then retry the original call

The client (`client.py`) wraps each request in `with_retry(...)` to apply
this policy uniformly.
"""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

import requests

from ecommerce_super.easyecom.exceptions import (
    EasyEcomAuthError,
    EasyEcomRateLimitError,
    EasyEcomServerError,
    EasyEcomTimeoutError,
)

T = TypeVar("T")

# Back-off parameters per §3.6.
INITIAL_BACKOFF_S: float = 1.0
MAX_BACKOFF_S: float = 60.0
MAX_RETRIES: int = 6


def compute_backoff(attempt: int) -> float:
    """Exponential back-off with jitter for HTTP-level retries.

    attempt=1 → ~1s, attempt=2 → ~2s, attempt=3 → ~4s, ... capped at 60s.
    Jitter is uniform in [0, 1) seconds to spread retries from concurrent
    callers facing the same back-end blip.
    """
    base = min(INITIAL_BACKOFF_S * (2 ** (attempt - 1)), MAX_BACKOFF_S)
    return base + random.random()


def classify_response(status_code: int) -> str:
    """Return one of {'success', 'transient', 'auth', 'permanent', 'rate_limit'}.

    - 2xx → success
    - 401 → auth (caller re-auths once and retries)
    - 429 → rate_limit (transient with Retry-After honoured)
    - 5xx → transient (back-off + retry)
    - other 4xx → permanent (no retry; raise)
    """
    if 200 <= status_code < 300:
        return "success"
    if status_code == 401:
        return "auth"
    if status_code == 429:
        return "rate_limit"
    if 500 <= status_code < 600:
        return "transient"
    return "permanent"


def is_transient_exception(exc: BaseException) -> bool:
    """True for low-level exceptions that warrant retry (TCP, DNS, timeouts)."""
    if isinstance(exc, EasyEcomRateLimitError):
        return True
    if isinstance(exc, EasyEcomServerError):
        return True
    if isinstance(exc, EasyEcomTimeoutError):
        return True
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    return False


def with_retry(
    call: Callable[[], T], *, on_auth_failure: Callable[[], None] | None = None
) -> T:
    """Execute `call`, retrying transient failures per §3.6.

    `on_auth_failure` is invoked once when a 401 is observed — typically
    to clear/refresh the JWT cache before the retry. After re-auth the
    call is retried once at the same attempt count; subsequent 401s are
    treated as permanent EasyEcomAuthError.
    """
    auth_retried = False
    last_exc: BaseException | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return call()

        except EasyEcomAuthError as e:
            if not auth_retried and on_auth_failure is not None:
                auth_retried = True
                try:
                    on_auth_failure()
                except Exception:
                    # If the re-auth itself fails, surface the original 401.
                    raise e
                continue
            raise

        except (EasyEcomRateLimitError,) as e:
            last_exc = e
            if attempt >= MAX_RETRIES:
                raise
            # Honour explicit retry_after if EE provided one; otherwise back off.
            wait_s = max(e.retry_after or 0, compute_backoff(attempt))
            time.sleep(wait_s)

        except (EasyEcomServerError, EasyEcomTimeoutError) as e:
            last_exc = e
            if attempt >= MAX_RETRIES:
                raise
            time.sleep(compute_backoff(attempt))

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt >= MAX_RETRIES:
                raise EasyEcomTimeoutError(str(e)) from e
            time.sleep(compute_backoff(attempt))

    # Defensive — the loop above either returns or raises; we should never
    # fall through. If we do, surface the last seen exception.
    if last_exc is not None:
        raise last_exc
    raise EasyEcomTimeoutError("Exhausted retries with no recorded exception.")
