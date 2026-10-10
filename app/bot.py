import logging

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import load
from .jsonutil import split_message
from .llm import LLMError, LLMRouter, Provider
from .browser import Browser
from .browser_agent import build as build_browser_agent
from .orchestrator import Orchestrator
from .voice import synthesize, transcribe
from .wb import WB
from .wb_agent import build as build_wb_agent

log = logging.getLogger("bot")
cfg = None


async def post_init(app):
    client = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=3))
    h = cfg.hosts()
    providers = [Provider(f"cf {m.split('/')[-1]}", f"{h['ai']}/v1", "-", m) for m in cfg.cf_models]
    if cfg.cohere_key:
        providers += [Provider(f"cohere {m}", f"{h['cohere']}/compatibility/v1", cfg.cohere_key, m) for m in cfg.cohere_models]
    if cfg.mistral_key:
        providers.append(Provider("mistral", f"{h['mistral']}/v1", cfg.mistral_key, cfg.mistral_model))
    if cfg.sambanova_key:
        providers.append(Provider("sambanova", f"{h['sambanova']}/v1", cfg.sambanova_key, cfg.sambanova_model))
    llm = LLMRouter(client, providers)
    wb = WB(client, cfg.wb_token)
    agents, browser = [build_wb_agent(wb)], None
    if cfg.browser_enabled:
        browser = Browser()
        agents.append(build_browser_agent(browser))
    app.bot_data.update(client=client, llm=llm, wb=wb, browser=browser, always_voice=False,
                        orch=Orchestrator(llm, agents))


async def post_shutdown(app):
    if app.bot_data.get("browser"):
        await app.bot_data["browser"].close()
    client = app.bot_data.get("client")  # его нет, если бот не смог даже запуститься
    if client:
        await client.aclose()


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
    if d.get("browser"):
        try:
            lines.append("✅ Браузер: " + await d["browser"].selftest())
        except Exception as e:  # noqa: BLE001
            lines.append(f"❌ Браузер: {e}")
    try:
        ok = len(await synthesize("Проверка голоса", cfg.tts_voice)) > 0
        lines.append("✅ Голос (синтез)" if ok else "❌ Голос: пустой результат")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ Голос: {e}")
    await update.message.reply_text("\n".join(lines))


async def respond(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text, voice_in):
    d = ctx.application.bot_data
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    spoken = ""  # то, что озвучим: без пометки о модели
    try:
        answer = spoken = await d["orch"].handle(text)
        answer += f"\n\n— ответила модель: {d['llm'].last_model}"
    except LLMError as e:
        answer = f"⚠️ Все бесплатные модели сейчас недоступны (обычно это лимиты). Попробуйте через минуту.\n{e}"
    except Exception as e:  # noqa: BLE001
        log.exception("handle failed")
        answer = f"⚠️ Не получилось: {type(e).__name__}: {e}"
    for part in split_message(answer):
        await update.message.reply_text(part)
    if voice_in or d["always_voice"]:
        try:
            audio = await synthesize(spoken, cfg.tts_voice)
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
        text = await transcribe(d["client"], cfg.hosts()["ai"], bytes(await f.download_as_bytearray()))
    except Exception as e:  # noqa: BLE001
        log.exception("stt failed")
        await update.message.reply_text(f"⚠️ Не удалось распознать голос: {type(e).__name__}: {e}")
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
    tg = cfg.hosts()["tg"]
    app = (Application.builder().token(cfg.bot_token)
           .base_url(f"{tg}/bot").base_file_url(f"{tg}/file/bot")
           .connect_timeout(15).read_timeout(30).write_timeout(30).pool_timeout(15)
           .get_updates_read_timeout(40)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start, filters=owner))
    app.add_handler(CommandHandler("check", check, filters=owner))
    app.add_handler(CommandHandler("voice", voice_toggle, filters=owner))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & owner, on_text))
    app.add_handler(MessageHandler(filters.VOICE & owner, on_voice))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
