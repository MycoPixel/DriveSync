"""Bandwidth limiter + throttled transfer body tests."""

import io

from drivesync.limits import TokenBucket


def test_unlimited_bucket_is_free():
    b = TokenBucket(0)
    b.consume(10_000)                    # no sleep, no error, no state


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_consume_blocks_until_refill():
    clk = FakeClock()
    b = TokenBucket(rate=1000, burst=4000, clock=clk, sleep=clk.sleep)
    b.consume(4000)                      # burst: immediate
    b.consume(1000)                      # bucket empty: sleeps until refill
    assert clk.t > 0


def test_burst_never_exceeds_capacity():
    clk = FakeClock()
    b = TokenBucket(rate=10, burst=100, clock=clk, sleep=clk.sleep)
    clk.t += 1_000_000                   # hours of accumulated refill time
    b.consume(100)                       # still capped at one burst
    assert b.tokens <= b.capacity


def test_throttled_generator_bills_chunks():
    from drivesync.drive import _GeneratorBody

    billed = []

    class Lim:
        def consume(self, n):
            billed.append(n)

    fh = io.BytesIO(b"x" * 200_000)      # read out in 64 KiB chunks
    body = _GeneratorBody(b"H", fh, b"T", Lim())
    chunks = list(body)
    assert chunks[0] == b"H" and chunks[-1] == b"T"
    # 200000 bytes = 3 x 65536 + 3392; metadata head/tail are not billed
    assert billed == [65536, 65536, 65536, 3392]


def test_throttled_file_bills_reads():
    from drivesync.drive import _Throttled

    billed = []

    class Lim:
        def consume(self, n):
            billed.append(n)

    t = _Throttled(io.BytesIO(b"abcdef"), Lim())
    assert t.read(3) == b"abc"
    assert t.read(10) == b"def"
    assert t.read(5) == b""
    assert billed == [3, 3]
