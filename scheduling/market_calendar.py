"""
NSE market calendar: trading hours, holidays, market open/close checks.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import holidays

IST = ZoneInfo("Asia/Kolkata")

# NSE market hours
MARKET_OPEN = time(9, 15)      # 9:15am IST
MARKET_CLOSE = time(15, 30)    # 3:30pm IST
PRE_OPEN = time(9, 0)          # Pre-open session
INTRADAY_CUTOFF = time(15, 15) # Stop new intraday entries
MIS_CLOSE = time(15, 15)       # Start closing MIS positions

# NSE holidays (India national holidays via `holidays` library)
# Note: NSE-specific trading holidays may differ — maintain a local list for accuracy
NSE_EXTRA_HOLIDAYS_2024 = {
    date(2024, 1, 26),   # Republic Day
    date(2024, 3, 25),   # Holi
    date(2024, 3, 29),   # Good Friday
    date(2024, 4, 14),   # Dr. Ambedkar Jayanti
    date(2024, 4, 17),   # Ram Navami
    date(2024, 4, 21),   # Mahavir Jayanti
    date(2024, 5, 23),   # Buddha Purnima
    date(2024, 6, 17),   # Bakri Id
    date(2024, 7, 17),   # Muharram
    date(2024, 8, 15),   # Independence Day
    date(2024, 10, 2),   # Gandhi Jayanti
    date(2024, 10, 14),  # Dussehra
    date(2024, 11, 1),   # Diwali Laxmi Puja
    date(2024, 11, 15),  # Gurunanak Jayanti
    date(2024, 12, 25),  # Christmas
}

NSE_EXTRA_HOLIDAYS_2025 = {
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 4, 10),   # Ugadi
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 12),   # Buddha Purnima
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Gandhi Jayanti
    date(2025, 10, 2),   # Dussehra
    date(2025, 10, 20),  # Diwali Laxmi Puja
    date(2025, 10, 21),  # Diwali Balipratipada
    date(2025, 11, 5),   # Gurunanak Jayanti
    date(2025, 12, 25),  # Christmas
}

NSE_EXTRA_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 11),   # Maha Shivaratri
    date(2026, 3, 29),   # Holi
    date(2026, 3, 30),   # Holi (2nd day)
    date(2026, 4, 2),    # Good Friday
    date(2026, 4, 10),   # Eid ul-Fitr
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 4, 17),   # Ram Navami
    date(2026, 4, 21),   # Mahavir Jayanti
    date(2026, 5, 15),   # Buddha Purnima
    date(2026, 7, 7),    # Bakri Id
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 31),   # Janmashtami
    date(2026, 9, 16),   # Milad un-Nabi
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 9),   # Dussehra
    date(2026, 10, 29),  # Diwali (Diwali day)
    date(2026, 10, 30),  # Diwali (Laxmi Puja + Govardhan Puja)
    date(2026, 11, 11),  # Gurunanak Jayanti
    date(2026, 12, 25),  # Christmas
}

ALL_NSE_HOLIDAYS = NSE_EXTRA_HOLIDAYS_2024 | NSE_EXTRA_HOLIDAYS_2025 | NSE_EXTRA_HOLIDAYS_2026


def is_trading_day(d: date | None = None) -> bool:
    """Return True if the given date is an NSE trading day."""
    d = d or date.today()
    # Weekend
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    # NSE holiday
    if d in ALL_NSE_HOLIDAYS:
        return False
    return True


def is_market_open(dt: datetime | None = None) -> bool:
    """Return True if the market is currently open."""
    dt = dt or datetime.now(IST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    if not is_trading_day(dt.date()):
        return False
    t = dt.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def is_intraday_entry_allowed(dt: datetime | None = None) -> bool:
    """Return True if new intraday entries are allowed right now."""
    dt = dt or datetime.now(IST)
    if not is_market_open(dt):
        return False
    return dt.time() < INTRADAY_CUTOFF


def should_close_mis(dt: datetime | None = None) -> bool:
    """Return True if it's time to start closing MIS positions."""
    dt = dt or datetime.now(IST)
    if not is_trading_day(dt.date()):
        return False
    return dt.time() >= MIS_CLOSE


def seconds_until_market_open(dt: datetime | None = None) -> float:
    """Return seconds until market opens (next trading day 9:15am)."""
    from datetime import timedelta
    dt = dt or datetime.now(IST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)

    # Find next trading day open
    candidate = dt.date()
    while True:
        if is_trading_day(candidate):
            open_dt = datetime.combine(candidate, MARKET_OPEN, tzinfo=IST)
            if open_dt > dt:
                return (open_dt - dt).total_seconds()
        candidate += date.resolution
        if (candidate - dt.date()).days > 10:
            break
    return 0.0


def current_ist() -> datetime:
    return datetime.now(IST)
