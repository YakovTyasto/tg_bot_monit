from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .config import BTC_QUANTITY, ENTRY_PRICE_USD, INITIAL_POSITION_USD, MAURITIUS_TZ
from .models import (
    MacroData,
    MarketAssessment,
    MarketData,
    NewsItem,
    Scenario,
    TechnicalAnalysis,
)


def money(value: float | Decimal, signed: bool = False) -> str:
    number = float(value)
    sign = "+" if signed and number > 0 else ""
    return f"{sign}${number:,.0f}"


def percent(value: float, signed: bool = False) -> str:
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.2f}%"


def zone(value: tuple[float, float]) -> str:
    return f"{money(value[0])}–{money(value[1])}"


def _position_section(market: MarketData) -> str:
    quantity = float(BTC_QUANTITY)
    current_value = quantity * market.price
    pnl = current_value - float(INITIAL_POSITION_USD)
    pnl_pct = pnl / float(INITIAL_POSITION_USD) * 100
    return "\n".join(
        (
            "1. ПОЗИЦИЯ",
            f"BTC/USD: {money(market.price)}",
            f"Цена входа: {money(ENTRY_PRICE_USD)}",
            f"Количество BTC: {quantity:.8f}",
            f"Текущая стоимость: {money(current_value)}",
            f"P&L: {money(pnl, signed=True)} ({percent(pnl_pct, signed=True)})",
            f"Изменение BTC за 24ч: {percent(market.change_24h_pct, signed=True)}",
        )
    )


def _market_section(technical: TechnicalAnalysis) -> str:
    sma200 = money(technical.sma200) if technical.sma200 is not None else "нет данных"
    return "\n".join(
        (
            "2. СТРУКТУРА РЫНКА",
            f"Краткосрочный тренд: {technical.short_trend}.",
            f"Среднесрочный тренд: {technical.medium_trend}.",
            f"Волатильность: дневная σ ≈ {technical.daily_volatility_pct:.2f}%; "
            f"ATR(14) ≈ {technical.atr14_pct:.2f}% цены.",
            f"SMA20: {money(technical.sma20)} | SMA50: {money(technical.sma50)} | SMA200: {sma200}",
            f"7 дней: high {money(technical.high_7d)}, low {money(technical.low_7d)}, "
            f"изменение {percent(technical.return_7d_pct, signed=True)}.",
            f"30 дней: high {money(technical.high_30d)}, low {money(technical.low_30d)}, "
            f"изменение {percent(technical.return_30d_pct, signed=True)}.",
            "Приблизительные зоны поддержки: " + "; ".join(map(zone, technical.supports)),
            "Приблизительные зоны сопротивления: " + "; ".join(map(zone, technical.resistances)),
            "Уровни являются зонами реакции, а не точными линиями.",
        )
    )


def _metric_line(metric, rising_is_bad: bool) -> str:
    if metric is None:
        return "данные временно недоступны"
    change = metric.change
    change_text = ""
    effect = ""
    if change is not None:
        change_text = f", изменение примерно {change:+.2f}{metric.unit} за 5 наблюдений"
        if abs(change) < (0.08 if metric.unit == "%" else 0.3):
            effect = " — заметного импульса нет"
        elif (change > 0) == rising_is_bad:
            effect = " — обычно это встречный ветер для BTC"
        else:
            effect = " — обычно это поддерживает риск-аппетит"
    return (
        f"{metric.value:.2f}{metric.unit}{change_text}{effect} "
        f"({metric.source}, {metric.as_of:%d.%m})"
    )


def _macro_section(macro: MacroData, news: list[NewsItem]) -> str:
    lines = [
        "3. МАКРО",
        "DXY / индекс доллара: " + _metric_line(macro.dollar_index, rising_is_bad=True),
        "US Treasury 10Y: " + _metric_line(macro.treasury_10y, rising_is_bad=True),
    ]
    if macro.fed_lower and macro.fed_upper:
        lines.append(
            f"Текущий целевой диапазон ставки ФРС: {macro.fed_lower.value:.2f}%–"
            f"{macro.fed_upper.value:.2f}% (FRED). Более высокие ожидаемые ставки обычно "
            "давят на ликвидность; смягчение обычно помогает рисковым активам."
        )
    else:
        lines.append("Текущий диапазон ставки ФРС: данные временно недоступны.")

    fed_news = next((item for item in news if item.category == "ФРС и ставки"), None)
    if fed_news:
        lines.append(f"Ожидания ставки: {fed_news.summary}")
    else:
        lines.append(
            "Ожидания ставки: за последние 24ч надежного нового сигнала в доступных "
            "источниках не найдено; числовые вероятности не выдумываются."
        )
    if macro.inflation_cpi:
        inflation_line = (
            f"Инфляция США, CPI год к году: {macro.inflation_cpi.value:.2f}%"
            f" (данные за {macro.inflation_cpi.as_of:%m.%Y}, FRED)"
        )
        if macro.inflation_cpi.change is not None:
            direction = (
                "ускорение"
                if macro.inflation_cpi.change > 0.05
                else ("замедление" if macro.inflation_cpi.change < -0.05 else "почти без изменений")
            )
            inflation_line += (
                f"; к предыдущему месяцу {macro.inflation_cpi.change:+.2f} п.п. — {direction}"
            )
        if macro.inflation_core_cpi:
            inflation_line += f". Core CPI: {macro.inflation_core_cpi.value:.2f}%"
        lines.append(inflation_line + ".")
        lines.append(
            "Более высокая инфляция обычно удерживает ставки выше дольше, что давит на "
            "рисковые активы; устойчивое замедление обычно работает в пользу BTC."
        )
    else:
        lines.append("Инфляция: официальные данные CPI временно недоступны.")

    inflation_news = next((item for item in news if item.category == "Инфляция"), None)
    if inflation_news:
        lines.append(f"Инфляционные новости за 24ч: {inflation_news.summary}")
    else:
        lines.append("Инфляционные новости: нового значимого релиза за последние 24ч не найдено.")

    if macro.events:
        now = datetime.now(MAURITIUS_TZ)
        lines.append("Ближайшие макрособытия (время Маврикия):")
        for event in macro.events:
            days_left = max(0, (event.starts_at - now).days)
            lines.append(
                f"• {event.starts_at:%d.%m %H:%M} (через {days_left} дн.) — "
                f"{event.title} ({event.source})"
            )
    else:
        lines.append("Календарь ближайших макрособытий временно недоступен.")
    return "\n".join(lines)


