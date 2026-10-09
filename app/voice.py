import re


async def transcribe(client, groq_key, model, audio, filename="voice.ogg"):
    """Голос -> текст через Groq Whisper (бесплатно). Telegram-голосовые уже в формате ogg."""
    r = await client.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {groq_key}"},
        files={"file": (filename, audio, "audio/ogg")},
        data={"model": model, "language": "ru", "response_format": "text"},
        timeout=60,
    )
    r.raise_for_status()
    return r.text.strip()


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
