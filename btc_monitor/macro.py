from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from .config import HTTP_TIMEOUT, MAURITIUS_TZ, UTC
from .models import MacroData, MacroEvent, MacroMetric


def _yahoo_metric(session: requests.Session, symbol: str, name: str, unit: str) -> MacroMetric:
    encoded = quote(symbol, safe="")
    response = session.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}",
        params={"range": "1mo", "interval": "1d"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    points = [
        (ts, close) for ts, close in zip(timestamps, closes) if close is not None
    ]
    if len(points) < 2:
        raise ValueError("Недостаточно макро-точек")
    last_ts, last = points[-1]
    previous = points[-6][1] if len(points) >= 6 else points[-2][1]
    return MacroMetric(
        name=name,
        value=float(last),
        change=float(last) - float(previous),
        unit=unit,
        as_of=datetime.fromtimestamp(last_ts, tz=UTC),
        source="Yahoo Finance",
    )


def _fred_series(session: requests.Session, series: str, days: int) -> list[tuple[datetime, float]]:
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days)
    response = session.get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv",
        params={"id": series, "cosd": start.isoformat(), "coed": today.isoformat()},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    fieldnames = reader.fieldnames or []
    date_column = next(
        (name for name in fieldnames if name.upper() in {"DATE", "OBSERVATION_DATE"}), None
    )
    value_column = series if series in fieldnames else next(
        (name for name in fieldnames if name != date_column), None
    )
    if not date_column or not value_column:
        raise ValueError(f"FRED {series}: неожиданный формат CSV")

    points: list[tuple[datetime, float]] = []
    for row in reader:
        raw = (row.get(value_column) or "").strip()
        observation_date = (row.get(date_column) or "").strip()
        if not raw or raw == "." or not observation_date:
            continue
        points.append((datetime.fromisoformat(observation_date).replace(tzinfo=UTC), float(raw)))
    if not points:
        raise ValueError(f"FRED {series}: нет данных")
    return points


def _fred_metric(session: requests.Session, series: str, name: str, unit: str) -> MacroMetric:
    points = _fred_series(session, series, days=45)
    previous = points[-6][1] if len(points) >= 6 else points[-1][1]
    return MacroMetric(
        name=name,
        value=points[-1][1],
        change=points[-1][1] - previous,
        unit=unit,
        as_of=points[-1][0],
        source="FRED",
    )


def _fred_yoy_metric(session: requests.Session, series: str, name: str) -> MacroMetric:
    """Year-over-year change of a monthly FRED index, e.g. headline or core CPI."""
    points = _fred_series(session, series, days=430)
    if len(points) < 13:
        raise ValueError(f"FRED {series}: недостаточно месяцев для расчёта год к году")

    def yoy(offset: int) -> float:
        current = points[-1 - offset][1]
        year_ago = points[-13 - offset][1]
        if year_ago <= 0:
            raise ValueError(f"FRED {series}: некорректное базовое значение")
        return (current / year_ago - 1) * 100

    latest = yoy(0)
    change = latest - yoy(1) if len(points) >= 14 else None
    return MacroMetric(
        name=name,
        value=latest,
        change=change,
        unit="%",
        as_of=points[-1][0],
        source="FRED",
    )


def _clean_html(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value).strip()


MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# The FOMC statement is published at 14:00 in Washington on the final day of a meeting.
FOMC_STATEMENT_HOUR = 14
NEW_YORK_TZ = ZoneInfo("America/New_York")


def _fomc_meeting_datetime(year: int, month_text: str, date_text: str) -> datetime | None:
    """Turn one calendar row (e.g. "Apr/May" + "30-1*") into the statement datetime."""
    month_parts = [part.strip()[:3].lower() for part in month_text.split("/") if part.strip()]
    months = [MONTH_NUMBERS[part] for part in month_parts if part in MONTH_NUMBERS]
    days = [int(value) for value in re.findall(r"\d+", date_text.split("(")[0])]
    if not months or not days:
        return None
    month = months[-1]
    day = days[-1]
    # A meeting that starts in December and ends in January belongs to the next year.
    meeting_year = year + 1 if month == 1 and months[0] == 12 else year
    try:
        local = datetime(meeting_year, month, day, FOMC_STATEMENT_HOUR, tzinfo=NEW_YORK_TZ)
    except ValueError:
        return None
    return local.astimezone(MAURITIUS_TZ)


