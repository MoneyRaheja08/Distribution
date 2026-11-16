"""Shop time. Every "today" in the app is India time, whatever clock the server runs on."""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))   # India has no daylight saving


def today_ist():
    return datetime.now(IST).date()
