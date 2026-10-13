"""Прогон инструментов WB и браузера настоящими данными, без модели.
Запуск: cd /opt/assistant && set -a && . ./.env && set +a && PYTHONPATH=. venv/bin/python scripts/smoke.py"""
import asyncio
import os
import sys
import time

import httpx

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/assistant/browsers")  # где служба хранит Chromium

from app import wb_agent  # noqa: E402
from app.browser import Browser, BrowserError  # noqa: E402
from app.wb import WB  # noqa: E402


def short(text, n=500):
    text = str(text)
    return "    " + (text[:n] + (" …" if len(text) > n else "")).replace("\n", "\n    ")


async def step(name, coro):
    t = time.time()
    try:
        out = await coro
        print(f"✅ {name} ({time.time() - t:.1f} с)\n{short(out)}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ {name}: {type(e).__name__}: {e}")


async def main():
    if "browser" not in sys.argv:
        print("=== WB (инструменты агента, живые данные) ===")
        async with httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0)) as client:
            tools = {t.name: t.fn for t in wb_agent.build(WB(client, os.environ["WB_TOKEN"])).tools}
            await step("stocks (остаток ≤3, 5 строк)", tools["stocks"](max_qty=3, top=5))
            await step("sales (7 дней)", tools["sales"](days=7))
            await step("orders (7 дней)", tools["orders"](days=7))
            await step("prices (3 строки)", tools["prices"](top=3))
            await step("cards (3 строки)", tools["cards"](top=3))
            await step("feedbacks (2 без ответа)", tools["feedbacks"](top=2))

    print("\n=== Браузер ===")
    b = Browser()
    try:
        await step("open example.com", b.open("https://example.com"))
        await step("elements", b.elements())
        for bad in ("http://127.0.0.1/", "http://169.254.169.254/", "file:///etc/passwd"):
            try:
                await b.open(bad)
                print(f"❌ защита: {bad} НЕ заблокирован!")
            except BrowserError as e:
                print(f"✅ защита: {bad} заблокирован ({e})")
        await step("search (DuckDuckGo)", b.search("Wildberries FBS что это"))
    finally:
        await b.close()
    print("\nГотово.")


asyncio.run(main())
