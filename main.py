import asyncio
import os
import time
import random
from typing import Dict, Tuple, Optional, List

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# =========================================================
# 1. НАСТРОЙКА ПРИЛОЖЕНИЯ И ШАБЛОНОВ
# =========================================================
app = FastAPI(title="Crypto Arbitrage Scanner")

# Подключаем папку static (для CSS) и templates (для HTML)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def fmt_price(v):
    """Форматирует число красиво: без e-нотации, с разумным округлением."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if v == 0:
        return "0"
    av = abs(v)
    if av >= 1000:
        return f"{v:,.2f}"
    elif av >= 1:
        return f"{v:.4f}"
    elif av >= 0.01:
        return f"{v:.6f}"
    elif av >= 0.0001:
        return f"{v:.8f}"
    else:
        return f"{v:.10f}"


templates.env.filters["fmt"] = fmt_price

CONFIG_PATH = "config.yaml"

# Глобальный httpx-клиент для переиспользования соединений (экономит RAM)
GLOBAL_HTTP_CLIENT: httpx.AsyncClient = None

async def get_http_client() -> httpx.AsyncClient:
    global GLOBAL_HTTP_CLIENT
    if GLOBAL_HTTP_CLIENT is None:
        GLOBAL_HTTP_CLIENT = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return GLOBAL_HTTP_CLIENT



# =========================================================
# 2. ЗАГРУЗКА КОНФИГУРАЦИИ
# =========================================================
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

# Токен и chat_id берём из переменных окружения (для облака),
# а если их нет — из config.yaml (для локального запуска)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", config.get("telegram", {}).get("bot_token", ""))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", config.get("telegram", {}).get("chat_id", ""))


# =========================================================
# 3. МОДЕЛИ ДАННЫХ
# =========================================================
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


# =========================================================
# 4. ХРАНИЛИЩА ДАННЫХ
# =========================================================
# Хранилище котировок: { (exchange, pair) : StoredQuote }
quotes_store: Dict[Tuple[str, str], StoredQuote] = {}

# Хранилище текущих spreads (для UI)
spreads_store: Dict[str, Dict[str, float]] = {}

# Защита от спама в Telegram: { pair : last_alert_timestamp }
last_alert_time: Dict[str, float] = {}


# =========================================================
# 5. TELEGRAM-НОТИФИКАТОР
# =========================================================
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
                print("✅ Сообщение успешно отправлено в Telegram")
            except Exception as e:
                print("❌ Ошибка отправки в Telegram:", e)


# ВАЖНО: экземпляр создаётся ПОСЛЕ класса
telegram_notifier = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)


# =========================================================
# 6. КОМИССИИ БИРЖ И ССЫЛКИ НА ТОРГОВЛЮ
# =========================================================
EXCHANGE_FEES = {
    "binance": 0.001,  # 0.1%
    "bybit": 0.001,
    "okx": 0.001,
    "gateio": 0.001,
}


def get_trade_link(exchange: str, pair: str) -> str:
    """Генерирует ссылку на страницу торгов для конкретной пары и биржи."""
    if exchange == "binance":
        return f"https://www.binance.com/en/trade/{pair}"
    elif exchange == "bybit":
        return f"https://www.bybit.com/trade/spot/{pair}"
    elif exchange == "okx":
        okx_pair = pair.replace("USDT", "-USDT")
        return f"https://www.okx.com/trade-spot/{okx_pair.lower()}"
    elif exchange == "gateio":
        gate_pair = pair.replace("USDT", "_USDT")
        return f"https://www.gate.io/trade/{gate_pair}"
    return "#"


# =========================================================
# 7. МОК-ДАННЫЕ (для режима симуляции)
# =========================================================
def mock_quote(exchange: str, pair: str) -> Quote:
    base = 100.0 + (abs(hash(pair)) % 100)
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


# =========================================================
# 8. ЗАПРОСЫ К РЕАЛЬНЫМ БИРЖАМ
# =========================================================
async def fetch_from_exchange(exchange: str, pair: str) -> Optional[Quote]:
    if MOCK:
        return mock_quote(exchange, pair)

    try:
        client = await get_http_client()
        if True:
            # --- BINANCE USDT-M FUTURES ---
            if exchange == "binance":
                # Binance использует 1000-префикс для мелких монет
                binance_special = {
                    "SHIBUSDT": "1000SHIBUSDT", "PEPEUSDT": "1000PEPEUSDT",
                    "RNDRUSDT": "1000RNDRUSDT", "FLOKIUSDT": "1000FLOKIUSDT",
                    "BONKUSDT": "1000BONKUSDT", "LUNCUSDT": "1000LUNCUSDT",
                    "XECUSDT": "1000XECUSDT", "SATSUSDT": "1000SATSUSDT",
                    "RATSUSDT": "1000RATSUSDT", "CATUSDT": "1000CATUSDT",
                    "XUSDT": "1000XUSDT", "CHEEMSUSDT": "1000CHEEMSUSDT",
                }
                binance_pair = binance_special.get(pair, pair)
                url = f"https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={binance_pair}"
                r = await client.get(url, timeout=5)
                data = r.json()
                # Для 1000-prefix пар делим цену на 1000, чтобы привести к единой шкале
                divisor = 1000.0 if pair in binance_special else 1.0
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bidPrice"]) / divisor,
                    ask=float(data["askPrice"]) / divisor,
                    last=None, ts=int(time.time() * 1000),
                )

            # --- BYBIT USDT PERPETUAL (linear) ---
            elif exchange == "bybit":
                url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["result"]["list"][0]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bid1Price"]), ask=float(data["ask1Price"]),
                    last=float(data["lastPrice"]), ts=int(time.time() * 1000),
                )

            # --- OKX SWAP (USDT-margined) ---
            elif exchange == "okx":
                okx_pair = pair.replace("USDT", "-USDT-SWAP")
                url = f"https://www.okx.com/api/v5/market/ticker?instId={okx_pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["data"][0]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bidPx"]), ask=float(data["askPx"]),
                    last=float(data["last"]), ts=int(time.time() * 1000),
                )

            # --- GATE.IO USDT FUTURES ---
            elif exchange == "gateio":
                gate_pair = pair.replace("USDT", "_USDT")
                url = f"https://api.gateio.ws/api/v4/futures/usdt/tickers?contract={gate_pair}"
                r = await client.get(url, timeout=5)
                data = r.json()[0]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["highest_bid"]), ask=float(data["lowest_ask"]),
                    last=float(data["last"]), ts=int(time.time() * 1000),
                )

            # --- MEXC FUTURES ---
            elif exchange == "mexc":
                mexc_pair = pair.replace("USDT", "_USDT")
                url = f"https://api.mexc.com/api/v1/contract/ticker?symbol={mexc_pair}"
                r = await client.get(url, timeout=5)
                resp = r.json()
                if not resp.get("success") or not resp.get("data"):
                    return None
                data = resp["data"]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bid1"]), ask=float(data["ask1"]),
                    last=float(data.get("lastPrice") or 0) or None,
                    ts=int(time.time() * 1000),
                )

            # --- HTX (HUOBI) LINEAR SWAP ---
            elif exchange == "htx":
                htx_pair = pair.replace("USDT", "-USDT")
                url = f"https://api.hbdm.com/linear-swap-ex/market/detail/merged?contract_code={htx_pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["tick"]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bid"][0]), ask=float(data["ask"][0]),
                    last=float(data["close"]), ts=int(time.time() * 1000),
                )
            elif exchange == "bingx":
                bingx_pair = pair.replace("USDT", "-USDT")
                url = f"https://open-api.bingx.com/openApi/swap/v2/quote/ticker?symbol={bingx_pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["data"]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bidPrice"]), ask=float(data["askPrice"]),
                    last=float(data.get("lastPrice") or 0) or None,
                    ts=int(time.time() * 1000),
                )

            elif exchange == "bitget":
                url = f"https://api.bitget.com/api/v2/mix/market/ticker?productType=USDT-FUTURES&symbol={pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["data"][0]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bidPr"]), ask=float(data["askPr"]),
                    last=float(data["lastPr"]), ts=int(time.time() * 1000),
                )

            elif exchange == "coinex":
                url = f"https://api.coinex.com/v2/futures/ticker?market={pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["data"][0]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bid"]), ask=float(data["ask"]),
                    last=float(data["last"]), ts=int(time.time() * 1000),
                )

            elif exchange == "phemex":
                url = f"https://api.phemex.com/md/v1/ticker/24hr?symbol={pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["result"]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bidRp"]), ask=float(data["askRp"]),
                    last=float(data["lastEp"]), ts=int(time.time() * 1000),
                )

            elif exchange == "bitrue":
                url = f"https://fapi.bitrue.com/fapi/v1/ticker/bookTicker?symbol={pair}"
                r = await client.get(url, timeout=5)
                data = r.json()
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bidPrice"]), ask=float(data["askPrice"]),
                    last=None, ts=int(time.time() * 1000),
                )

            elif exchange == "toobit":
                toobit_pair = pair.replace("USDT", "-SWAP-USDT")
                url = f"https://api.toobit.com/api/v1/futures/market/ticker?symbol={toobit_pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["data"]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bidPrice"]), ask=float(data["askPrice"]),
                    last=float(data["lastPrice"]), ts=int(time.time() * 1000),
                )

            elif exchange == "deribit":
                if pair == "BTCUSDT":
                    instrument = "BTC-PERPETUAL"
                elif pair == "ETHUSDT":
                    instrument = "ETH-PERPETUAL"
                else:
                    return None
                payload = {"jsonrpc": "2.0", "id": 1, "method": "public/ticker", "params": {"instrument_name": instrument}}
                r = await client.post("https://www.deribit.com/api/v2/public/ticker", json=payload, timeout=5)
                data = r.json()["result"]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["best_bid_price"]), ask=float(data["best_ask_price"]),
                    last=float(data["last_price"]), ts=int(time.time() * 1000),
                )

            elif exchange == "coinw":
                coinw_pair = pair.replace("USDT", "_USDT")
                url = f"https://api.coinw.com/v1/perpumPublic/tickers?symbol={coinw_pair}"
                r = await client.get(url, timeout=5)
                data = r.json()["data"][0]
                return Quote(
                    exchange=exchange, pair=pair,
                    bid=float(data["bid"]), ask=float(data["ask"]),
                    last=float(data["last"]), ts=int(time.time() * 1000),
                )

            else:
                return None

    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        # Пара не торгуется на этой бирже — молча пропускаем
        return None
    except Exception as e:
        print(f"Ошибка {pair} на {exchange}: {e}")
        return None


# =========================================================
# 9. ЛОГИКА АРБИТРАЖА И АЛЕРТОВ
# =========================================================
def compute_spreads_and_alerts(current_quotes: Dict[Tuple[str, str], StoredQuote]):
    """
    Находит максимальный bid и минимальный ask по каждой паре.
    Отсеивает выбросы (разные номиналы контрактов на разных биржах).
    """
    import statistics
    global spreads_store
    current_time = time.time()

    for pair in PAIRS:
        # Собираем все цены
        prices_data = []  # [(bid, ask, exchange), ...]
        for ex in EXCHANGES:
            key = (ex, pair)
            if key in current_quotes:
                q = current_quotes[key]
                if q.bid > 0 and q.ask > 0:
                    prices_data.append((q.bid, q.ask, ex))

        if len(prices_data) < 2:
            continue

        # Медиана как "эталонная" цена
        bids_list = [p[0] for p in prices_data]
        asks_list = [p[1] for p in prices_data]
        median_bid = statistics.median(bids_list)
        median_ask = statistics.median(asks_list)

        # Отсеиваем выбросы — если цена отличается от медианы больше чем в 3 раза,
        # значит это другой номинал контракта (1000x и т.п.), пропускаем
        filtered = []
        for bid, ask, ex in prices_data:
            # Отклонение bid от медианы
            bid_ratio = bid / median_bid if median_bid > 0 else 1
            ask_ratio = ask / median_ask if median_ask > 0 else 1

            if bid_ratio > 3 or bid_ratio < 0.33:
                continue
            if ask_ratio > 3 or ask_ratio < 0.33:
                continue

            filtered.append((bid, ask, ex))

        if len(filtered) < 2:
            continue

        # Находим max bid и min ask среди "нормальных" цен
        max_bid, _, max_bid_exchange = max(filtered, key=lambda x: x[0])
        min_ask, _, min_ask_exchange = min(filtered, key=lambda x: x[1])

        if max_bid <= 0 or min_ask <= 0:
            continue

        mid_price = (max_bid + min_ask) / 2.0
        spread_usd = max_bid - min_ask
        spread_pct = (spread_usd / mid_price) * 100.0 if mid_price != 0 else 0.0

        # Расчёт чистой прибыли на 1000 USDT
        trade_amount_usdt = 1000.0
        buy_fee = EXCHANGE_FEES.get(min_ask_exchange, 0.001)
        asset_quantity = (trade_amount_usdt / min_ask) * (1 - buy_fee)
        sell_fee = EXCHANGE_FEES.get(max_bid_exchange, 0.001)
        sell_revenue = asset_quantity * max_bid * (1 - sell_fee)
        net_profit = sell_revenue - trade_amount_usdt

        spreads_store[pair] = {
            "spread_usd": spread_usd,
            "spread_pct": round(spread_pct, 4),
            "max_bid": max_bid,
            "max_bid_exchange": max_bid_exchange,
            "min_ask": min_ask,
            "min_ask_exchange": min_ask_exchange,
            "net_profit_usdt": round(net_profit, 4),
        }

        # Кулдаун на 5 минут на пару
        if pair in last_alert_time and (current_time - last_alert_time[pair]) < 300:
            continue

        if spread_pct >= ALERT_PCT and net_profit > 0:
            buy_link = get_trade_link(min_ask_exchange, pair)
            sell_link = get_trade_link(max_bid_exchange, pair)

            text = (
                f"🚀 <b>АРБИТРАЖНАЯ ВОЗМОЖНОСТЬ!</b> 🚀\n\n"
                f"💰 Пара: <b>{pair}</b>\n\n"
                f"🟢 <b>КУПИТЬ</b> на <a href='{buy_link}'><b>{min_ask_exchange.upper()}</b></a> "
                f"по цене <b>{min_ask:.6f}</b>\n"
                f"🔴 <b>ПРОДАТЬ</b> на <a href='{sell_link}'><b>{max_bid_exchange.upper()}</b></a> "
                f"по цене <b>{max_bid:.6f}</b>\n\n"
                f"📊 <b>Спред:</b> {spread_usd:.4f} USDT ({spread_pct:.2f}%)\n"
                f"💵 <b>Чистая прибыль (на 1000 USDT):</b> ~<b>{net_profit:.2f} USDT</b>\n"
                f"⏰ Время: {time.strftime('%H:%M:%S')}"
            )
            asyncio.create_task(telegram_notifier.send(text))
            last_alert_time[pair] = current_time
            print(f"📨 Отправлено уведомление в Telegram по паре {pair}")



# =========================================================
# 10. API-ЭНДПОИНТЫ
# =========================================================
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    # Перестраиваем данные в удобный для шаблона вид
    quotes_by_pair = {}
    for (ex, pair), q in quotes_store.items():
        quotes_by_pair.setdefault(pair, []).append({
            "exchange": ex,
            "bid": q.bid,
            "ask": q.ask,
            "last": q.last,
            "ts": q.ts,
        })

    # Сортируем по спреду в ПРОЦЕНТАХ (от большего к меньшему)
    def sort_key(item):
        data = item[1]
        return (data.get("spread_pct", 0), data.get("spread_usd", 0))

    sorted_items = sorted(spreads_store.items(), key=sort_key, reverse=True)

    # Топ-10: только ПРИБЫЛЬНЫЕ пары (net_profit > 0)
    profitable = [(k, v) for k, v in sorted_items if v.get("net_profit_usdt", 0) > 0]
    top_10 = dict(profitable[:10])
    top_10_pairs = set(top_10.keys())

    # Остальные: всё, что не попало в топ-10
    rest = dict((k, v) for k, v in sorted_items if k not in top_10_pairs)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "quotes": quotes_by_pair,
            "top_spreads": top_10,
            "spreads": rest,
            "pairs_count": len(PAIRS),
            "exchanges_count": len(EXCHANGES),
        }
    )


@app.get("/quotes")
def api_quotes():
    """Возвращает сырые котировки по всем парам и биржам (JSON)."""
    payload = {}
    for (ex, pair), q in quotes_store.items():
        payload.setdefault(pair, []).append({
            "exchange": ex,
            "bid": q.bid,
            "ask": q.ask,
            "last": q.last,
            "ts": q.ts,
        })
    return payload


@app.get("/spreads")
def api_spreads():
    """Возвращает текущие спреды по парам (JSON)."""
    return spreads_store


# =========================================================
# 11. ФОНОВЫЙ СБОР ДАННЫХ
# =========================================================

async def fetch_with_retry(exchange: str, pair: str, retries: int = 2) -> Optional[Quote]:
    """Повторяет запрос при сетевых сбоях."""
    for attempt in range(retries):
        try:
            q = await fetch_from_exchange(exchange, pair)
            return q
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError):
            if attempt < retries - 1:
                await asyncio.sleep(1)
                continue
            return None
        except Exception:
            return None
    return None


async def fetch_and_store(exchange: str, pair: str):
    q = await fetch_with_retry(exchange, pair)
    if q:
        key = (exchange, pair)
        quotes_store[key] = StoredQuote(bid=q.bid, ask=q.ask, last=q.last, ts=q.ts)


async def data_collector():
    # Ограничиваем параллелизм: не более 10 одновременных запросов
    semaphore = asyncio.Semaphore(10)

    async def fetch_limited(exchange, pair):
        async with semaphore:
            return await fetch_and_store(exchange, pair)

    while True:
        tasks = []
        for pair in PAIRS:
            for ex in EXCHANGES:
                tasks.append(fetch_limited(ex, pair))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            compute_spreads_and_alerts(quotes_store)
        await asyncio.sleep(FETCH_INTERVAL)


# =========================================================
# 12. ТОЧКА ВХОДА
# =========================================================
@app.on_event("startup")
async def startup_event():
    print(">>> Запуск фонового сборщика данных...")
    await get_http_client()  # создаём клиент заранее
    asyncio.create_task(data_collector())


@app.on_event("shutdown")
async def shutdown_event():
    global GLOBAL_HTTP_CLIENT
    if GLOBAL_HTTP_CLIENT is not None:
        await GLOBAL_HTTP_CLIENT.aclose()
        GLOBAL_HTTP_CLIENT = None
        print(">>> HTTP-клиент закрыт")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
