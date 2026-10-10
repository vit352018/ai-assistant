import logging
import time

log = logging.getLogger("llm")

# Что выбирать среди бесплатных моделей OpenRouter (по подстроке в названии), по порядку.
PREFER = ("llama-3.3-70b", "gpt-oss-120b", "qwen3", "gemma", "llama")


class LLMError(Exception):
    pass


class Provider:
    """Один OpenAI-совместимый сервис (Groq, OpenRouter...). Пустая model = выбрать бесплатную автоматически."""

    def __init__(self, name, base_url, key, model=""):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.model = model
        self.auto = not model
        self.pause_until = 0.0


async def pick_free_model(client, base_url):
    r = await client.get(f"{base_url}/models", timeout=30)
    r.raise_for_status()
    free = [
        m["id"]
        for m in r.json().get("data", [])
        if str((m.get("pricing") or {}).get("prompt")) == "0"
        and str((m.get("pricing") or {}).get("completion")) == "0"
    ]
    for key in PREFER:
        for model_id in free:
            if key in model_id:
                return model_id
    return free[0] if free else None


def _retry_after(r):
    try:
        return min(float(r.headers.get("retry-after", 60)), 300)
    except ValueError:
        return 60


def _text(content):
    """Ответ модели бывает строкой или списком частей. Остальное — ошибка провайдера."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content).strip()
    raise LLMError(f"неожиданный формат ответа: {type(content).__name__}")


class LLMRouter:
    """Пробует провайдеров по очереди: если один упёрся в лимит или упал — берёт следующий."""

    def __init__(self, client, providers):
        self.client = client
        self.providers = providers

    async def chat(self, messages, max_tokens=1500, temperature=0.2):
        import asyncio
        errors = []
        for attempt in range(2):  # второй круг — если все провайдеры лишь временно заняты
            errors = []
            for p in self.providers:
                if time.time() < p.pause_until:
                    errors.append(f"{p.name}: занят, пауза")
                    continue
                try:
                    return await self._call(p, messages, max_tokens, temperature)
                except Exception as e:  # noqa: BLE001 — любая ошибка = пробуем следующего
                    errors.append(f"{p.name}: {e}")
                    log.warning("LLM %s: %s", p.name, e)
            waits = [p.pause_until - time.time() for p in self.providers if p.pause_until > time.time()]
            if attempt == 0 and waits:
                await asyncio.sleep(min(max(min(waits), 1), 12))
            elif attempt == 0:
                await asyncio.sleep(2)
        raise LLMError("; ".join(errors))

    async def _call(self, p, messages, max_tokens, temperature):
        if not p.model:
            p.model = await pick_free_model(self.client, p.base_url)
            if not p.model:
                raise LLMError("бесплатных моделей не найдено")
        r = await self.client.post(
            f"{p.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {p.key}"} if p.key else {},
            json={"model": p.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
            timeout=60,
        )
        if r.status_code == 429:
            p.pause_until = time.time() + _retry_after(r)
            raise LLMError("лимит запросов (429)")
        if r.status_code in (401, 403):
            p.pause_until = time.time() + 600
        if r.status_code >= 400:
            if p.auto:
                p.model = ""  # выбранная модель пропала — в следующий раз выберем заново
            raise LLMError(f"HTTP {r.status_code}: {r.text[:150]}")
        text = _text(r.json()["choices"][0]["message"].get("content"))
        if not text:
            raise LLMError("пустой ответ")
        return text

    async def ping(self):
        out = []
        for p in self.providers:
            try:
                await self._call(p, [{"role": "user", "content": "Ответь одним словом: ок"}], 300, 0)
                out.append(f"✅ {p.name} ({p.model})")
            except Exception as e:  # noqa: BLE001
                out.append(f"❌ {p.name}: {e}")
        return out
