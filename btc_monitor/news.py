from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from time import struct_time
from urllib.parse import quote_plus

import feedparser
import requests

from .config import HTTP_TIMEOUT
from .models import NewsItem

DIRECT_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Decrypt": "https://decrypt.co/feed",
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "U.S. SEC": "https://www.sec.gov/news/pressreleases.rss",
    "U.S. BLS": "https://www.bls.gov/feed/bls_latest.rss",
}

GOOGLE_QUERIES = (
    "Bitcoin ETF (inflow OR outflow OR flows) when:1d",
    "Bitcoin (Federal Reserve OR inflation OR Treasury yields OR DXY) when:1d",
    "Bitcoin (regulation OR government OR exchange OR liquidation OR institutional) when:1d",
)

REPUTABLE_DISCOVERY_SOURCES = {
    "Reuters",
    "Bloomberg",
    "CNBC",
    "Financial Times",
    "The Wall Street Journal",
    "Associated Press",
    "CoinDesk",
    "The Block",
    "Decrypt",
    "Barron's",
    "Fortune",
    "Forbes",
    "Federal Reserve",
    "U.S. Securities and Exchange Commission",
    "U.S. Department of the Treasury",
    "U.S. Bureau of Labor Statistics",
    "BlackRock",
    "Fidelity",
    "Farside Investors",
}

RELEVANCE_TERMS = (
    "bitcoin",
    " btc",
    "crypto",
    "spot etf",
    "federal reserve",
    "fomc",
    "inflation",
    "consumer price",
    "interest rate",
    "treasury yield",
    "dollar index",
    "sec ",
)


