import logging
import os
import re
import time

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

from .actions import ActionStore
from .config import load
from .jsonutil import split_message
from .llm import LLMError, LLMRouter, Provider
from .browser import Browser
from .browser_agent import build as build_browser_agent
from .orchestrator import Orchestrator
from .stock_actions import KIND as STOCK_KIND, execute_stock_update
from .voice import speak_via_worker, transcribe_telegram, wants_voice
from .wb import WB
from .wb_write import WBWriter
from .wb_agent import build as build_wb_agent

log = logging.getLogger("bot")
cfg = None


async def post_init(app):
    # Соединения не переиспользуем: через мобильную/сетевую прослойку переиспользованное соединение
    # может «зависнуть» на 15 секунд. Каждый запрос идёт по новому соединению.
    client = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0))
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
    os.makedirs(cfg.data_dir, exist_ok=True)
    store = ActionStore(os.path.join(cfg.data_dir, "actions.db"))
    writer = WBWriter(client, cfg.wb_write_token) if cfg.wb_write_token else None  # без токена записи изменений нет
    agents, browser = [build_wb_agent(wb, store if writer else None)], None
    if cfg.browser_enabled:
        browser = Browser()
        agents.append(build_browser_agent(browser))
    app.bot_data.update(client=client, llm=llm, wb=wb, store=store, writer=writer, browser=browser, always_voice=False,
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
        "/check — проверить, что всё подключено\n/voicetest — проверить голосовой ответ\n/log — последние изменения в WB\n"
        "Чтобы получить ответ голосом, добавьте в вопрос «ответь голосом» или «озвучь».\n/voice — отвечать голосом всегда (повторно — выключить)"
    )


async def voice_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.application.bot_data
    d["always_voice"] = not d["always_voice"]
    await update.message.reply_text("Голосовые ответы всегда: " + ("включены" if d["always_voice"] else "выключены (голос — только по просьбе)"))


async def voicetest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.application.bot_data
    try:
        method = await speak_via_worker(d["client"], cfg.hosts()["ai"], cfg.bot_token, update.effective_chat.id,
                                        "Проверка голоса. Раз, два, три.", cfg.tts_voice, update.message.message_id)
        await update.message.reply_text(f"✅ Голос отправлен ({method})")
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"❌ Голос: {type(e).__name__}: {e}")


async def check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.application.bot_data
    lines = await d["llm"].ping()
    try:
        lines.append(f"✅ WB: магазин «{await d['wb'].seller()}»")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ WB: {e}")
    if d.get("writer"):
        try:
            lines.append(f"✅ WB запись: токен принят, магазин «{await d['writer'].seller()}» (изменения только по кнопке ✅)")
        except Exception as e:  # noqa: BLE001
            lines.append(f"❌ WB запись: {e}")
    else:
        lines.append("ℹ️ WB запись: токен не задан (WB_WRITE_TOKEN), бот может только читать")
    if d.get("browser"):
        try:
            lines.append("✅ Браузер: " + await d["browser"].selftest())
        except Exception as e:  # noqa: BLE001
            lines.append(f"❌ Браузер: {e}")
    lines.append("ℹ️ Голосовой ответ проверяется командой /voicetest (озвучка идёт через Worker, а не с сервера)")
    await update.message.reply_text("\n".join(lines))


EXECUTORS = {STOCK_KIND: execute_stock_update}


async def decide(store, writer, data):
    """Нажатие кнопки: ok:<id> выполняет предложение (один раз), no:<id> отменяет. Возвращает текст для чата."""
    verb, aid = data.split(":", 1)
    if verb == "no":
        return "❌ Отменено. Ничего не изменено." if store.cancel(aid) else "Это предложение уже обработано или истекло."
    if writer is None:
        return "Токен WB на запись не настроен, выполнить нельзя."
    claimed = store.claim(aid)
    if not claimed:
        return "Предложение уже выполнено, отменено или истекло (оно действует 10 минут). Попросите заново."
    kind, payload = claimed
    try:
        ok, text = await EXECUTORS[kind](writer, payload)
    except Exception as e:  # noqa: BLE001
        log.exception("action failed")
        ok, text = False, (f"❌ Ошибка при выполнении: {type(e).__name__}: {e}\n"
                           "Прежде чем повторять, проверьте остатки в кабинете WB: запрос мог дойти.")
    store.finish(aid, ok, text)
    return text


async def on_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q, d = update.callback_query, ctx.application.bot_data
    if q.from_user.id != cfg.owner_id:  # кнопки работают только у владельца
        await q.answer("Нет доступа", show_alert=True)
        return
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)  # кнопки исчезают сразу: двойное нажатие невозможно
    await q.message.reply_text(await decide(d["store"], d["writer"], q.data))


async def send_proposals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Предложения, созданные при обработке сообщения, показываем с кнопками подтверждения."""
    store = ctx.application.bot_data["store"]
    for aid in store.pop_new():
        a = store.get(aid)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнить", callback_data=f"ok:{aid}"),
                                    InlineKeyboardButton("❌ Отмена", callback_data=f"no:{aid}")]])
        await update.message.reply_text(
            f"🛠 Предложение #{aid}\n{a['preview']}\n\nДействует 10 минут. Пока вы не нажмёте ✅, ничего не изменится.",
            reply_markup=kb)


async def log_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = ctx.application.bot_data["store"].recent(10)
    lines = [f"{time.strftime('%d.%m %H:%M', time.localtime(r['created']))} #{r['id']} {r['status']}" for r in rows]
    await update.message.reply_text("Последние действия:\n" + "\n".join(lines) if lines else "Действий пока не было.")


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
    store = d.get("store")
    if store is not None and not store.new_ids and re.search(r"предложени\w*\s*#|предложение\s+подготовлено|кнопк", answer, re.I):
        # модель написала про предложение, но инструмент его не создал: кнопок не будет — говорим прямо
        answer += "\n\n⚠️ Предложение НЕ создано (кнопок нет, ничего не изменено). Повторите просьбу, например: «установи остаток артикула X равным N»."
    for part in split_message(answer):
        await update.message.reply_text(part)
    await send_proposals(update, ctx)
    if d["always_voice"] or wants_voice(text):  # голос — только по просьбе (или если включён /voice)
        try:
            await speak_via_worker(d["client"], cfg.hosts()["ai"], cfg.bot_token, update.effective_chat.id,
                                   spoken, cfg.tts_voice, update.message.message_id)
        except Exception as e:  # noqa: BLE001 — текст уже отправлен, о сбое голоса говорим прямо
            log.exception("tts failed")
            await update.message.reply_text(f"🔇 Голосом не получилось: {type(e).__name__}: {e}")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await respond(update, ctx, update.message.text, voice_in=False)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.application.bot_data
    try:
        text = await transcribe_telegram(d["client"], cfg.hosts()["ai"], cfg.bot_token, update.message.voice.file_id)
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
    app.add_handler(CommandHandler("voicetest", voicetest, filters=owner))
    app.add_handler(CommandHandler("voice", voice_toggle, filters=owner))
    app.add_handler(CommandHandler("log", log_command, filters=owner))
    app.add_handler(CallbackQueryHandler(on_action, pattern=r"^(ok|no):[0-9a-f]{8}$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & owner, on_text))
    app.add_handler(MessageHandler(filters.VOICE & owner, on_voice))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
