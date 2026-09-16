"""Sources report completeness separately from their normalized records."""
from abc import ABC, abstractmethod
from .quotes import MarketData
from .types import SourceError


class AnnouncementSource(ABC):
    name = 'base'

    def __init__(self, market=None):
        self.market = market if market is not None else MarketData()

    @abstractmethod
    def fetch_days(self, start, end):
        """Yield DayResult for every requested calendar day, including zero days."""
        raise NotImplementedError

    def fetch_dates(self, days):
        for day in days:
            try:
                yield from self.fetch_days(day, day)
            except Exception as exc:
                from .types import DayResult
                yield DayResult(day, self.name, error=str(exc))

    def announcements(self, start, end):
        for result in self.fetch_days(start, end):
            yield from result.announcements
            if not result.complete:
                raise SourceError(result.error or f'{result.date} 公告未抓完整')

    def probe_day_counts(self, days):
        return {}  # Missing key means UNKNOWN, never zero.

    def max_items_per_day(self):
        return 0

    def klines(self, code, start='', end=''):
        return self.market.klines(code, start, end)

    def klines_batch(self, codes, start, end):
        yield from self.market.klines_batch(codes, start, end)

    def market_cap(self, code):
        return self.market.market_cap(code)

    def prefetch_market_caps(self, codes):
        return self.market.prefetch_market_caps(codes)

    def close(self):
        self.market.close()
