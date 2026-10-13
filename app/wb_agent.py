import datetime as dt

from .agents import Agent, Tool, today
from .wb import WB, agg_orders, agg_sales, agg_stocks, extract_items

PROMPT = """Ты — WB-агент: аналитик магазина на Wildberries. Работаешь ТОЛЬКО на чтение: менять цены, остатки и карточки ты пока не умеешь (эти действия появятся позже и только с подтверждением владельца).
Сначала получи данные инструментами, затем дай выводы: что важно, что заканчивается, что падает или растёт. Если данных не хватает — скажи, чего именно.
Всегда называй общее число найденных позиций из ответа инструмента; если инструмент сообщил, что часть товаров не показана, скажи об этом и предложи показать остальные."""


def build(wb: WB) -> Agent:
    def since(days):
        return (today() - dt.timedelta(days=max(1, min(int(days), 90)))).isoformat()

    async def stocks(order="low", top=None, max_qty=None):
        # Старый метод statistics/supplier/stocks отключён 23.06.2026; остатки — в Analytics API (лимит 3 запроса в минуту)
        resp = await wb.post_read("analytics", "/api/analytics/v1/stocks-report/wb-warehouses",
                                  {"limit": 20000, "offset": 0}, ttl=300)
        rows = extract_items(resp)
        if not rows and resp:
            return f"Остатки пусты или формат ответа WB изменился. Ключи ответа: {list(resp)[:8] if isinstance(resp, dict) else type(resp).__name__}"
        try:
            names = await wb.catalog()  # артикулы и названия, чтобы по остаткам можно было найти товар в базе
        except Exception:  # noqa: BLE001 — без названий остатки всё равно полезны
            names = {}
        limit = int(max_qty) if max_qty not in (None, "") else None
        return agg_stocks(rows, order, int(top or (200 if limit is not None else 25)), names, limit)

    async def sales(days=7):
        s = since(days)
        return agg_sales(await wb.get("stats", "/api/v1/supplier/sales", {"dateFrom": s}, ttl=300), s)

    async def orders(days=7):
        s = since(days)
        return agg_orders(await wb.get("stats", "/api/v1/supplier/orders", {"dateFrom": s}, ttl=300), s)

    async def prices(search="", top=30):
        data = await wb.get("prices", "/api/v2/list/goods/filter", {"limit": 1000, "offset": 0}, ttl=120)
        goods = (data.get("data") or {}).get("listGoods", [])
        out = []
        for g in goods:
            if search.lower() not in str(g.get("vendorCode", "")).lower():
                continue
            sizes = g.get("sizes") or [{}]
            price = min(s.get("price", 0) for s in sizes)
            final = min(s.get("discountedPrice", 0) for s in sizes)
            out.append(f"{' '.join(str(g.get('vendorCode')).split())} (nm {g.get('nmID')}): цена {price}₽, скидка {g.get('discount', 0)}%, итого {final}₽")
        return f"Найдено товаров: {len(out)}\n" + "\n".join(out[: int(top)])

    async def cards(search="", top=20):
        body = {"settings": {"sort": {"ascending": False},
                             "filter": {"textSearch": search, "withPhoto": -1},
                             "cursor": {"limit": min(int(top), 100)}}}
        data = await wb.post_read("content", "/content/v2/get/cards/list", body, {"locale": "ru"}, ttl=120)
        return "\n".join(
            f"nm {c.get('nmID')}, арт. {c.get('vendorCode')}: {c.get('title')} | {c.get('brand')} | {c.get('subjectName')}"
            for c in data.get("cards", [])
        ) or "Карточек не найдено."

    async def feedbacks(top=10):
        params = {"isAnswered": "false", "take": min(int(top), 30), "skip": 0, "order": "dateDesc"}
        d = (await wb.get("feedbacks", "/api/v1/feedbacks", params, ttl=60)).get("data") or {}
        lines = [f"Отзывов без ответа: {d.get('countUnanswered', '?')}"]
        for f in d.get("feedbacks", []):  # имён покупателей не берём — только текст и оценку
            p = f.get("productDetails") or {}
            lines.append(f"{f.get('productValuation')}★ {p.get('productName')} ({p.get('supplierArticle')}) "
                         f"{str(f.get('createdDate', ''))[:10]}: {str(f.get('text', ''))[:200]}")
        return "\n".join(lines)

    tools = [
        Tool("stocks", "остатки на складах WB: артикул, название и количество каждого товара (названия уже включены, cards для них вызывать не нужно). Аргументы: order='low' (сначала мало) или 'high', max_qty=число (показать только товары с остатком не больше этого числа, например 3 для «меньше 4 штук»), top=число строк (по умолчанию 25)", stocks),
        Tool("sales", "продажи и возвраты за период. Аргумент: days=число дней (по умолчанию 7, максимум 90)", sales),
        Tool("orders", "заказы и отмены за период. Аргумент: days=число дней (по умолчанию 7)", orders),
        Tool("prices", "текущие цены и скидки. Аргументы: search='часть артикула' (необязательно), top=число строк", prices),
        Tool("cards", "карточки товаров (название, бренд, категория). Аргументы: search='текст' (необязательно), top=число", cards),
        Tool("feedbacks", "последние отзывы покупателей без ответа. Аргумент: top=число (по умолчанию 10)", feedbacks),
    ]
    return Agent("wb", "Wildberries: остатки, продажи, заказы, цены, карточки товаров, отзывы (только чтение)", PROMPT, tools)
