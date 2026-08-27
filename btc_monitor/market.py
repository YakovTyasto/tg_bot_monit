from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests

from .config import HTTP_TIMEOUT
from .models import Candle, MarketData


class MarketDataError(RuntimeError):
    pass


def _utc_timestamp(value: float | int) -> datetime:
    return datetime.fromtimestamp(float(value), tz=UTC)


def _coingecko_spot(session: requests.Session) -> tuple[float, float]:
    response = session.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": "bitcoin",
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        },
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    bitcoin = response.json()["bitcoin"]
    return float(bitcoin["usd"]), float(bitcoin["usd_24h_change"])


def _kraken_ohlc(session: requests.Session, interval: int) -> list[Candle]:
    response = session.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": "XBTUSD", "interval": interval},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    if payload.get("error"):
        raise ValueError("Kraken вернул ошибку данных")
    result = payload["result"]
    pair_key = next(key for key in result if key != "last")
    candles = [
        Candle(
            timestamp=_utc_timestamp(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
        )
        for row in result[pair_key]
    ]
    return sorted(candles, key=lambda item: item.timestamp)


def _kraken_spot(session: requests.Session) -> float:
    response = session.get(
        "https://api.kraken.com/0/public/Ticker",
        params={"pair": "XBTUSD"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise ValueError("Kraken вернул ошибку котировки")
    pair_key = next(iter(payload["result"]))
    return float(payload["result"][pair_key]["c"][0])


def _coinbase_candles(session: requests.Session, granularity: int) -> list[Candle]:
    response = session.get(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        params={"granularity": granularity},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    candles = [
        Candle(
            timestamp=_utc_timestamp(row[0]),
            low=float(row[1]),
            high=float(row[2]),
            open=float(row[3]),
            close=float(row[4]),
        )
        for row in response.json()
    ]
    return sorted(candles, key=lambda item: item.timestamp)


def _coinbase_spot(session: requests.Session) -> float:
    response = session.get(
        "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return float(response.json()["data"]["amount"])


def _coingecko_history(session: requests.Session, days: int, hourly: bool = False) -> list[Candle]:
    response = session.get(
        "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
        params={"vs_currency": "usd", "days": days},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    prices = response.json()["prices"]
    candles = [
        Candle(
            timestamp=_utc_timestamp(row[0] / 1000),
            open=float(row[1]),
            high=float(row[1]),
            low=float(row[1]),
            close=float(row[1]),
        )
        for row in prices
    ]
    if not hourly:
        return candles

    buckets: dict[datetime, list[Candle]] = {}
    for candle in candles:
        bucket = candle.timestamp.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(bucket, []).append(candle)
    return [
        Candle(
            timestamp=bucket,
            open=points[0].open,
            high=max(point.high for point in points),
            low=min(point.low for point in points),
            close=points[-1].close,
        )
        for bucket, points in sorted(buckets.items())
    ]


def _try(callable_, warnings: list[str], label: str):
    try:
        return callable_()
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        warnings.append(f"{label} временно недоступен ({type(exc).__name__})")
        return None


def fetch_market_data(session: requests.Session) -> MarketData:
    warnings: list[str] = []
    sources: list[str] = []

    spot = _try(lambda: _coingecko_spot(session), warnings, "CoinGecko spot")
    if spot is not None:
        price, change_24h = spot
        sources.append("CoinGecko")
    else:
        price = _try(lambda: _kraken_spot(session), warnings, "Kraken spot")
        if price is not None:
            sources.append("Kraken")
        else:
            price = _try(lambda: _coinbase_spot(session), warnings, "Coinbase spot")
            if price is not None:
                sources.append("Coinbase")
        change_24h = None

    daily = _try(lambda: _kraken_ohlc(session, 1440), warnings, "Kraken daily")
    if daily and len(daily) >= 200:
        sources.append("Kraken OHLC")
    else:
        fallback = _try(lambda: _coinbase_candles(session, 86400), warnings, "Coinbase daily")
        if fallback and len(fallback) >= 200:
            daily = fallback
            sources.append("Coinbase Exchange")
        else:
            fallback = _try(lambda: _coingecko_history(session, 365), warnings, "CoinGecko history")
            if fallback and len(fallback) >= 200:
                daily = fallback
                sources.append("CoinGecko history")

    hourly = _try(lambda: _kraken_ohlc(session, 60), warnings, "Kraken hourly")
    if hourly and len(hourly) >= 25:
        sources.append("Kraken hourly")
    else:
        fallback = _try(lambda: _coinbase_candles(session, 3600), warnings, "Coinbase hourly")
        if fallback and len(fallback) >= 25:
            hourly = fallback
            sources.append("Coinbase hourly")
        else:
            fallback = _try(
                lambda: _coingecko_history(session, 2, hourly=True),
                warnings,
                "CoinGecko intraday",
            )
            if fallback and len(fallback) >= 25:
                hourly = fallback
                sources.append("CoinGecko intraday")

    if price is None or not daily or len(daily) < 200 or not hourly or len(hourly) < 25:
        raise MarketDataError(
            "Не удалось получить минимально необходимые BTC-данные ни из одного "
            "набора резервных источников"
        )

    if change_24h is None:
        reference = hourly[-25].close
        change_24h = (price / reference - 1) * 100

    return MarketData(
        price=float(price),
        change_24h_pct=float(change_24h),
        daily=daily,
        hourly=hourly,
        sources=list(dict.fromkeys(sources)),
        warnings=warnings,
    )
