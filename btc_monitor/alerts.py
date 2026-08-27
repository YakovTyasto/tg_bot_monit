from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import BTC_QUANTITY, ENTRY_PRICE_USD, INITIAL_POSITION_USD
from .models import MarketAssessment, MarketData, NewsItem, TechnicalAnalysis
from .report import money, percent, zone


@dataclass(frozen=True)
class AlertDecision:
    should_send: bool
    event_keys: list[str]
    reasons: list[str]
    importance: str


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _fresh_event(sent: dict[str, str], key: str, hours: int) -> bool:
    timestamp = _parse_time(sent.get(key))
    return timestamp is None or datetime.now(UTC) - timestamp > timedelta(hours=hours)


def detect_alert(
    market: MarketData,
    technical: TechnicalAnalysis,
    assessment: MarketAssessment,
    news: list[NewsItem],
    state: dict,
) -> AlertDecision:
    alert_state = state.get("alerts") or {}
    sent = alert_state.get("sent") or {}
    previous_daily = state.get("last_daily") or {}
    reference_price = alert_state.get("last_reference_price") or previous_daily.get("price")
    previous_assessment = alert_state.get("last_assessment") or {
        "overall_score": previous_daily.get("overall_score"),
        "short_label": previous_daily.get("short_label"),
        "medium_label": previous_daily.get("medium_label"),
    }

    reasons: list[str] = []
    keys: list[str] = []
    critical = False
    one_hour_reference = market.hourly[-2].close
    six_hour_reference = market.hourly[-7].close if len(market.hourly) >= 7 else one_hour_reference
    move_1h = (market.price / one_hour_reference - 1) * 100
    move_6h = (market.price / six_hour_reference - 1) * 100

    if abs(move_1h) >= 2.5:
        direction = "up" if move_1h > 0 else "down"
        key = f"move1h:{direction}:{datetime.now(UTC):%Y%m%d}"
        if _fresh_event(sent, key, 12):
            reasons.append(f"Необычно сильное движение за час: {move_1h:+.2f}%.")
            keys.append(key)
            critical = abs(move_1h) >= 5
    if abs(move_6h) >= 5:
        direction = "up" if move_6h > 0 else "down"
        key = f"move6h:{direction}:{datetime.now(UTC):%Y%m%d}"
        if _fresh_event(sent, key, 12):
            reasons.append(f"Сильное движение примерно за 6 часов: {move_6h:+.2f}%.")
            keys.append(key)

    if reference_price:
        reference_price = float(reference_price)
        stored_resistances = previous_daily.get("resistances") or technical.resistances
        stored_supports = previous_daily.get("supports") or technical.supports
        for low, high in stored_resistances:
            low, high = float(low), float(high)
            rounded = round(high / 100) * 100
            key = f"resistance_break:{rounded}"
            if reference_price <= high < market.price and _fresh_event(sent, key, 48):
                reasons.append(f"BTC вышел выше зоны сопротивления {zone((low, high))}.")
                keys.append(key)
                break
        for low, high in stored_supports:
            low, high = float(low), float(high)
            rounded = round(low / 100) * 100
            key = f"support_loss:{rounded}"
            if reference_price >= low > market.price and _fresh_event(sent, key, 48):
                reasons.append(f"BTC потерял зону поддержки {zone((low, high))}.")
                keys.append(key)
                break
        pnl_change = (market.price - reference_price) * float(BTC_QUANTITY)
        if abs(pnl_change) >= 1500:
            direction = "up" if pnl_change > 0 else "down"
            bucket = int(abs(pnl_change) // 1000)
            key = f"pnl_change:{direction}:{bucket}k"
            if _fresh_event(sent, key, 24):
                reasons.append(
                    f"P&L позиции изменился примерно на {money(pnl_change, signed=True)} "
                    "от последней контрольной точки."
                )
                keys.append(key)

    old_score = previous_assessment.get("overall_score") if previous_assessment else None
    if old_score is not None and abs(assessment.overall_score - int(old_score)) >= 3:
        direction = "better" if assessment.overall_score > int(old_score) else "worse"
        key = f"assessment:{direction}:{assessment.overall_score}"
        if _fresh_event(sent, key, 24):
            reasons.append(
                f"Сводная оценка заметно изменилась: {int(old_score):+d} → "
                f"{assessment.overall_score:+d}."
            )
            keys.append(key)

    now = datetime.now(UTC)
    for item in news:
        key = f"news:{item.fingerprint}"
        if (
            item.importance >= 7
            and now - item.published_at <= timedelta(hours=3)
            and _fresh_event(sent, key, 168)
        ):
            reasons.append(f"{item.sentiment}: {item.summary} {item.why_it_matters}")
            keys.append(key)
            break

    if not reasons:
        return AlertDecision(False, [], [], "")

    last_sent = _parse_time(alert_state.get("last_sent_at"))
    if last_sent and now - last_sent < timedelta(hours=3) and not critical:
        return AlertDecision(False, [], [], "")
    importance = (
        "🔴 Риск усилился"
        if assessment.overall_score <= -3
        else ("🟢 Картина улучшилась" if assessment.overall_score >= 3 else "🟡 Значимое изменение")
    )
    return AlertDecision(True, keys, reasons, importance)


def build_alert_report(
    decision: AlertDecision,
    market: MarketData,
    technical: TechnicalAnalysis,
    assessment: MarketAssessment,
) -> str:
    current_value = market.price * float(BTC_QUANTITY)
    pnl = current_value - float(INITIAL_POSITION_USD)
    pnl_pct = (market.price / float(ENTRY_PRICE_USD) - 1) * 100
    reason_text = "\n• ".join(decision.reasons)
    return "\n\n".join(
        (
            "🚨 BTC ALERT",
            f"BTC: {money(market.price)}\n"
            f"Position P&L: {money(pnl, signed=True)} ({percent(pnl_pct, signed=True)})",
            f"Что произошло:\n• {reason_text}",
            "Почему это важно:\nДвижение, пробой зоны или новая информация могут "
            "изменить краткосрочный баланс спроса и предложения. Подтверждение требует "
            "закрепления цены и реакции на следующем интервале.",
            f"Что изменилось:\n{decision.importance}\n"
            f"Краткосрочно: {assessment.short_label}; среднесрочно: {assessment.medium_label}.",
            "Следующие ключевые уровни:\n"
            f"Support: {'; '.join(map(zone, technical.supports))}\n"
            f"Resistance: {'; '.join(map(zone, technical.resistances))}",
            "Уровни приблизительны. Это мониторинг, а не финансовая рекомендация.",
        )
    )
