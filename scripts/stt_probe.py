"""Проверка распознавания речи через свой Worker: синтезируем фразу и отправляем её как запись."""
import asyncio
import base64
import os
import time

import edge_tts
import httpx


async def main():
    """Проверяем разные длины записи: видно, с какого размера начинаются обрывы."""
    url = os.environ["PROXY_BASE"].rstrip("/") + "/ai/stt"
    phrases = {"короткая": "Покажи остатки на складе",
               "средняя": "Покажи остатки на складе. Сравни продажи за неделю с прошлой неделей и напиши, что растёт, а что падает",
               "длинная": "Покажи остатки на складе. " * 6}
    async with httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0)) as c:
        for name, text in phrases.items():
            audio = bytearray()
            async for ch in edge_tts.Communicate(text, "ru-RU-DmitryNeural").stream():
                if ch["type"] == "audio":
                    audio += ch["data"]
            for i in (1, 2):
                t = time.time()
                try:
                    r = await c.post(url, content=base64.b64encode(bytes(audio)),
                                     headers={"Content-Type": "text/plain"}, timeout=90)
                    print(f"{name} ({len(audio)} байт), попытка {i}: HTTP {r.status_code}, {time.time() - t:.1f} с, {r.text[:120]}")
                except Exception as e:  # noqa: BLE001
                    print(f"{name} ({len(audio)} байт), попытка {i}: ОШИБКА {type(e).__name__}, {time.time() - t:.1f} с")


asyncio.run(main())