def _news_section(news: list[NewsItem]) -> str:
    lines = ["4. ВАЖНЫЕ НОВОСТИ ЗА 24 ЧАСА"]
    if not news:
        lines.append(
            "Надежных и действительно значимых новостей в доступных лентах не найдено. "
            "Бот не заполняет раздел кликбейтом."
        )
        return "\n".join(lines)
    for index, item in enumerate(news, start=1):
        local_time = item.published_at.astimezone(MAURITIUS_TZ)
        lines.extend(
            (
                f"{index}) {item.sentiment} — {item.category}",
                item.summary,
                f"Почему важно: {item.why_it_matters}",
                f"Источник: {item.source}, {local_time:%d.%m %H:%M}",
                item.url,
            )
        )
    return "\n".join(lines)


def _assessment_section(assessment: MarketAssessment) -> str:
    return "\n".join(
        (
            "5. ОЦЕНКА РЫНКА",
            f"Краткосрочно: {assessment.short_label}",
            assessment.short_reason,
            f"Среднесрочно: {assessment.medium_label}",
            assessment.medium_reason,
        )
    )


def _scenario_section(scenarios: list[Scenario]) -> str:
    lines = [
        "6. СЦЕНАРИИ, НЕ ТОЧНЫЙ ПРОГНОЗ",
        "Диапазоны ориентировочные и расширяются вместе с горизонтом и волатильностью.",
    ]
    for item in scenarios:
        lines.extend(
            (
                f"\n{item.label}",
                f"BASE CASE: {money(item.base_range[0])}–{money(item.base_range[1])}. {item.base_trigger}",
                f"BULL CASE: {money(item.bull_range[0])}–{money(item.bull_range[1])}. {item.bull_trigger}",
                f"BEAR CASE: {money(item.bear_range[0])}–{money(item.bear_range[1])}. {item.bear_trigger}",
            )
        )
    return "\n".join(lines)


def compare_with_previous(
    previous: dict | None, assessment: MarketAssessment, market: MarketData
) -> tuple[str, str]:
    if not previous:
        return "Первый отчёт", "Предыдущего отчёта пока нет."
    old_score = int(previous.get("overall_score", 0))
    difference = assessment.overall_score - old_score
    old_price = float(previous.get("price", market.price))
    price_change = (market.price / old_price - 1) * 100 if old_price else 0.0
    if difference >= 2:
        label = "🟢 Улучшилась"
    elif difference <= -2:
        label = "🔴 Ухудшилась"
    else:
        label = "🟡 Без существенных изменений"
    explanation = (
        f"Сводный сигнал изменился с {old_score:+d} до {assessment.overall_score:+d}; "
        f"BTC относительно прошлого отчёта {price_change:+.2f}%. "
        f"Краткосрочная оценка: {previous.get('short_label', 'нет данных')} → "
        f"{assessment.short_label}; среднесрочная: "
        f"{previous.get('medium_label', 'нет данных')} → {assessment.medium_label}."
    )
    return label, explanation


def build_daily_report(
    market: MarketData,
    technical: TechnicalAnalysis,
    macro: MacroData,
    news: list[NewsItem],
    assessment: MarketAssessment,
    scenarios: list[Scenario],
    previous: dict | None,
    extra_warnings: list[str] | None = None,
) -> str:
    now = datetime.now(MAURITIUS_TZ)
    change_label, change_explanation = compare_with_previous(previous, assessment, market)
    warnings = list(dict.fromkeys(market.warnings + macro.warnings + (extra_warnings or [])))
    sections = [
        f"₿ ЕЖЕДНЕВНЫЙ ОТЧЁТ BTC\n{now:%d.%m.%Y %H:%M} (Маврикий, UTC+4)",
        _position_section(market),
        _market_section(technical),
        _macro_section(macro, news),
        _news_section(news),
        _assessment_section(assessment),
        _scenario_section(scenarios),
        f"7. ИЗМЕНЕНИЕ КАРТИНЫ СО ВЧЕРА:\n{change_label}\n{change_explanation}",
        "Источники рынка: " + ", ".join(market.sources) + ".",
    ]
    if warnings:
        sections.append(
            "Часть источников была недоступна, использованы резервные данные:\n• "
            + "\n• ".join(warnings[:8])
        )
    sections.append(
        "Это автоматический мониторинг сценариев, а не финансовая рекомендация. "
        "Диапазоны и технические зоны приблизительны."
    )
    return "\n\n".join(sections)
