from app.platform.rate_limit import TokenBucketLimiter


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_capacity_then_reports_the_wait() -> None:
    clock = FakeMonotonic()
    limiter = TokenBucketLimiter(clock)

    results = [
        limiter.try_acquire("login", "1.2.3.4", capacity=3, per_seconds=60) for _ in range(4)
    ]

    assert results[:3] == [0.0, 0.0, 0.0]
    assert results[3] == 20.0  # 3 per 60 s -> one token every 20 s


def test_refills_over_time() -> None:
    clock = FakeMonotonic()
    limiter = TokenBucketLimiter(clock)
    for _ in range(3):
        limiter.try_acquire("login", "ip", capacity=3, per_seconds=60)

    clock.now += 20
    assert limiter.try_acquire("login", "ip", capacity=3, per_seconds=60) == 0.0
    assert limiter.try_acquire("login", "ip", capacity=3, per_seconds=60) > 0


def test_keys_and_scopes_are_independent() -> None:
    limiter = TokenBucketLimiter(FakeMonotonic())
    limiter.try_acquire("login", "a", capacity=1, per_seconds=60)

    assert limiter.try_acquire("login", "b", capacity=1, per_seconds=60) == 0.0
    assert limiter.try_acquire("register", "a", capacity=1, per_seconds=60) == 0.0
    assert limiter.try_acquire("login", "a", capacity=1, per_seconds=60) > 0


def test_bucket_count_is_bounded() -> None:
    clock = FakeMonotonic()
    limiter = TokenBucketLimiter(clock)
    limiter.MAX_BUCKETS = 50
    for n in range(200):
        clock.now += 1
        limiter.try_acquire("s", str(n), capacity=1, per_seconds=60)

    assert len(limiter._buckets) <= 50
