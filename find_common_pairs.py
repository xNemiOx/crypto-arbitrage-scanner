import asyncio
import httpx
import json
from collections import Counter

async def fetch_binance(c):
    r = await c.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
    return {s["symbol"] for s in r.json()["symbols"] if s["contractType"]=="PERPETUAL" and s["quoteAsset"]=="USDT"}

async def fetch_bybit(c):
    r = await c.get("https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000")
    return {s["symbol"] for s in r.json()["result"]["list"] if s["quoteCoin"]=="USDT"}

async def fetch_okx(c):
    r = await c.get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    return {s["instId"].replace("-USDT-SWAP","USDT") for s in r.json()["data"] if s["settleCcy"]=="USDT"}

async def fetch_gateio(c):
    r = await c.get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    return {s["name"].replace("_USDT","USDT") for s in r.json()}

async def fetch_mexc(c):
    r = await c.get("https://contract.mexc.com/api/v1/contract/detail")
    return {s["symbol"].replace("_USDT","USDT") for s in r.json()["data"] if "USDT" in s["symbol"]}

async def fetch_htx(c):
    r = await c.get("https://api.hbdm.com/linear-swap-api/v1/swap_contract_info")
    return {s["contract_code"].replace("-USDT","USDT") for s in r.json()["data"]}

async def fetch_bingx(c):
    r = await c.get("https://open-api.bingx.com/openApi/swap/v2/quote/contracts")
    return {s["symbol"].replace("-USDT","USDT") for s in r.json()["data"]}

async def fetch_bitget(c):
    r = await c.get("https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES")
    return {s["symbol"] for s in r.json()["data"]}

async def fetch_deribit(c):
    pairs = set()
    for cur in ["BTC","ETH"]:
        r = await c.get(f"https://www.deribit.com/api/v2/public/get_instruments?currency={cur}&kind=future")
        for s in r.json()["result"]:
            if s["instrument_name"].endswith("PERPETUAL"):
                pairs.add(f"{cur}USDT")
    return pairs

async def main():
    async with httpx.AsyncClient(headers={"User-Agent":"Mozilla/5.0"}, timeout=15) as c:
        tasks = [fetch_binance(c), fetch_bybit(c), fetch_okx(c), fetch_gateio(c),
                 fetch_mexc(c), fetch_htx(c), fetch_bingx(c), fetch_bitget(c), fetch_deribit(c)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    names = ["binance","bybit","okx","gateio","mexc","htx","bingx","bitget","deribit"]
    exchange_pairs = {}
    print("📊 Список пар по биржам:")
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            print(f"  ❌ {name}: ошибка — {res}")
            exchange_pairs[name] = set()
        else:
            print(f"  ✅ {name}: {len(res)} пар")
            exchange_pairs[name] = res

    counter = Counter()
    for pairs in exchange_pairs.values():
        for p in pairs:
            counter[p] += 1

    for n in [9, 8, 7, 6]:
        pairs_n = sorted([p for p, c in counter.items() if c == n])
        print(f"\n🔸 Пар на {n} биржах: {len(pairs_n)}")
        if n >= 8:
            print("   " + ", ".join(pairs_n[:30]) + (" ..." if len(pairs_n) > 30 else ""))

    report = {
        "summary": {n: len([p for p, c in counter.items() if c == n]) for n in range(1, 10)},
        "pairs_by_count": {str(n): sorted([p for p, c in counter.items() if c == n]) for n in range(1, 10)}
    }
    with open("pairs_report.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print("\n💾 Полный отчёт сохранён в pairs_report.json")

    all_9 = sorted([p for p, c in counter.items() if c == 9])
    if not all_9:
        print("\n⚠️ Нет пар на всех 9 биржах — config.yaml НЕ обновлён")
    else:
        with open("config.yaml", "r") as f:
            content = f.read()
        pairs_yaml = "pairs:\n" + "\n".join(f"  - {p}" for p in all_9)
        start = content.find("pairs:")
        end = content.find("\n\n", start)
        if end == -1:
            end = content.find("\nexchanges:", start)
        content = content[:start] + pairs_yaml + "\n" + content[end:]
        with open("config.yaml", "w") as f:
            f.write(content)
        print(f"\n✅ config.yaml обновлён — {len(all_9)} пар на всех 9 биржах:")
        for p in all_9:
            print(f"   • {p}")

asyncio.run(main())
