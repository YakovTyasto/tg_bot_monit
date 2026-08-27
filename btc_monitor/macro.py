from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from .config import HTTP_TIMEOUT, MAURITIUS_TZ
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
        (ts, close) for ts, close in zip(timestamps, closes, strict=False) if close is not None
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


def _fred_metric(session: requests.Session, series: str, name: str, unit: str) -> MacroMetric:
    today = datetime.now(UTC).date()
    start = today - timedelta(days=45)
    response = session.get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv",
        params={"id": series, "cosd": start.isoformat(), "coed": today.isoformat()},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))
    points: list[tuple[datetime, float]] = []
    for row in rows:
        raw = row.get(series, "")
        if raw and raw != ".":
            observation_date = row.get("DATE") or row.get("observation_date")
            if not observation_date:
                continue
            points.append(
                (
                    datetime.fromisoformat(observation_date).replace(tzinfo=UTC),
                    float(raw),
                )
            )
    if not points:
        raise ValueError(f"FRED {series}: нет данных")
    previous = points[-6][1] if len(points) >= 6 else points[-1][1]
    return MacroMetric(
        name=name,
        value=points[-1][1],
        change=points[-1][1] - previous,
        unit=unit,
        as_of=points[-1][0],
        source="FRED",
    )


def _unfold_ics(text: str) -> list[str]:
    unfolded: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _parse_ics_datetime(line: str) -> datetime:
    prefix, value = line.split(":", 1)
    tz_match = re.search(r"TZID=([^;:]+)", prefix)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
    tz = ZoneInfo(tz_match.group(1)) if tz_match else UTC
    return parsed.replace(tzinfo=tz)


def _bls_events(session: requests.Session) -> list[MacroEvent]:
    response = session.get(
        "https://www.bls.gov/schedule/news_release/bls.ics",
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    events: list[MacroEvent] = []
    current: dict[str, str] | None = None
    for line in _unfold_ics(response.text):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            start_line = current.get("DTSTART")
            title = current.get("SUMMARY", "").replace("\\,", ",")
            if start_line and title:
                events.append(
                    MacroEvent(
                        title=title,
                        starts_at=_parse_ics_datetime(start_line).astimezone(MAURITIUS_TZ),
                        source="U.S. Bureau of Labor Statistics",
                    )
                )
            current = None
        elif current is not None:
            if line.startswith("DTSTART"):
                current["DTSTART"] = line
            elif line.startswith("SUMMARY:"):
                current["SUMMARY"] = line.split(":", 1)[1]

    now = datetime.now(MAURITIUS_TZ)
    end = now + timedelta(days=7)
    major_terms = (
        "consumer price index",
        "producer price index",
        "employment situation",
        "job openings",
        "import and export price",
        "real earnings",
    )
    return sorted(
        [
            event
            for event in events
            if now <= event.starts_at <= end
            and any(term in event.title.lower() for term in major_terms)
        ],
        key=lambda event: event.starts_at,
    )[:6]


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

    try:
        events = _bls_events(session)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        warnings.append(f"Календарь BLS временно недоступен ({type(exc).__name__})")
        events = []

    return MacroData(
        dollar_index=dollar,
        treasury_10y=treasury,
        fed_lower=fed_lower,
        fed_upper=fed_upper,
        events=events,
        warnings=warnings,
    )
