import re

# Озвучиваем ответ только если об этом попросили: «ответь голосом», «озвучь», «скажи вслух»...
VOICE_REQUEST = re.compile(r"голосом|голосов\w*\s+(сообщени|ответ)|озвуч|вслух|продиктуй", re.I)


def wants_voice(text):
    return bool(VOICE_REQUEST.search(text or ""))


async def _stt_post(client, url, tries, note="", **kw):
    """Запрос на распознавание с повторами: обрыв соединения — частое дело, поэтому повторяем сразу."""
    import asyncio
    import httpx
    timeout = httpx.Timeout(connect=15, read=60, write=30, pool=15)
    last = ""
    for i in range(tries):
        try:
            r = await client.post(url, timeout=timeout, **kw)
        except httpx.TransportError as e:
            last = f"{type(e).__name__}: соединение с Worker оборвалось"
            await asyncio.sleep(0.5)
            continue
        if r.status_code == 200:
            return (r.json().get("text") or "").strip()
        last = f"HTTP {r.status_code}: {r.text[:200]}"
        if r.status_code not in (429, 502, 503, 504):
            break
        await asyncio.sleep(2 * (i + 1))
    raise RuntimeError(f"{last}{note}")


async def transcribe_telegram(client, ai_base, bot_token, file_id, tries=3):
    """Голос -> текст. На сервере из России большие запросы к Cloudflare обрываются, поэтому сам звук
    не отправляем: Worker получает только file_id, скачивает запись у Telegram и распознаёт её Whisper'ом."""
    return await _stt_post(client, f"{ai_base}/stt-telegram", tries,
                           json={"token": bot_token, "file_id": file_id})


async def transcribe(client, ai_base, audio, tries=3):
    """Распознавание готовой записи (до ~15 КБ). Нужно для проверочных скриптов."""
    import base64
    return await _stt_post(client, f"{ai_base}/stt", tries, f" (запись {len(audio) // 1024} КБ)",
                           content=base64.b64encode(audio), headers={"Content-Type": "text/plain"})


def clean_for_speech(text, limit=900):
    t = re.sub(r"https?://\S+", "", text)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = re.sub(r"[\u2600-\u27BF\U0001F300-\U0001FAFF]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t.rfind(".", 0, limit)
        t = t[: cut + 1] if cut > limit // 2 else t[:limit]
    return t


async def speak_via_worker(client, ai_base, bot_token, chat_id, text, voice, reply_to=None, tries=2):
    """Озвучка внутри Worker: ему уходит только текст, звук он получает у Microsoft Edge TTS и сам
    отправляет в Telegram (с сервера из России большой файл в Cloudflare не проходит)."""
    import asyncio
    import httpx
    speech = clean_for_speech(text)
    if not speech:
        return "пусто"
    payload = {"token": bot_token, "chat_id": chat_id, "text": speech, "voice": voice, "reply_to": reply_to}
    timeout = httpx.Timeout(connect=15, read=60, write=30, pool=15)
    last = ""
    for i in range(tries):
        try:
            r = await client.post(f"{ai_base}/tts-telegram", json=payload, timeout=timeout)
        except httpx.TransportError as e:
            last = f"{type(e).__name__}: соединение с Worker оборвалось"
            await asyncio.sleep(0.5)
            continue
        if r.status_code == 200:
            j = r.json()
            return f"{j.get('method', 'ok')}, голос {j.get('engine', '?')}"
        last = f"HTTP {r.status_code}: {r.text[:200]}"
        break
    raise RuntimeError(last)


async def synthesize(text, voice):
    """Текст -> голос (mp3) через edge-tts. Telegram принимает mp3 как голосовое."""
    import edge_tts

    speech = clean_for_speech(text)
    if not speech:
        return b""
    buf = bytearray()
    async for chunk in edge_tts.Communicate(speech, voice).stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    return bytes(buf)
