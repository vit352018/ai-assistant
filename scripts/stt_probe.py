"""Проверка распознавания речи через свой Worker: синтезируем фразу и отправляем её как запись."""
import asyncio
import os
import time

import edge_tts
import httpx


async def main():
    url = os.environ["PROXY_BASE"].rstrip("/") + "/ai/stt"
    audio = bytearray()
    async for ch in edge_tts.Communicate("Покажи остатки на складе", "ru-RU-DmitryNeural").stream():
        if ch["type"] == "audio":
            audio += ch["data"]
    print("записано байт (mp3):", len(audio))
    async with httpx.AsyncClient() as c:
        for i in (1, 2):
            t = time.time()
            try:
                r = await c.post(url, content=bytes(audio), headers={"Content-Type": "application/octet-stream"}, timeout=90)
                print(f"попытка {i}: HTTP {r.status_code}, {time.time() - t:.1f} с, ответ: {r.text[:300]}")
            except Exception as e:  # noqa: BLE001
                print(f"попытка {i}: ОШИБКА {type(e).__name__} {e!r}, {time.time() - t:.1f} с")


asyncio.run(main())
