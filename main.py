import asyncio
import time
import random
from typing import Dict, Tuple, Optional, List
import httpx
import yaml
from fastapi import FastAPI

app = FastAPI(title="Crypto Arbitrage Scanner")

from pydantic import BaseModel

CONFIG_PATH = "config.yaml"

# ---------------------------
# Загрузка конфигурации
# ---------------------------

def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)

config = load_config()

MOCK = config.get("mock_data", True)
FETCH_INTERVAL = int(config.get("fetch_interval", 5))
PAIRS: List[str] = config.get("pairs", [])
EXCHANGES: List[str] = config.get("exchanges", [])
ALERT_USD = float(config.get("alerts", {}).get("spread_usd", 0.5))
ALERT_PCT = float(config.get("alerts", {}).get("spread_pct", 0.5))

TELEGRAM_TOKEN = config.get("telegram", {}).get("bot_token", "")
TELEGRAM_CHAT_ID = config.get("telegram", {}).get("chat_id", "")

# ---------------------------
# Модель данных
# ---------------------------

# Создаем простой маршрут (endpoint) для проверки
@app.get("/")
def read_root():
    return {
        "status": "Бот работает!",
        "config_loaded": config
    }


class Quote(BaseModel):
    exchange: str
    pair: str
    bid: float
    ask: float
    last: Optional[float] = None
    ts: int

class StoredQuote(BaseModel):
    bid: float
    ask: float
    last: Optional[float]
    ts: int

# Хранилище котировок: { (exchange, pair) : StoredQuote }
quotes_store: Dict[Tuple[str, str], StoredQuote] = {}

# Хранилище текущих spreads (для UI)
spreads_store: Dict[str, Dict[str, float]] = {}  # pair -> { 'spread_usd': ..., 'spread_pct': ... }

# ---------------------------
# Нотификатор Telegram
# ---------------------------

class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"

    async def send(self, text: str):
        # Проверяем, заполнены ли реальные данные
        if not self.token or not self.chat_id or "YOUR_" in self.token:
            print("⚠️ Telegram не настроен, пропускаем уведомление.")
            return
            
        data = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.url, json=data, timeout=5)
                response.raise_for_status()
            except Exception as e:
                print("❌ Ошибка отправки в Telegram:", e)

telegram_notifier = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

# ---------------------------
# Адаптеры для бирж (моки по умолчанию)
# ---------------------------

def mock_quote(exchange: str, pair: str) -> Quote:
    # Детерминированная генерация на основе пары и случайности
    base = 100.0 + (abs(hash(pair)) % 100)  # пример базовой цены
    jitter = random.uniform(-0.8, 0.8)
    price = max(0.01, base * (1 + jitter * 0.005))
    spread = random.uniform(0.05, 0.25)

    bid = round(price - spread, 4)
    ask = round(price + spread, 4)
    return Quote(
        exchange=exchange,
        pair=pair,
        bid=round(bid, 4),
        ask=round(ask, 4),
        last=round(price, 4),
        ts=int(time.time() * 1000),
    )

async def fetch_from_exchange(exchange: str, pair: str) -> Optional[Quote]:
    if MOCK:
        # мок: один источник данных всегда возвращает разумную цену
        return mock_quote(exchange, pair)

    # Реальный режим: разные эндпойнты под каждую биржу.
    try:
        async with httpx.AsyncClient() as client:
            if exchange == "binance":
                url = f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={pair}"
                r = await client.get(url, timeout=5)
                data = r.json()
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bidPrice"]), ask=float(data["askPrice"]),
                    last=None, ts=int(time.time() * 1000),
                )

            elif exchange == "bybit":
                # Bybit использует формат BTCUSDT
                url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["result"]["list"][0]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bid1Price"]), ask=float(data["ask1Price"]),
                    last=float(data["lastPrice"]), ts=int(time.time() * 1000),
                )

            elif exchange == "okx":
                # OKX требует дефис: BTC-USDT
                okx_pair = pair.replace("USDT", "-USDT")
                url = f"https://www.okx.com/api/v5/market/ticker?instId={okx_pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["data"][0]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bidPx"]), ask=float(data["askPx"]),
                    last=float(data["last"]), ts=int(time.time() * 1000),
                )

            elif exchange == "gateio":
                # Gate.io требует нижнее подчеркивание: BTC_USDT
                gate_pair = pair.replace("USDT", "_USDT")
                url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={gate_pair}"
                r = await client.get(url, timeout=5)
                data = r.json()[0]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["highest_bid"]), ask=float(data["lowest_ask"]),
                    last=float(data["last"]), ts=int(time.time() * 1000),
                )

            # Если биржа не реализована, возвращаем None
            else:
                return None
                
    except Exception as e:
        print(f"Ошибка при запросе {pair} на {exchange}: {e}")
        return None

