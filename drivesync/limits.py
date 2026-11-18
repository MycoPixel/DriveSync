"""Shared bandwidth limiter for Drive transfers (token bucket).

One TokenBucket per worker caps uploads + downloads combined at a fixed
KB/s. drive.py calls consume(n) per chunk; a 0/absent limiter means
'unlimited' and costs nothing.
"""

import threading
import time


class TokenBucket:
    """Classic token bucket: `rate` bytes/sec, burst up to `capacity`.

    consume(n) returns only once n bytes are affordable; between calls the
    bucket refills continuously. rate <= 0 (unlimited) makes consume a
    no-op so unthrottled runs pay no lock traffic.
    """

    def __init__(self, rate, burst=None, clock=time.monotonic, sleep=time.sleep):
        self.rate = float(rate)
        self.capacity = float(burst if burst is not None
                              else max(rate, 1 << 20))
        self.tokens = self.capacity
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    def consume(self, n):
        if self.rate <= 0 or n <= 0:
            return
        while True:
            with self._lock:
                now = self._clock()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            # cap each nap so a burst of concurrent consumers stays fair
            self._sleep(min(wait, 0.25))
