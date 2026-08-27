from __future__ import annotations

import math
import statistics

from .models import (
    Candle,
    MacroData,
    MarketAssessment,
    MarketData,
    NewsItem,
    Scenario,
    TechnicalAnalysis,
)


def _sma(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Для SMA{period} недостаточно данных")
    return statistics.fmean(values[-period:])


def _return(values: list[float], periods: int) -> float:
    if len(values) <= periods:
        return 0.0
    return (values[-1] / values[-periods - 1] - 1) * 100


def _atr(candles: list[Candle], period: int = 14) -> float:
    recent = candles[-(period + 1) :]
    true_ranges: list[float] = []
    for previous, current in zip(recent, recent[1:], strict=False):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return statistics.fmean(true_ranges) if true_ranges else 0.0


def _cluster_levels(levels: list[float], tolerance_pct: float = 1.5) -> list[float]:
    clusters: list[list[float]] = []
    for level in sorted(levels):
        if not clusters:
            clusters.append([level])
            continue
        center = statistics.fmean(clusters[-1])
        if abs(level / center - 1) * 100 <= tolerance_pct:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    weighted = [(statistics.fmean(cluster), len(cluster)) for cluster in clusters]
    return [value for value, _ in sorted(weighted, key=lambda pair: pair[1], reverse=True)]


def _zones(
    candles: list[Candle], price: float, atr_value: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    recent = candles[-120:]
    lows: list[float] = []
    highs: list[float] = []
    for index in range(2, len(recent) - 2):
        window = recent[index - 2 : index + 3]
        current = recent[index]
        if current.low == min(candle.low for candle in window):
            lows.append(current.low)
        if current.high == max(candle.high for candle in window):
            highs.append(current.high)

    if not lows:
        lows = [candle.low for candle in recent]
    if not highs:
        highs = [candle.high for candle in recent]

    supports = sorted((level for level in _cluster_levels(lows) if level < price), reverse=True)[:2]
    resistances = sorted(level for level in _cluster_levels(highs) if level > price)[:2]

    width = max(price * 0.004, atr_value * 0.25)

    def as_zone(level: float) -> tuple[float, float]:
        return (max(0.0, level - width), level + width)

    if not supports:
        supports = [min(candle.low for candle in recent[-30:])]
    if not resistances:
        resistances = [max(candle.high for candle in recent[-30:])]
    return [as_zone(level) for level in supports], [as_zone(level) for level in resistances]


def analyze_technical(market: MarketData) -> TechnicalAnalysis:
    closes = [candle.close for candle in market.daily]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200) if len(closes) >= 200 else None
    return_7d = _return(closes, 7)
    return_30d = _return(closes, 30)

    returns = [
        math.log(current / previous)
        for previous, current in zip(closes[-31:-1], closes[-30:], strict=False)
        if previous > 0 and current > 0
    ]
    daily_vol = statistics.pstdev(returns) * 100 if len(returns) >= 2 else 0.0
    atr_value = _atr(market.daily)
    atr_pct = atr_value / market.price * 100
    supports, resistances = _zones(market.daily, market.price, atr_value)

    short_score = 0
    short_score += 1 if market.price > sma20 else -1
    short_score += 1 if return_7d > 1 else (-1 if return_7d < -1 else 0)
    short_score += 1 if market.change_24h_pct > 1 else (-1 if market.change_24h_pct < -1 else 0)

    medium_score = 0
    medium_score += 1 if sma20 > sma50 else -1
    medium_score += 1 if market.price > sma50 else -1
    if sma200 is not None:
        medium_score += 1 if market.price > sma200 else -1
    medium_score += 1 if return_30d > 3 else (-1 if return_30d < -3 else 0)

    if short_score >= 2:
        short_trend = "восходящий"
    elif short_score <= -2:
        short_trend = "нисходящий"
    else:
        short_trend = "боковой / смешанный"
    if medium_score >= 2:
        medium_trend = "восходящий"
    elif medium_score <= -2:
        medium_trend = "нисходящий"
    else:
        medium_trend = "нейтральный / переходный"

    last7 = market.daily[-7:]
    last30 = market.daily[-30:]
    return TechnicalAnalysis(
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        return_7d_pct=return_7d,
        return_30d_pct=return_30d,
        daily_volatility_pct=daily_vol,
        atr14_pct=atr_pct,
        high_7d=max(candle.high for candle in last7),
        low_7d=min(candle.low for candle in last7),
        high_30d=max(candle.high for candle in last30),
        low_30d=min(candle.low for candle in last30),
        supports=supports,
        resistances=resistances,
        short_trend=short_trend,
        medium_trend=medium_trend,
        short_score=short_score,
        medium_score=medium_score,
    )


def _label(score: int) -> str:
    if score >= 2:
        return "🟢 bullish"
    if score <= -2:
        return "🔴 bearish"
    return "🟡 neutral"


def assess_market(
    technical: TechnicalAnalysis, macro: MacroData, news: list[NewsItem]
) -> MarketAssessment:
    macro_score = 0
    macro_notes: list[str] = []
    if macro.dollar_index and macro.dollar_index.change is not None:
        change = macro.dollar_index.change
        if change > 0.5:
            macro_score -= 1
            macro_notes.append("доллар усиливается")
        elif change < -0.5:
            macro_score += 1
            macro_notes.append("доллар ослабевает")
    if macro.treasury_10y and macro.treasury_10y.change is not None:
        change = macro.treasury_10y.change
        if change > 0.10:
            macro_score -= 1
            macro_notes.append("доходность 10Y растёт")
        elif change < -0.10:
            macro_score += 1
            macro_notes.append("доходность 10Y снижается")

    news_values = {
        "🟢 Bullish": 1,
        "🔴 Bearish": -1,
        "🟡 Neutral/Mixed": 0,
    }
    news_total = sum(news_values[item.sentiment] for item in news[:5])
    news_score = 1 if news_total >= 2 else (-1 if news_total <= -2 else 0)

    short_score = technical.short_score + news_score
    medium_score = technical.medium_score + macro_score + news_score
    short_reason = (
        f"Цена относительно SMA20, динамика 7 дней ({technical.return_7d_pct:+.1f}%) "
        f"и изменение за 24ч формируют {technical.short_trend} сигнал."
    )
    macro_text = (
        ", ".join(macro_notes) if macro_notes else "макро-сигналы смешанные или данных мало"
    )
    medium_reason = (
        f"Связка SMA20/SMA50, положение к SMA200 и 30-дневная динамика "
        f"({technical.return_30d_pct:+.1f}%) дают {technical.medium_trend} фон; {macro_text}."
    )
    return MarketAssessment(
        short_label=_label(short_score),
        medium_label=_label(medium_score),
        short_score=short_score,
        medium_score=medium_score,
        overall_score=short_score + medium_score,
        short_reason=short_reason,
        medium_reason=medium_reason,
    )


def build_scenarios(
    market: MarketData, technical: TechnicalAnalysis, assessment: MarketAssessment
) -> list[Scenario]:
    price = market.price
    daily_move = max(
        0.018, min(0.08, max(technical.daily_volatility_pct, technical.atr14_pct * 0.55) / 100)
    )
    support = technical.supports[0][0]
    resistance = technical.resistances[0][1]
    bias = max(-0.35, min(0.35, assessment.overall_score / 14))

    horizons = (
        ("Следующие 24 часа", 1, 0.10),
        ("Следующие 2–3 дня", 3, 0.14),
        ("Следующие 7 дней", 7, 0.22),
        ("Следующие 1–3 месяца", 60, 0.45),
    )
    scenarios: list[Scenario] = []
    for label, days, cap in horizons:
        move = min(cap, daily_move * math.sqrt(days))
        base_low = price * (1 - move * (1 - bias) * 0.65)
        base_high = price * (1 + move * (1 + bias) * 0.65)
        bull_low = max(price, resistance * 0.995)
        bull_high = price * (1 + move * 1.45)
        bear_low = price * (1 - move * 1.45)
        bear_high = min(price, support * 1.005)
        if bull_high < bull_low:
            bull_high = bull_low * 1.03
        if bear_high < bear_low:
            bear_high = bear_low * 1.03
        scenarios.append(
            Scenario(
                label=label,
                base_range=(base_low, base_high),
                bull_range=(bull_low, bull_high),
                bear_range=(bear_low, bear_high),
                base_trigger="Цена остаётся между ближайшими зонами, без нового сильного макро-импульса.",
                bull_trigger="Закрепление выше сопротивления при росте спроса/ETF-потоков или смягчении макро-фона.",
                bear_trigger="Потеря поддержки на росте доходностей/доллара либо при негативном рыночном событии.",
            )
        )
    return scenarios
