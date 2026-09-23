"""Exponential backoff with jitter for retry scheduling.

This module provides the shared backoff delay calculation used by retry
loops, implementing capped exponential backoff with random jitter to
prevent thundering-herd retries.
"""

import random


def backoff_delay(
    base: float,
    factor: float,
    attempt: int,
    *,
    cap: float = 30.0,
    jitter: float = 0.2,
) -> float:
    """Compute the wait time before the next retry attempt.

    Implements capped exponential backoff with multiplicative jitter:

        delay = min(base * factor ** attempt, cap) * uniform(1-j, 1+j)

    Args:
        base: Initial delay in seconds (from ResilienceConfig.retry_delay).
        factor: Multiplier applied per attempt (from ResilienceConfig.backoff_factor).
        attempt: Zero-based attempt index that just failed (0 = first retry wait).
        cap: Upper bound on the exponential component in seconds.
        jitter: Relative jitter fraction; the computed delay is scaled by a
            random factor in [1-jitter, 1+jitter] to desynchronize callers.

    Returns:
        float: Delay in seconds, always non-negative.

    Example:
        >>> 0 < backoff_delay(1.0, 2.0, 0) <= 1.2 * 1.0
        True
        >>> 0 < backoff_delay(1.0, 2.0, 3) <= 30 * 1.2
        True
    """
    delay = min(base * (factor**attempt), cap)
    # Non-crypto jitter is intentional: it only desynchronizes retry timing
    return delay * random.uniform(1 - jitter, 1 + jitter)  # noqa: S311
