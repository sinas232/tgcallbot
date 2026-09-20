"""One monetary/time policy for checkout, preview, cancellation and receipts."""
from decimal import Decimal, ROUND_HALF_UP
import time

CENT = Decimal('0.01')  # تومان; rounded ONCE, never per timer tick


def money(value):
    value = Decimal(str(value or 0))
    if not value.is_finite():
        raise ValueError('Non-finite money')
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def prorate(total, elapsed, duration):
    total = money(total)
    if total < 0:
        raise ValueError('Negative price')
    elapsed = Decimal(str(elapsed))
    duration = Decimal(str(duration))
    if not elapsed.is_finite() or not duration.is_finite():
        raise ValueError('Non-finite duration')
    elapsed = min(max(elapsed, Decimal(0)), max(duration, Decimal(0)))
    used = money(total * elapsed / duration) if duration > 0 else Decimal(0)
    return float(used), float(total - used), float(elapsed)


class ActiveClock:
    """Wall-clock seconds an ORDER has been actively running.

    The customer buys a plan duration; how many accounts manage to join does
    not change the rate. Charging therefore follows the order's active window:
    it starts when execution starts and stops at the terminal state (cancel,
    completion, failure). A restart resumes from the persisted checkpoint plus
    the time observed since resume; the unpersisted tail of a crash is gifted
    to the customer, never invented.
    """
    def __init__(self, served=0, now=time.monotonic):
        self.now = now
        self._observed = Decimal(str(served))
        self._started_at = None

    @property
    def running(self):
        return self._started_at is not None

    def start(self):
        if self._started_at is None:
            self._started_at = self.now()
        return self.served

    def freeze(self):
        if self._started_at is not None:
            self._observed += Decimal(str(max(0., self.now() - self._started_at)))
            self._started_at = None
        return self.served

    @property
    def served(self):
        if self._started_at is None:
            return float(self._observed)
        return float(self._observed + Decimal(str(max(0., self.now() - self._started_at))))

    @served.setter
    def served(self, value):
        """Pin the observed total (checkpoint restore / tests)."""
        if self._started_at is not None:
            self._started_at = self.now()
        self._observed = Decimal(str(value))
