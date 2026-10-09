import logging

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import load
from .jsonutil import split_message
from .llm import LLMError, LLMRouter, Provider
from .orchestrator import Orchestrator
from .voice import synthesize, transcribe
from .wb import WB
from .wb_agent import build as build_wb_agent

log = logging.getLogger("bot")
cfg = None


async def post_init(app):
    client = httpx.AsyncClient()
    providers = [Provider("groq", "https://api.groq.com/openai/v1", cfg.groq_key, cfg.groq_model)]
    if cfg.openrouter_key:
        providers.append(Provider("openrouter", "https://openrouter.ai/api/v1", cfg.openrouter_key, cfg.openrouter_model))
    llm = LLMRouter(client, providers)
    wb = WB(client, cfg.wb_token)
    app.bot_data.update(client=client, llm=llm, wb=wb, always_voice=False,
                        orch=Orchestrator(llm, [build_wb_agent(wb)]))


async def post_shutdown(app):
    await app.bot_data["client"].aclose()


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я ваш помощник. Пишите или говорите голосом, например: «что с остатками?» или «продажи за неделю».\n"
        "/check — проверить, что всё подключено\n/voice — отвечать голосом всегда (повторно — выключить)"
    )


async def voice_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.application.bot_data
    d["always_voice"] = not d["always_voice"]
    await update.message.reply_text("Голосовые ответы всегда: " + ("включены" if d["always_voice"] else "выключены (только на ваши голосовые)"))


async def check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.application.bot_data
    lines = await d["llm"].ping()
    try:
        lines.append(f"✅ WB: магазин «{await d['wb'].seller()}»")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ WB: {e}")
    try:
        ok = len(await synthesize("Проверка голоса", cfg.tts_voice)) > 0
        lines.append("✅ Голос (синтез)" if ok else "❌ Голос: пустой результат")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ Голос: {e}")
    await update.message.reply_text("\n".join(lines))


async def respond(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text, voice_in):
    d = ctx.application.bot_data
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        answer = await d["orch"].handle(text)
    except LLMError as e:
        answer = f"⚠️ Все бесплатные модели сейчас недоступны (обычно это лимиты). Попробуйте через минуту.\n{e}"
    except Exception as e:  # noqa: BLE001
        log.exception("handle failed")
        answer = f"⚠️ Не получилось: {e}"
    for part in split_message(answer):
        await update.message.reply_text(part)
    if voice_in or d["always_voice"]:
        try:
            audio = await synthesize(answer, cfg.tts_voice)
            if audio:
                await update.message.reply_voice(audio)
        except Exception:  # noqa: BLE001 — текст уже отправлен, голос не критичен
            log.exception("tts failed")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await respond(update, ctx, update.message.text, voice_in=False)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.application.bot_data
    try:
        f = await update.message.voice.get_file()
        text = await transcribe(d["client"], cfg.groq_key, cfg.stt_model, bytes(await f.download_as_bytearray()))
    except Exception as e:  # noqa: BLE001
        log.exception("stt failed")
        await update.message.reply_text(f"⚠️ Не удалось распознать голос: {e}")
        return
    if not text:
        await update.message.reply_text("Не расслышал, повторите, пожалуйста.")
        return
    await update.message.reply_text(f"🎙 {text}")
    await respond(update, ctx, text, voice_in=True)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка Telegram: %s", ctx.error)


def main():
    global cfg
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore"):  # в адресах запросов виден токен бота — не пишем их в журнал
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load()
    owner = filters.User(user_id=cfg.owner_id)  # всем остальным бот не отвечает
    app = Application.builder().token(cfg.bot_token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start, filters=owner))
    app.add_handler(CommandHandler("check", check, filters=owner))
    app.add_handler(CommandHandler("voice", voice_toggle, filters=owner))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & owner, on_text))
    app.add_handler(MessageHandler(filters.VOICE & owner, on_voice))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
