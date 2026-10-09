import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    groq_key: str
    openrouter_key: str
    wb_token: str
    groq_model: str
    stt_model: str
    openrouter_model: str
    tts_voice: str
    proxy_base: str = ""

    def hosts(self):
        """Куда ходить за границу: напрямую или через свой Cloudflare Worker (PROXY_BASE)."""
        p = self.proxy_base.rstrip("/")
        if not p:
            return {"tg": "https://api.telegram.org", "groq": "https://api.groq.com", "or": "https://openrouter.ai"}
        return {"tg": p + "/tg", "groq": p + "/groq", "or": p + "/or"}


def _need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"В файле .env не заполнено: {name}")
    return value


def load():
    return Config(
        bot_token=_need("TELEGRAM_BOT_TOKEN"),
        owner_id=int(_need("OWNER_ID")),
        groq_key=_need("GROQ_API_KEY"),
        openrouter_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        wb_token=os.environ.get("WB_TOKEN", "").strip(),
        groq_model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip(),
        stt_model=os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo").strip(),
        openrouter_model=os.environ.get("OPENROUTER_MODEL", "").strip(),
        tts_voice=os.environ.get("TTS_VOICE", "ru-RU-DmitryNeural").strip(),
        proxy_base=os.environ.get("PROXY_BASE", "").strip(),
    )
