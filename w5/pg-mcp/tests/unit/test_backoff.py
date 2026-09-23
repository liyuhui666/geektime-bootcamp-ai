"""Unit tests for the exponential backoff helper (P2 / design doc §5.2)."""

import random

import pytest

from pg_mcp.resilience.backoff import backoff_delay


class TestBackoffDelay:
    """Tests for backoff_delay computation."""

    def test_first_attempt_near_base(self) -> None:
        """Attempt 0 waits approximately the base delay."""
        delay = backoff_delay(1.0, 2.0, 0, jitter=0.0)
        assert delay == pytest.approx(1.0)

    def test_exponential_growth(self) -> None:
        """Delays grow by the factor per attempt (no jitter)."""
        assert backoff_delay(1.0, 2.0, 1, jitter=0.0) == pytest.approx(2.0)
        assert backoff_delay(1.0, 2.0, 2, jitter=0.0) == pytest.approx(4.0)
        assert backoff_delay(1.0, 2.0, 3, jitter=0.0) == pytest.approx(8.0)

    def test_cap_bounds_growth(self) -> None:
        """Delay never exceeds the cap (before jitter)."""
        assert backoff_delay(1.0, 2.0, 20, cap=30.0, jitter=0.0) == pytest.approx(30.0)
        assert backoff_delay(1.0, 2.0, 20, cap=5.0, jitter=0.0) == pytest.approx(5.0)

    def test_jitter_stays_in_bounds(self) -> None:
        """Jitter keeps the delay within [1-j, 1+j] of the base component."""
        for _ in range(100):
            delay = backoff_delay(1.0, 2.0, 2, cap=30.0, jitter=0.2)
            assert 4.0 * 0.8 <= delay <= 4.0 * 1.2

    def test_jitter_randomizes_output(self) -> None:
        """Consecutive calls with jitter produce differing delays."""
        delays = {backoff_delay(1.0, 1.0, 5) for _ in range(50)}
        assert len(delays) > 1

    def test_zero_base(self) -> None:
        """Zero base yields zero delay (useful to disable backoff in tests)."""
        assert backoff_delay(0.0, 2.0, 3) == 0.0

    def test_seeded_random_is_deterministic(self) -> None:
        """With a seeded RNG the delay sequence is reproducible."""
        random.seed(42)
        first = [backoff_delay(1.0, 2.0, a) for a in range(4)]
        random.seed(42)
        second = [backoff_delay(1.0, 2.0, a) for a in range(4)]
        assert first == second