def _fomc_events(session: requests.Session, limit: int = 2) -> list[MacroEvent]:
    """Upcoming FOMC decisions from the Federal Reserve's own published calendar."""
    response = session.get(
        "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    page = re.sub(r"\s+", " ", response.text)

    events: list[MacroEvent] = []
    headings = list(re.finditer(r'<a id="\d+">(\d{4}) FOMC Meetings</a>', page))
    if not headings:
        raise ValueError("Календарь FOMC: неожиданная структура страницы")
    for index, heading in enumerate(headings):
        year = int(heading.group(1))
        block_end = headings[index + 1].start() if index + 1 < len(headings) else len(page)
        block = page[heading.end() : block_end]
        months = re.findall(r"fomc-meeting__month[^>]*>\s*<strong>(.*?)</strong>", block)
        dates = re.findall(r"fomc-meeting__date[^>]*>(.*?)</div>", block)
        for month_text, date_text in zip(months, dates):
            starts_at = _fomc_meeting_datetime(year, month_text, _clean_html(date_text))
            if starts_at is None:
                continue
            events.append(
                MacroEvent(
                    title="Заседание FOMC: решение по ставке ФРС",
                    starts_at=starts_at,
                    source="Federal Reserve Board",
                )
            )

    now = datetime.now(MAURITIUS_TZ)
    upcoming = sorted(
        (event for event in events if event.starts_at >= now), key=lambda event: event.starts_at
    )
    if not upcoming:
        raise ValueError("Календарь FOMC: будущих заседаний не найдено")
    return upcoming[:limit]


def _safe_metric(callable_, warnings: list[str], label: str) -> MacroMetric | None:
    try:
        return callable_()
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        warnings.append(f"{label} временно недоступен ({type(exc).__name__})")
        return None


def fetch_macro_data(session: requests.Session) -> MacroData:
    warnings: list[str] = []
    dollar = _safe_metric(lambda: _yahoo_metric(session, "DX-Y.NYB", "DXY", ""), warnings, "DXY")
    if dollar is None:
        dollar = _safe_metric(
            lambda: _fred_metric(
                session,
                "DTWEXBGS",
                "Широкий торгово-взвешенный индекс доллара",
                "",
            ),
            warnings,
            "Индекс доллара FRED",
        )

    treasury = _safe_metric(
        lambda: _fred_metric(session, "DGS10", "US Treasury 10Y", "%"),
        warnings,
        "US Treasury 10Y",
    )
    if treasury is None:
        treasury = _safe_metric(
            lambda: _yahoo_metric(session, "^TNX", "US Treasury 10Y", "%"),
            warnings,
            "US Treasury 10Y Yahoo",
        )
        if treasury and treasury.value > 20:
            treasury = MacroMetric(
                name=treasury.name,
                value=treasury.value / 10,
                change=(treasury.change / 10 if treasury.change is not None else None),
                unit=treasury.unit,
                as_of=treasury.as_of,
                source=treasury.source,
            )

    fed_lower = _safe_metric(
        lambda: _fred_metric(session, "DFEDTARL", "Нижняя граница ставки ФРС", "%"),
        warnings,
        "Нижняя граница ставки ФРС",
    )
    fed_upper = _safe_metric(
        lambda: _fred_metric(session, "DFEDTARU", "Верхняя граница ставки ФРС", "%"),
        warnings,
        "Верхняя граница ставки ФРС",
    )

    inflation = _safe_metric(
        lambda: _fred_yoy_metric(session, "CPIAUCSL", "CPI год к году"),
        warnings,
        "Индекс потребительских цен (CPI)",
    )
    core_inflation = _safe_metric(
        lambda: _fred_yoy_metric(session, "CPILFESL", "Core CPI год к году"),
        warnings,
        "Базовый CPI",
    )

    try:
        events = _fomc_events(session)
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        warnings.append(f"Календарь FOMC временно недоступен ({type(exc).__name__})")
        events = []

    return MacroData(
        dollar_index=dollar,
        treasury_10y=treasury,
        fed_lower=fed_lower,
        fed_upper=fed_upper,
        inflation_cpi=inflation,
        inflation_core_cpi=core_inflation,
        events=events,
        warnings=warnings,
    )
