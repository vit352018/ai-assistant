"""На каком размере запроса обрывается связь с Worker. Данные нарочно не звук:
ответ HTTP 502 от Worker здесь — это УСПЕХ (связь дошла), а ОБРЫВ — признак сетевой проблемы."""
import asyncio
import base64
import os
import time

import httpx


async def main():
    url = os.environ["PROXY_BASE"].rstrip("/") + "/ai/stt"
    async with httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0)) as c:
        for kb in (5, 15, 30, 60, 120):
            body = base64.b64encode(os.urandom(kb * 1024 * 3 // 4))
            for i in (1, 2, 3):
                t = time.time()
                try:
                    r = await c.post(url, content=body, headers={"Content-Type": "text/plain"}, timeout=40)
                    print(f"{kb:>4} КБ, попытка {i}: связь дошла (HTTP {r.status_code}), {time.time() - t:.1f} с")
                except Exception as e:  # noqa: BLE001
                    print(f"{kb:>4} КБ, попытка {i}: ОБРЫВ {type(e).__name__}, {time.time() - t:.1f} с")


asyncio.run(main())
