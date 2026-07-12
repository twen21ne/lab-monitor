#!/usr/bin/env python3
"""
LAB Monitor — простой скрипт мониторинга признаков пампа/дампа на LABUSDT (Binance Futures).

Использует только бесплатные публичные эндпоинты Binance (без API-ключа).
Состояние (история последних измерений) хранится в state.json и коммитится обратно в репозиторий
GitHub Actions'ом, чтобы скрипт "помнил" предыдущие точки между запусками.

Метрики:
1. Funding rate — текущая ставка (premiumIndex). Порог: > 0.1% (0.001) по модулю.
2. Open Interest — изменение за 1 час и за 24 часа. Пороги: >15% за 1ч ИЛИ >25% за 24ч.
3. "Каскад ликвидаций" (приближение) — резкое падение/рост цены > 5% ЗА ОДИН ИНТЕРВАЛ ОПРОСА (15 мин)
   вместе с падением OI > 5% в это же окно. Точных данных по объёму ликвидаций бесплатно и по расписанию
   получить нельзя (публичный REST-эндпоинт Binance для этого закрыт), поэтому это осознанное упрощение.

Алерт "внимание" отправляется, если сработали 2 из 3 метрик (funding, OI, liq-proxy) в одном и том же запуске.
Алерт про funding отдельно тоже логируется, но не спамит, если сработал только один раз.
"""

import json
import os
import time
import requests

SYMBOL = "LABUSDT"
BASE_URL = "https://fapi.binance.com"
STATE_FILE = "state.json"

# ---- Пороги (настраиваются здесь) ----
FUNDING_RATE_THRESHOLD = 0.001        # 0.1% за период расчёта funding rate
OI_CHANGE_1H_THRESHOLD = 0.15         # +15% за 1 час
OI_CHANGE_24H_THRESHOLD = 0.25        # +25% за 24 часа
PRICE_MOVE_INTERVAL_THRESHOLD = 0.05  # 5% за один запуск (15 мин) — компонент liq-proxy
OI_DROP_INTERVAL_THRESHOLD = 0.05     # 5% падение OI за один запуск — компонент liq-proxy

MAX_HISTORY_POINTS = 100  # ~25 часов при интервале 15 минут

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def fetch_current_data():
    """Забираем funding rate, цену и open interest с публичного Binance Futures API."""
    premium = requests.get(f"{BASE_URL}/fapi/v1/premiumIndex", params={"symbol": SYMBOL}, timeout=10).json()
    oi = requests.get(f"{BASE_URL}/fapi/v1/openInterest", params={"symbol": SYMBOL}, timeout=10).json()

    return {
        "timestamp": int(time.time()),
        "price": float(premium["markPrice"]),
        "funding_rate": float(premium["lastFundingRate"]),
        "open_interest": float(oi["openInterest"]),
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"history": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def find_point_before(history, seconds_ago):
    """Находим ближайшую по времени точку из истории, которая была >= seconds_ago назад."""
    now = history[-1]["timestamp"] if history else int(time.time())
    target = now - seconds_ago
    candidates = [p for p in history if p["timestamp"] <= target]
    if not candidates:
        return None
    return candidates[-1]  # ближайшая к target, но не позже неё


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — алерт не отправлен, только вывод в лог:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    if resp.status_code != 200:
        print(f"[ERROR] Telegram API вернул {resp.status_code}: {resp.text}")


def main():
    state = load_state()
    history = state.get("history", [])

    current = fetch_current_data()
    history.append(current)
    history = history[-MAX_HISTORY_POINTS:]

    signals = []
    details = []

    # --- 1. Funding rate ---
    fr = current["funding_rate"]
    if abs(fr) >= FUNDING_RATE_THRESHOLD:
        signals.append("funding")
        details.append(f"Funding rate: {fr*100:.4f}% (порог {FUNDING_RATE_THRESHOLD*100:.2f}%)")

    # --- 2. Open Interest change ---
    point_1h = find_point_before(history, 3600)
    point_24h = find_point_before(history, 86400)

    if point_1h and point_1h["open_interest"] > 0:
        change_1h = (current["open_interest"] - point_1h["open_interest"]) / point_1h["open_interest"]
        if change_1h >= OI_CHANGE_1H_THRESHOLD:
            signals.append("oi")
            details.append(f"OI за 1ч: {change_1h*100:+.1f}% (порог +{OI_CHANGE_1H_THRESHOLD*100:.0f}%)")

    if point_24h and point_24h["open_interest"] > 0:
        change_24h = (current["open_interest"] - point_24h["open_interest"]) / point_24h["open_interest"]
        if change_24h >= OI_CHANGE_24H_THRESHOLD:
            if "oi" not in signals:
                signals.append("oi")
            details.append(f"OI за 24ч: {change_24h*100:+.1f}% (порог +{OI_CHANGE_24H_THRESHOLD*100:.0f}%)")

    # --- 3. Liquidation proxy (резкое движение цены + падение OI за один интервал опроса) ---
    if len(history) >= 2:
        prev = history[-2]
        price_change = (current["price"] - prev["price"]) / prev["price"] if prev["price"] > 0 else 0
        oi_change_interval = (
            (current["open_interest"] - prev["open_interest"]) / prev["open_interest"]
            if prev["open_interest"] > 0 else 0
        )
        if abs(price_change) >= PRICE_MOVE_INTERVAL_THRESHOLD and oi_change_interval <= -OI_DROP_INTERVAL_THRESHOLD:
            signals.append("liq_proxy")
            details.append(
                f"Резкое движение за интервал: цена {price_change*100:+.1f}%, "
                f"OI {oi_change_interval*100:+.1f}% — похоже на каскад ликвидаций"
            )

    unique_signals = set(signals)

    print(f"[{current['timestamp']}] price={current['price']} funding={fr} oi={current['open_interest']}")
    print(f"Сработавшие сигналы: {unique_signals}")

    # Алерт "внимание": 2 из 3 метрик сработали одновременно
    if len(unique_signals) >= 2:
        message = (
            f"⚠️ LAB: сработало {len(unique_signals)} сигнала одновременно\n\n"
            + "\n".join(details)
            + f"\n\nЦена: ${current['price']:.4f}"
        )
        send_telegram_message(message)

    # Резкий каскад ликвидаций — отдельный немедленный алерт даже если он один
    if "liq_proxy" in unique_signals:
        liq_detail = [d for d in details if "каскад" in d]
        if liq_detail:
            send_telegram_message(f"🚨 LAB: возможный каскад ликвидаций\n\n{liq_detail[0]}\n\nЦена: ${current['price']:.4f}")

    state["history"] = history
    save_state(state)


if __name__ == "__main__":
    main()