# ---------------------------
# Логика арбитража и алертов
# ---------------------------

# Глобальный словарь для защиты от спама (запоминает время последнего алерта по каждой паре)
last_alert_time = {}

def compute_spreads_and_alerts(current_quotes: Dict[Tuple[str, str], StoredQuote]):
    """
    Для каждого pair'а находим max_bid и min_ask по всем доступным exchanges.
    """
    global spreads_store
    current_time = time.time()
    
    for pair in PAIRS:
        bids = []
        asks = []
        for ex in EXCHANGES:
            key = (ex, pair)
            if key in current_quotes:
                q = current_quotes[key]
                bids.append((q.bid, ex)) # Сохраняем цену и биржу
                asks.append((q.ask, ex)) # Сохраняем цену и биржу
                
        if not bids or not asks:
            continue
            
        # Находим максимальный bid и минимальный ask вместе с биржами
        max_bid_data = max(bids, key=lambda x: x[0])
        min_ask_data = min(asks, key=lambda x: x[0])
        
        max_bid, max_bid_exchange = max_bid_data
        min_ask, min_ask_exchange = min_ask_data
        
        if max_bid <= 0 or min_ask <= 0:
            continue
            
        mid_price = (max_bid + min_ask) / 2.0
        spread_usd = max_bid - min_ask
        spread_pct = (spread_usd / mid_price) * 100.0 if mid_price != 0 else 0.0

        spreads_store[pair] = {
            "spread_usd": round(spread_usd, 6),
            "spread_pct": round(spread_pct, 4),
            "max_bid": round(max_bid, 6),
            "max_bid_exchange": max_bid_exchange, # Добавили в JSON, где продавать
            "min_ask": round(min_ask, 6),
            "min_ask_exchange": min_ask_exchange, # Добавили в JSON, где покупать
        }

        # Проверяем, не спамим ли мы (кулдаун 5 минут = 300 секунд)
        if pair in last_alert_time and (current_time - last_alert_time[pair]) < 300:
            continue

        # Алёрты
        if spread_usd >= ALERT_USD and spread_pct >= ALERT_PCT:
            text = (
                f"🚀 <b>АРБИТРАЖНАЯ ВОЗМОЖНОСТЬ!</b> 🚀\n\n"
                f"💰 Пара: <b>{pair}</b>\n"
                f"📉 Купить на: <b>{min_ask_exchange.upper()}</b> по цене <b>{min_ask:.6f}</b>\n"
                f"📈 Продать на: <b>{max_bid_exchange.upper()}</b> по цене <b>{max_bid:.6f}</b>\n\n"
                f"📊 Спред: <b>{spread_usd:.4f} USDT</b> ({spread_pct:.2f}%)\n"
                f"⏰ Время: {time.strftime('%H:%M:%S')}"
            )
            asyncio.create_task(telegram_notifier.send(text))
            
            # Запоминаем время отправки, чтобы не спамить
            last_alert_time[pair] = current_time
            print(f"📨 Отправлено уведомление в Telegram по паре {pair}")

# ---------------------------
# Функции сервиса и API
# ---------------------------

@app.get("/quotes")
def api_quotes():
    """
    Возвращает текущие котировки по всем парам и биржам.
    """
    payload = {}
    for (ex, pair), q in quotes_store.items():
        payload.setdefault(pair, []).append({
            "exchange": ex,
            "bid": q.bid,
            "ask": q.ask,
            "last": q.last,
            "ts": q.ts
        })
    return payload

@app.get("/spreads")
def api_spreads():
    """
    Возвращает текущие спреды по парам.
    """
    return spreads_store

# ---------------------------
# Фоновый цикл сбора данных
# ---------------------------

async def data_collector():
    # подготовить адаптеры (в MVP они моковые)
    while True:
        tasks = []
        # Собираем котировки по всем парам и биржам
        for pair in PAIRS:
            for ex in EXCHANGES:
                tasks.append(fetch_and_store(ex, pair))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # Обновим спреды после сбора
            compute_spreads_and_alerts(quotes_store)
        await asyncio.sleep(FETCH_INTERVAL)

async def fetch_and_store(exchange: str, pair: str):
    q = await fetch_from_exchange(exchange, pair)
    if q:
        key = (exchange, pair)
        quotes_store[key] = StoredQuote(bid=q.bid, ask=q.ask, last=q.last, ts=q.ts)

# ---------------------------
# Точка входа
# ---------------------------

@app.on_event("startup")
async def startup_event():
    # Эта функция запустится автоматически при старте сервера
    print(">>> Запуск фонового сборщика данных...")
    asyncio.create_task(data_collector())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)