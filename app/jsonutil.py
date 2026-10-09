import json
import re


def extract_json(text):
    """Достаёт первый JSON-объект из ответа модели, даже если вокруг есть слова или ```."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text).strip()
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def split_message(text, limit=4000):
    """Telegram не принимает сообщения длиннее 4096 символов — режем по строкам."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts
