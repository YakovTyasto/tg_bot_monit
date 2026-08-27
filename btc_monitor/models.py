from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class MarketData:
    price: float
    change_24h_pct: float
    daily: list[Candle]
    hourly: list[Candle]
    sources: list[str]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MacroMetric:
    name: str
    value: float
    change: float | None
    unit: str
    as_of: datetime
    source: str


@dataclass(frozen=True)
class MacroEvent:
    title: str
    starts_at: datetime
    source: str


@dataclass
class MacroData:
    dollar_index: MacroMetric | None = None
    treasury_10y: MacroMetric | None = None
    fed_lower: MacroMetric | None = None
    fed_upper: MacroMetric | None = None
    events: list[MacroEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    source: str
    published_at: datetime
    category: str
    sentiment: str
    summary: str
    why_it_matters: str
    importance: int
    fingerprint: str


@dataclass(frozen=True)
class TechnicalAnalysis:
    sma20: float
    sma50: float
    sma200: float | None
    return_7d_pct: float
    return_30d_pct: float
    daily_volatility_pct: float
    atr14_pct: float
    high_7d: float
    low_7d: float
    high_30d: float
    low_30d: float
    supports: list[tuple[float, float]]
    resistances: list[tuple[float, float]]
    short_trend: str
    medium_trend: str
    short_score: int
    medium_score: int


@dataclass(frozen=True)
class MarketAssessment:
    short_label: str
    medium_label: str
    short_score: int
    medium_score: int
    overall_score: int
    short_reason: str
    medium_reason: str


@dataclass(frozen=True)
class Scenario:
    label: str
    base_range: tuple[float, float]
    bull_range: tuple[float, float]
    bear_range: tuple[float, float]
    base_trigger: str
    bull_trigger: str
    bear_trigger: str
