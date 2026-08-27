from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btc_monitor.alerts import detect_alert
from btc_monitor.analysis import analyze_technical, assess_market, build_scenarios
from btc_monitor.config import BTC_QUANTITY, ENTRY_PRICE_USD, INITIAL_POSITION_USD
from btc_monitor.models import Candle, MacroData, MarketData
from btc_monitor.report import build_daily_report
from btc_monitor.state import load_state, save_state
from btc_monitor.telegram import TelegramError, send_telegram_message, split_message


def market_fixture(price: float = 85_000.0) -> MarketData:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    daily: list[Candle] = []
    for index in range(260):
        close = 62_000 + index * 85 + ((index % 9) - 4) * 120
        daily.append(
            Candle(
                timestamp=start + timedelta(days=index),
                open=close - 80,
                high=close + 500,
                low=close - 500,
                close=close,
            )
        )
    daily[-1] = Candle(daily[-1].timestamp, price - 100, price + 500, price - 500, price)
    hourly = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=price * 0.999,
            high=price * 1.002,
            low=price * 0.998,
            close=price * (0.98 + 0.02 * index / 49),
        )
        for index in range(50)
    ]
    return MarketData(
        price=price,
        change_24h_pct=2.1,
        daily=daily,
        hourly=hourly,
        sources=["fixture"],
    )


def test_position_math_is_exact() -> None:
    assert BTC_QUANTITY == INITIAL_POSITION_USD / ENTRY_PRICE_USD
    assert BTC_QUANTITY.quantize(Decimal("0.00000001")) == Decimal("0.63104223")
    assert float(BTC_QUANTITY * ENTRY_PRICE_USD) == 50_000.0


def test_analysis_and_report_contain_required_sections() -> None:
    market = market_fixture()
    macro = MacroData()
    technical = analyze_technical(market)
    assessment = assess_market(technical, macro, [])
    scenarios = build_scenarios(market, technical, assessment)
    report = build_daily_report(market, technical, macro, [], assessment, scenarios, previous=None)
    for expected in (
        "1. ПОЗИЦИЯ",
        "2. СТРУКТУРА РЫНКА",
        "3. МАКРО",
        "4. ВАЖНЫЕ НОВОСТИ",
        "5. ОЦЕНКА РЫНКА",
        "6. СЦЕНАРИИ",
        "ИЗМЕНЕНИЕ КАРТИНЫ СО ВЧЕРА",
        "Предыдущего отчёта пока нет",
    ):
        assert expected in report


def test_telegram_split_never_exceeds_limit() -> None:
    text = "\n\n".join(["Раздел " + ("данные " * 900) for _ in range(3)])
    chunks = split_message(text, limit=3900)
    assert len(chunks) > 3
    assert all(0 < len(chunk) <= 3900 for chunk in chunks)
    assert "Раздел" in chunks[0]


def test_alert_is_not_sent_without_significant_change() -> None:
    market = market_fixture()
    macro = MacroData()
    technical = analyze_technical(market)
    assessment = assess_market(technical, macro, [])
    state = {
        "last_daily": {
            "price": market.price,
            "overall_score": assessment.overall_score,
            "short_label": assessment.short_label,
            "medium_label": assessment.medium_label,
        },
        "alerts": {
            "last_sent_at": None,
            "last_reference_price": market.price,
            "last_assessment": {"overall_score": assessment.overall_score},
            "sent": {},
        },
    }
    decision = detect_alert(market, technical, assessment, [], state)
    assert decision.should_send is False


def test_alert_detects_crossing_of_previously_stored_zone() -> None:
    market = market_fixture()
    macro = MacroData()
    technical = analyze_technical(market)
    assessment = assess_market(technical, macro, [])
    state = {
        "last_daily": {
            "price": 84_000,
            "overall_score": assessment.overall_score,
            "resistances": [[84_300, 84_500]],
            "supports": [[80_000, 80_500]],
        },
        "alerts": {
            "last_sent_at": None,
            "last_reference_price": 84_000,
            "last_assessment": {"overall_score": assessment.overall_score},
            "sent": {},
        },
    }
    decision = detect_alert(market, technical, assessment, [], state)
    assert decision.should_send is True
    assert any("сопротивления" in reason for reason in decision.reasons)


def test_telegram_connection_error_does_not_expose_token(monkeypatch) -> None:
    class FakeTelegramSession:
        def __init__(self) -> None:
            self.headers = {}

        def post(self, *args, **kwargs):
            import requests

            raise requests.ConnectionError("connection failed")

        def close(self) -> None:
            pass

    class DataSession:
        headers = {"User-Agent": "test"}

    monkeypatch.setattr("btc_monitor.telegram.requests.Session", FakeTelegramSession)
    token = "123456:SECRET_TOKEN_THAT_MUST_NOT_LEAK"
    try:
        send_telegram_message(DataSession(), token, "42", "test")
    except TelegramError as exc:
        assert token not in str(exc)
    else:
        raise AssertionError("TelegramError was expected")


def test_state_round_trip(tmp_path) -> None:
    path = tmp_path / "nested" / "state.json"
    state = load_state(path)
    state["last_daily"] = {"price": 80_123.45}
    save_state(state, path)
    assert load_state(path)["last_daily"]["price"] == 80_123.45
