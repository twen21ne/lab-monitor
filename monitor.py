#!/usr/bin/env python3
"""
LAB Monitor — простой скрипт мониторинга признаков пампа/дампа на LABUSDT.

Данные берутся через CoinGecko Derivatives API (агрегатор, не блокируется по стране в отличие
от прямых обращений к Binance/Bybit с облачных IP — см. комментарий ниже).
Состояние (история последних измерений) хранится в state.json и коммитится обратно в репозиторий
GitHub Actions'ом, чтобы скрипт "помнил" предыдущие точки между запусками.

Метрики:
1. Funding rate — текущая ставка. Порог: > 0.1% по модулю.
2. Open Interest (в USD) — изменение за 1 час и за 24 часа. Пороги: >15% за 1ч ИЛИ >25% за 24ч.
3. "Каскад ликвидаций" (приближение) — резкое падение/рост цены > 5% ЗА ОДИН ИНТЕРВАЛ ОПРОСА (15 мин)
   вместе с падением OI > 5% в это же окно. Точных данных по объёму ликвидаций бесплатно и по расписанию
   получить нельзя, поэтому это осознанное упрощение.

Алерт "внимание" отправляется, если сработали 2 из 3 метрик (funding, OI, liq-proxy) в одном и том же запуске.
Алерт про funding отдельно тоже логируется, но не спамит, если сработал только один раз.
"""

import json
import os
import time
import requests

SYMBOL = "LABUSDT"
EXCHANGE_ID = "binance_futures"  # CoinGecko id для площадки, с которой берём тикер
BASE_URL = "https://api.coingecko.com/api/v3"
STATE_FILE = "state.json"

# История: сначала использовался Binance напрямую (fapi.binance.com) -> HTTP 451
# "restricted location". Потом Bybit напрямую (api.bybit.com) -> тоже заблокировал
# по стране через CloudFront. Обе биржи ограничивают доступ с IP дата-центров США
# (там физически расположены раннеры GitHub Actions).
# Решение: CoinGecko — это агрегатор, который сам стягивает данные с бирж на своей
# стороне и отдаёт нам через свой API. Нас как клиента CoinGecko биржи не видят
# напрямую, поэтому гео-блокировки такого рода не применяются.
# Требуется бесплатный CoinGecko Demo API key (регистрация без карты):
# https://www.coingecko.com/en/api/pricing -> "Demo Plan"

COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY")

# ---- Пороги (настраиваются здесь) ----
FUNDING_RATE_THRESHOLD = 0.0000001        # 0.1% за период расчёта funding rate
OI_CHANGE_1H_THRESHOLD = 0.0000001         # +15% за 1 час
OI_CHANGE_24H_THRESHOLD = 0.0000001        # +25% за 24 часа
PRICE_MOVE_INTERVAL_THRESHOLD = 0.05  # 5% за один запуск (15 мин) — компонент liq-proxy
OI_DROP_INTERVAL_THRESHOLD = 0.05     # 5% падение OI за один запуск — компонент liq-proxy

MAX_HISTORY_POINTS = 100  # ~25 часов при интервале 15 минут

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def fetch_json(url, params=None, headers=None):
    """GET-запрос с понятной диагностикой: если биржа/API вернула не то, что ожидалось,
    выводим статус-код и сырое тело ответа в лог, а не падаем с невнятным KeyError."""
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    print(f"[DEBUG] GET {url} params={params} -> HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        print(f"[ERROR] Ответ не в формате JSON. Сырое тело ответа:\n{resp.text[:1000]}")
        raise
    if resp.status_code != 200:
        print(f"[ERROR] Биржа вернула ошибку вместо данных: {data}")
    return data


def fetch_current_data():
    """Забираем funding rate, цену и open interest через CoinGecko derivatives API."""
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    if not COINGECKO_API_KEY:
        print("[WARN] COINGECKO_API_KEY не задан — запрос, скорее всего, будет отклонён (rate limit/403).")

    data = fetch_json(
        f"{BASE_URL}/derivatives/exchanges/{EXCHANGE_ID}",
        params={"include_tickers": "unexpired"},
        headers=headers,
    )

    tickers = data.get("tickers", [])
    if not tickers:
        raise RuntimeError(f"CoinGecko вернул пустой список тикеров для {EXCHANGE_ID}. Полный ответ: {data}")

    match = next((t for t in tickers if t.get("symbol") == SYMBOL), None)
    if not match:
        raise RuntimeError(
            f"Тикер {SYMBOL} не найден среди {len(tickers)} тикеров биржи {EXCHANGE_ID}. "
            f"Возможно, поменялось название пары или контракт делистнули."
        )

    return {
        "timestamp": int(time.time()),
        "price": float(match["last"]),
        "funding_rate": float(match.get("funding_rate") or 0) / 100,  # CoinGecko отдаёт в %, приводим к доле
        "open_interest": float(match.get("open_interest_usd") or 0),
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
