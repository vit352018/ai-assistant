import os
from dataclasses import dataclass

# Модели Workers AI по порядку: если у первой кончился дневной лимит, берём следующую.
DEFAULT_CF_MODELS = "@cf/meta/llama-3.3-70b-instruct-fp8-fast,@cf/meta/llama-3.1-8b-instruct-fast"


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    proxy_base: str  # адрес своего Cloudflare Worker вместе с секретом
    wb_token: str
    openrouter_key: str
    cf_models: tuple
    openrouter_model: str
    tts_voice: str
    openrouter_base: str = ""  # необязательно: ходить в OpenRouter напрямую, минуя Worker

    def hosts(self):
        p = self.proxy_base.rstrip("/")
        return {"tg": p + "/tg", "ai": p + "/ai", "or": self.openrouter_base.rstrip("/") or p + "/or"}


def _need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"В файле .env не заполнено: {name}")
    return value


def load():
    models = os.environ.get("CF_MODELS", DEFAULT_CF_MODELS)
    return Config(
        bot_token=_need("TELEGRAM_BOT_TOKEN"),
        owner_id=int(_need("OWNER_ID")),
        proxy_base=_need("PROXY_BASE"),
        wb_token=os.environ.get("WB_TOKEN", "").strip(),
        openrouter_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        cf_models=tuple(m.strip() for m in models.split(",") if m.strip()),
        openrouter_model=os.environ.get("OPENROUTER_MODEL", "").strip(),
        tts_voice=os.environ.get("TTS_VOICE", "ru-RU-DmitryNeural").strip(),
        openrouter_base=os.environ.get("OPENROUTER_BASE", "").strip(),
    )
