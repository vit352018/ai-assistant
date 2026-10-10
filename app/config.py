import os
from dataclasses import dataclass

# Модели Workers AI по порядку: если у первой кончился дневной лимит, берём следующую.
# Порядок = приоритет. Все модели Cloudflare делят один дневной лимит аккаунта.
DEFAULT_CF_MODELS = (
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast,@cf/openai/gpt-oss-120b,"
    "@cf/google/gemma-4-26b-a4b-it,@cf/meta/llama-3.1-8b-instruct-fast"
)
COHERE_MODELS = ("command-a-03-2025", "command-r7b-12-2024")


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    proxy_base: str  # адрес своего Cloudflare Worker вместе с секретом
    wb_token: str
    cf_models: tuple
    tts_voice: str
    browser_enabled: bool = True
    cohere_key: str = ""
    cohere_models: tuple = COHERE_MODELS
    mistral_key: str = ""
    mistral_model: str = "mistral-small-latest"
    sambanova_key: str = ""
    sambanova_model: str = "Meta-Llama-3.3-70B-Instruct"

    def hosts(self):
        p = self.proxy_base.rstrip("/")
        return {"tg": p + "/tg", "ai": p + "/ai",
                "mistral": p + "/mistral", "sambanova": p + "/sambanova", "cohere": p + "/cohere"}


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
        cf_models=tuple(m.strip() for m in models.split(",") if m.strip()),
        tts_voice=os.environ.get("TTS_VOICE", "ru-RU-DmitryNeural").strip(),
        browser_enabled=os.environ.get("BROWSER", "1").strip() != "0",
        cohere_key=os.environ.get("COHERE_API_KEY", "").strip(),
        mistral_key=os.environ.get("MISTRAL_API_KEY", "").strip(),
        mistral_model=os.environ.get("MISTRAL_MODEL", "mistral-small-latest").strip(),
        sambanova_key=os.environ.get("SAMBANOVA_API_KEY", "").strip(),
        sambanova_model=os.environ.get("SAMBANOVA_MODEL", "Meta-Llama-3.3-70B-Instruct").strip(),
    )
