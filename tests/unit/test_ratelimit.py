"""
AEOS Unit Tests — tiered rate limiter (app/core/ratelimit.py)

Deterministic: every call passes an explicit `now` so no wall-clock sleeps.
"""
from app.core.ratelimit import RateLimiter


def test_token_bucket_allows_capacity_then_blocks():
    rl = RateLimiter(capacity=3)
    assert [rl.allow("ip") for _ in range(3)] == [True, True, True]
    assert rl.allow("ip") is False


def test_buckets_are_per_key():
    rl = RateLimiter(capacity=1)
    assert rl.allow("a") is True
    assert rl.allow("b") is True
    assert rl.allow("a") is False


def test_refill_over_time():
    rl = RateLimiter(capacity=1, refill_per_sec=1.0)
    assert rl.check("k", now=0.0).allowed is True
    assert rl.check("k", now=0.0).allowed is False   # empty
    assert rl.check("k", now=1.1).allowed is True     # refilled after ~1s


def test_check_reports_retry_after_when_blocked():
    rl = RateLimiter(capacity=1, refill_per_sec=1.0)
    rl.check("k", now=0.0)                 # consume
    d = rl.check("k", now=0.0)             # denied
    assert d.allowed is False
    assert d.retry_after > 0.0


def test_exponential_backoff_grows_across_denials():
    # capacity 1, refill negligible; each fresh denial after the prior block
    # expires should roughly double the block: 2s → 4s → 8s.
    rl = RateLimiter(capacity=1, refill_per_sec=0.0001, penalty_base=2.0, penalty_max=100.0)
    rl.check("k", now=0.0)                 # consume the single token
    d1 = rl.check("k", now=0.0)            # denial #1 → block ~2s
    d2 = rl.check("k", now=2.1)            # after block expires → denial #2 → ~4s
    d3 = rl.check("k", now=6.3)            # after that expires → denial #3 → ~8s
    assert d1.retry_after == 2.0
    assert d2.retry_after == 4.0
    assert d3.retry_after == 8.0


def test_backoff_capped_at_max():
    rl = RateLimiter(capacity=1, refill_per_sec=0.0001, penalty_base=10.0, penalty_max=15.0)
    rl.check("k", now=0.0)
    rl.check("k", now=0.0)                 # → 10s
    d = rl.check("k", now=10.1)            # would be 20s, capped to 15s
    assert d.retry_after == 15.0


def test_success_resets_penalty():
    rl = RateLimiter(capacity=1, refill_per_sec=1.0, penalty_base=2.0)
    rl.check("k", now=0.0)                 # consume
    rl.check("k", now=0.0)                 # denial → penalty engaged
    ok = rl.check("k", now=5.0)            # refilled → allowed, resets penalty
    assert ok.allowed is True
    # Next denial starts the ladder from base again, not where it left off.
    rl.check("k", now=5.0)                 # consume again
    d = rl.check("k", now=5.0)             # denial #1 again → base 2s
    assert d.retry_after == 2.0