def _published(entry) -> datetime | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def _clean(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _category(text: str) -> str:
    lower = text.lower()
    if "etf" in lower:
        return "ETF"
    if any(
        term in lower
        for term in (
            "strategy",
            "microstrategy",
            "institutional",
            "corporate treasury",
            "company buys",
            "company purchase",
        )
    ):
        return "Институциональные покупки/продажи"
    if any(
        term in lower
        for term in ("federal reserve", "fomc", "powell", "interest rate", "rate cut", "rate hike")
    ):
        return "ФРС и ставки"
    if any(
        term in lower for term in ("inflation", "consumer price", "producer price", "cpi", "ppi")
    ):
        return "Инфляция"
    if any(term in lower for term in ("treasury yield", "dxy", "dollar index", "us dollar")):
        return "Доллар и доходности"
    if any(
        term in lower
        for term in ("sec ", "regulation", "regulator", "government", "law", "ban", "sanction")
    ):
        return "Регулирование"
    if any(term in lower for term in ("exchange", "hack", "exploit", "custody", "outage")):
        return "Криптоинфраструктура"
    if any(term in lower for term in ("liquidation", "liquidated")):
        return "Ликвидации"
    return "Bitcoin / рынок"


def _sentiment(text: str) -> str:
    lower = text.lower()
    positive = (
        "inflow",
        "buys",
        "bought",
        "purchase",
        "approval",
        "approved",
        "adoption",
        "rate cut",
        "dovish",
        "lower inflation",
        "falling yields",
        "weaker dollar",
        "reserve bill",
    )
    negative = (
        "outflow",
        "sells",
        "sold",
        "hack",
        "exploit",
        "ban",
        "crackdown",
        "charges",
        "rate hike",
        "hawkish",
        "hot inflation",
        "rising yields",
        "stronger dollar",
        "liquidated",
        "liquidation",
    )
    score = sum(term in lower for term in positive) - sum(term in lower for term in negative)
    if score > 0:
        return "🟢 Bullish"
    if score < 0:
        return "🔴 Bearish"
    return "🟡 Neutral/Mixed"


def _importance(text: str, category: str, source: str) -> int:
    lower = text.lower()
    score = 3
    if category in {"ETF", "ФРС и ставки", "Инфляция", "Регулирование"}:
        score += 2
    if category in {"Институциональные покупки/продажи", "Криптоинфраструктура", "Ликвидации"}:
        score += 1
    if source in {"Reuters", "Bloomberg", "Federal Reserve", "U.S. SEC", "U.S. BLS"}:
        score += 2
    if any(
        term in lower
        for term in ("billion", "record", "fomc", "cpi", "hack", "ban", "approval", "liquidat")
    ):
        score += 1
    return min(score, 10)


def _summary(category: str, title: str) -> str:
    prefixes = {
        "ETF": "Появились новые данные или сообщение о потоках спотовых Bitcoin-ETF",
        "Институциональные покупки/продажи": "Сообщается о действиях крупного или корпоративного участника",
        "ФРС и ставки": "Вышло значимое сообщение о ФРС или процентных ставках",
        "Инфляция": "Опубликовано инфляционное сообщение, важное для ожиданий по ставкам",
        "Доллар и доходности": "Изменилась картина по доллару или доходностям US Treasuries",
        "Регулирование": "Появилось важное регуляторное или государственное сообщение",
        "Криптоинфраструктура": "Появилась значимая новость о криптоинфраструктуре",
        "Ликвидации": "Рынок столкнулся с заметной волной ликвидаций",
        "Bitcoin / рынок": "Появилась важная новость о Bitcoin",
    }
    return f"{prefixes[category]}: «{title}»."


def _why(category: str, sentiment: str) -> str:
    direction = {
        "🟢 Bullish": "Потенциально поддерживает спрос и риск-аппетит, но эффект зависит от масштаба и продолжительности.",
        "🔴 Bearish": "Может давить на спрос или усиливать риск распродажи; важна реакция цены, а не только заголовок.",
        "🟡 Neutral/Mixed": "Сигнал неоднозначен: значение будет определяться деталями и последующей реакцией рынка.",
    }[sentiment]
    context = {
        "ETF": "ETF-потоки отражают часть институционального спроса на спотовый BTC.",
        "Институциональные покупки/продажи": "Крупные сделки влияют на доступное предложение и настроение участников.",
        "ФРС и ставки": "Ожидания ставок меняют стоимость ликвидности и привлекательность рисковых активов.",
        "Инфляция": "Инфляция влияет на траекторию ставок ФРС и доходности облигаций.",
        "Доллар и доходности": "Сильный доллар и растущие реальные доходности часто создают встречный ветер для BTC.",
        "Регулирование": "Регулирование меняет доступ инвесторов, юридические риски и инфраструктуру рынка.",
        "Криптоинфраструктура": "Сбои и риски инфраструктуры могут быстро снизить доверие и ликвидность.",
        "Ликвидации": "Принудительное закрытие плечевых позиций способно ускорить движение цены.",
        "Bitcoin / рынок": "Новость может изменить краткосрочные ожидания и потоки капитала.",
    }[category]
    return f"{context} {direction}"


def _fingerprint(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _entry_to_item(entry, default_source: str, discovery: bool) -> NewsItem | None:
    title = _clean(entry.get("title", ""))
    description = _clean(entry.get("summary", ""))
    published = _published(entry)
    if not title or published is None:
        return None
    combined = f"{title} {description}"
    if not any(term in combined.lower() for term in RELEVANCE_TERMS):
        return None

    source_obj = entry.get("source") or {}
    source = _clean(source_obj.get("title", "")) if isinstance(source_obj, dict) else ""
    source = source or default_source
    if discovery and source not in REPUTABLE_DISCOVERY_SOURCES:
        return None

    category = _category(combined)
    sentiment = _sentiment(combined)
    return NewsItem(
        title=title,
        url=entry.get("link", ""),
        source=source,
        published_at=published,
        category=category,
        sentiment=sentiment,
        summary=_summary(category, title),
        why_it_matters=_why(category, sentiment),
        importance=_importance(combined, category, source),
        fingerprint=_fingerprint(title),
    )


def _parse_feed(
    session: requests.Session, url: str, source: str, discovery: bool
) -> list[NewsItem]:
    response = session.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError("Некорректная RSS-лента")
    result: list[NewsItem] = []
    for entry in parsed.entries:
        item = _entry_to_item(entry, source, discovery)
        if item:
            result.append(item)
    return result


def fetch_news(
    session: requests.Session, hours: int = 24, limit: int = 6
) -> tuple[list[NewsItem], list[str]]:
    warnings: list[str] = []
    items: list[NewsItem] = []

    for source, url in DIRECT_FEEDS.items():
        try:
            items.extend(_parse_feed(session, url, source, discovery=False))
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"RSS {source} недоступен ({type(exc).__name__})")

    for query in GOOGLE_QUERIES:
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        try:
            items.extend(_parse_feed(session, url, "Google News", discovery=True))
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"Google News RSS недоступен ({type(exc).__name__})")

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    unique: dict[str, NewsItem] = {}
    for item in items:
        if item.published_at < cutoff:
            continue
        existing = unique.get(item.fingerprint)
        if existing is None or item.importance > existing.importance:
            unique[item.fingerprint] = item

    ranked = sorted(
        unique.values(),
        key=lambda item: (item.importance, item.published_at),
        reverse=True,
    )
    return ranked[:limit], warnings
