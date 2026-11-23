from .ads import AdsClient, AdsError, status_name, summarize_stats
from .agents import Agent, Tool

PROMPT = """Ты — рекламный аналитик магазина Wildberries. Пока ты только читаешь рекламу и считаешь итоги.
Ничего не запускаешь, не останавливаешь и не меняешь ставки и бюджеты: такие действия появятся позже, только по подтверждению владельца.
Лимиты, которые владелец установил: общий дневной бюджет на рекламу 3000 ₽, на одну кампанию до 1000 ₽, ставка вверх до 10% за раз, вниз до 20%.
Числа бери только из ответов инструментов. Если кампаний нет или статистики нет — так и скажи."""


def build(ads: AdsClient) -> Agent:
    async def campaigns(status=None, offset=0):
        rows = await ads.campaigns()
        if status not in (None, ""):
            rows = [r for r in rows if str(r["status"]) == str(status)]
        counts = {}
        for r in rows:  # итоги считаются по ВСЕМ кампаниям, а не по показанной странице
            counts[status_name(r["status"])] = counts.get(status_name(r["status"]), 0) + 1
        head = f"Всего кампаний: {len(rows)}. По статусам: " + (", ".join(f"{k} — {v}" for k, v in counts.items()) or "нет")
        if not rows:
            return head
        offset = max(0, int(offset or 0))
        page = rows[offset:offset + 40]
        lines = [f"{r['id']} «{r['name'][:50]}» — {status_name(r['status'])}, тип ставок: {r['bid_type']}" for r in page]
        out = [head, f"Показаны {offset + 1}–{offset + len(page)} из {len(rows)}."] + lines
        if offset + len(page) < len(rows):
            out.append(f"Это не все кампании. Следующая страница: campaigns с offset={offset + len(page)}.")
        return "\n".join(out)

    async def budget(advert_id):
        return f"Бюджет кампании {advert_id}: {await ads.budget(int(advert_id)):.0f} ₽ (остаток на счёте кампании)."

    async def stats(days=7):
        ids = [c["id"] for c in await ads.campaigns() if c["id"]]
        rows = await ads.stats(ids, int(days))
        return summarize_stats(rows)

    async def account():
        bal = await ads.balance()
        cfg = await ads.settings()
        return (f"Баланс рекламного счёта: {bal.get('balance', '?')} ₽ (чистый: {bal.get('net', '?')}). "
                f"Минимальный дневной лимит WB: {cfg.get('minDailyLimit', '?')} ₽; минимальная ставка для поиска: "
                f"{cfg.get('cpcMaxBid', '?')} (макс.) — шаг {cfg.get('cpcStep', '?')}.")

    tools = [
        Tool("campaigns", "список рекламных кампаний со статусами и итогами по статусам. Аргументы: status=число (необязательно: 9 активна, 11 на паузе, 7 завершена), offset=с какой кампании показать (для следующей страницы)", campaigns),
        Tool("budget", "бюджет одной кампании. Аргумент: advert_id=число", budget),
        Tool("stats", "статистика по всем кампаниям за период: показы, клики, CTR, заказы, расход. Аргумент: days=число (по умолчанию 7)", stats),
        Tool("account", "баланс рекламного счёта и общие ограничения WB на рекламу", account),
    ]
    return Agent("ads", "Реклама Wildberries: кампании и их статусы, бюджеты, статистика показов и заказов, баланс рекламного счёта (только чтение, изменений не делает)",
                 PROMPT, tools)
