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
    """Sampled monotonic delivery clock, excluding unobserved/offline intervals.

    Unknown long scheduling gaps are gifted rather than billed. At crash, only
    the last persisted checkpoint is charged. Normally up to five recent seconds
    are gifted; a storage outage may conservatively gift a longer interval.
    """
    def __init__(self, served=0, now=time.monotonic):
        self.now = now
        self.served = served
        self.account_ids = frozenset()
        self.last = now()
        self.delivering = False

    @property
    def served(self):
        return float(self._served)

    @served.setter
    def served(self, value):
        self._served = Decimal(str(value))

    def sample_accounts(self, account_ids, required):
        """Full-plan-equivalent seconds = observed account-seconds / plan count.

        Only identities present at BOTH sample boundaries contribute. New joins,
        replacements, unknown states and long observation gaps aren't backdated.
        No per-tick money rounding; fractional service accumulates as Decimal.
        """
        now = self.now()
        delta = max(0., now - self.last)
        current = frozenset(account_ids)
        if required > 0 and delta <= 5:
            count = min(required, len(self.account_ids & current))
            self._served += Decimal(str(delta)) * count / Decimal(required)
        self.last = now
        self.account_ids = current
        self.delivering = bool(current)
        return self.served

    def sample(self, delivering):
        now = self.now()
        delta = max(0., now - self.last)
        if self.delivering and delivering and delta <= 5:
            self._served += Decimal(str(delta))
        self.last = now
        self.delivering = delivering
        self.account_ids = frozenset()
        return self.served
