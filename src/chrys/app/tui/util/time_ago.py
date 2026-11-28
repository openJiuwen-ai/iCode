# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared localized relative timestamps for session lists."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from collections.abc import Callable


_JUST_NOW = msg("tui.sessions.time.just_now", fallback="just now")
_MINUTES_AGO = msg("tui.sessions.time.minutes_ago", fallback="{minute_count}m ago")
_HOURS_AGO = msg("tui.sessions.time.hours_ago", fallback="{hour_count}h ago")
_DAYS_AGO = msg(
    "tui.sessions.time.days_ago",
    fallback="1 day ago",
    plural_fallback="{count} days ago",
)
_MONTHS_AGO = msg(
    "tui.sessions.time.months_ago",
    fallback="1 month ago",
    plural_fallback="{count} months ago",
)
_YEARS_AGO = msg(
    "tui.sessions.time.years_ago",
    fallback="1 year ago",
    plural_fallback="{count} years ago",
)


def time_ago(
    dt: datetime,
    *,
    now: datetime | None = None,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """Format a datetime as a relative time string."""
    delta = (now or datetime.now(UTC)) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return render_message(_JUST_NOW.bind())
    minutes = seconds // 60
    if minutes < 60:
        return render_message(_MINUTES_AGO.bind(minute_count=minutes))
    hours = minutes // 60
    if hours < 24:
        return render_message(_HOURS_AGO.bind(hour_count=hours))
    days = hours // 24
    if days == 1:
        return render_message(_DAYS_AGO.bind(count=1))
    if days < 30:
        return render_message(_DAYS_AGO.bind(count=days))
    months = days // 30
    if months == 1:
        return render_message(_MONTHS_AGO.bind(count=1))
    if months < 12:
        return render_message(_MONTHS_AGO.bind(count=months))
    # Months are 30 days throughout this function, so twelve of them make a
    # year: 360 days reads "1 year ago", never "0 years ago".
    years = months // 12
    if years == 1:
        return render_message(_YEARS_AGO.bind(count=1))
    return render_message(_YEARS_AGO.bind(count=years))


_UNTIL_UNITS = (
    (timedelta(hours=1), timedelta(minutes=1)),
    (timedelta(days=1), timedelta(hours=1)),
    (timedelta(days=30), timedelta(days=1)),
    (timedelta(days=360), timedelta(days=30)),
)


def time_ago_until(dt: datetime, *, now: datetime | None = None) -> datetime:
    """When :func:`time_ago` for ``dt`` next reads differently."""
    elapsed = max(timedelta(), (now or datetime.now(UTC)) - dt)
    unit = next((unit for limit, unit in _UNTIL_UNITS if elapsed < limit), timedelta(days=360))
    try:
        return dt + (elapsed // unit + 1) * unit
    except OverflowError:
        # A time near the calendar's end never reads differently.
        return datetime.max.replace(tzinfo=dt.tzinfo)
