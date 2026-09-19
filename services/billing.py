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


class ServiceClock:
    """Sampled monotonic delivery clock, excluding build/unknown/offline intervals.

    Unknown long scheduling gaps are gifted rather than billed. At crash, only
    the last persisted checkpoint is charged. Normally up to five recent seconds
    are gifted; a storage outage may conservatively gift a longer interval.
    """
    def __init__(self, served=0, now=time.monotonic):
        self.now = now
        self.served = float(served)
        self.last = now()
        self.delivering = False

    def sample(self, delivering):
        now = self.now()
        delta = max(0., now - self.last)
        if self.delivering and delivering and delta <= 5:
            self.served += delta
        self.last = now
        self.delivering = delivering
        return self.served
