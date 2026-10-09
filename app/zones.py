"""CDR stamps are wall time in the timezone configured on the cloud."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CDR_ZONE_NAME = "America/Chicago"


def cloud_zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def from_cdr_wall(moment: datetime | None, zone: str = CDR_ZONE_NAME) -> datetime | None:
    """A CDR stamp is wall time in the cloud timezone, stored without a timezone."""
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=cloud_zone(zone))
    return moment


def cdr_wall(moment: datetime, zone: str = CDR_ZONE_NAME) -> datetime:
    """UTC instant written back as the naive wall time the CDR column stores."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(cloud_zone(zone)).replace(tzinfo=None)


def utc_offset(zone_name: str, moment: datetime | None = None) -> str:
    zone = cloud_zone(zone_name or "UTC")
    if moment is None:
        instant = datetime.now(timezone.utc)
    elif moment.tzinfo is None:
        instant = moment.replace(tzinfo=timezone.utc)
    else:
        instant = moment.astimezone(timezone.utc)
    shift = instant.astimezone(zone).utcoffset() or timedelta(0)
    seconds = int(shift.total_seconds())
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
