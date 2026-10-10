import re


async def transcribe(client, ai_base, audio, tries=3):
    """Голос -> текст: запись уходит в свой Worker, он распознаёт её Whisper'ом (Workers AI).
    При перегрузке повторяем с паузой."""
    import asyncio
    last = ""
    for i in range(tries):
        r = await client.post(f"{ai_base}/stt", content=audio,
                              headers={"Content-Type": "application/octet-stream"}, timeout=90)
        if r.status_code == 200:
            return (r.json().get("text") or "").strip()
        last = f"HTTP {r.status_code}: {r.text[:120]}"
        if r.status_code not in (429, 502, 503, 504):
            break
        await asyncio.sleep(2 * (i + 1))
    raise RuntimeError(last)


def clean_for_speech(text, limit=900):
    t = re.sub(r"https?://\S+", "", text)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = re.sub(r"[\u2600-\u27BF\U0001F300-\U0001FAFF]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t.rfind(".", 0, limit)
        t = t[: cut + 1] if cut > limit // 2 else t[:limit]
    return t


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
