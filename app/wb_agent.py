import datetime as dt
import json
import re

from .agents import Agent, Tool, today
from .stock_actions import ProposalError, pick_warehouse, propose_stock_update
from .wb import WB, agg_orders, agg_sales, agg_stocks, extract_items

PROMPT = """Ты — WB-агент: аналитик магазина на Wildberries. Данные ты получаешь инструментами и сам ничего в WB не меняешь.
Изменить остатки FBS можно только инструментом propose_stock_update: он НИЧЕГО не меняет, а лишь готовит предложение, которое владелец подтверждает кнопкой ✅ в чате.
Правила для изменений:
- Бери товары и количества строго из слов владельца. Не угадывай артикулы и числа; если что-то неясно — спроси.
- Текущий остаток узнаёшь САМ: propose_stock_update сам покажет «было → станет» в превью. Никогда не проси владельца сообщить текущий остаток или данные склада, которые он уже указал.
- Для предложения нужны только: артикул (или nm), новое количество, склад (warehouse_id, если он указан владельцем). Если чего-то из этого нет — спроси именно это, одним вопросом.
- Если владелец назвал склад по названию или номеру — передай его как warehouse_id, не переспрашивай.
- После вызова propose_stock_update скажи, что предложение показано с кнопками и ПОКА НИЧЕГО НЕ ИЗМЕНЕНО. Никогда не пиши, что остатки уже изменены.
- Цены, скидки и карточки пока менять нельзя — скажи об этом, если попросят.
Для анализа: сначала получи данные, затем дай выводы: что важно, что заканчивается, что падает или растёт. Если данных не хватает — скажи, чего именно.
Всегда называй общее число найденных позиций из ответа инструмента; если инструмент сообщил, что часть товаров не показана, скажи об этом и предложи показать остальные."""


CLAIM_STOCK = re.compile(r"предложени\w*|подготовлен\w*|показан\w* с кнопк|кнопк", re.I)
STOCK_NUDGE = ('Ты написал о подготовленном предложении, но инструмент propose_stock_update НЕ вызывался — '
               'предложения нет, кнопок нет. Вызови его сейчас, одной строкой JSON, с артикулом и новым количеством '
               'из запроса владельца: {"tool": "propose_stock_update", "args": {"items": [{"article": "...", "amount": N}]}}. '
               'Не пиши ничего, кроме этого JSON.')


def verify_stock(answer, called):
    if CLAIM_STOCK.search(answer or "") and "propose_stock_update" not in called:
        return STOCK_NUDGE
    return None


def build(wb: WB, store=None) -> Agent:
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

    async def fbs_warehouses():
        whs = await wb.warehouses()
        return "\n".join(f"{w.get('id')}: {w.get('name')}" for w in whs) or "Складов продавца нет."

    async def fbs_stocks(top=None, max_qty=None, warehouse_id=None):
        catalog = await wb.catalog()
        wh = await pick_warehouse(wb, warehouse_id)
        by_chrt = {sz["chrtId"]: (nm, c) for nm, c in catalog.items() for sz in c.get("sizes", [])}
        amounts = await wb.fbs_stocks(wh["id"], list(by_chrt))
        rows = sorted(((amounts.get(cid, 0), nm, c, cid in amounts) for cid, (nm, c) in by_chrt.items()),
                      key=lambda r: (r[0], not r[3]))
        limit = int(max_qty) if max_qty not in (None, "") else None
        if limit is not None:
            rows = [r for r in rows if r[0] <= limit]
        top = int(top or (100 if limit is not None else 30))
        missing = sum(1 for cid in by_chrt if cid not in amounts)
        head = f"Склад «{wh.get('name')}» (id {wh['id']}): подходит позиций {len(rows)}"
        if missing:
            head += f"; по {missing} из {len(by_chrt)} позиций WB остаток не вернул (считаю 0)"
        lines, used = [head], 0
        for n, (qty, nm, c, present) in enumerate(rows[:top]):
            line = f"{' '.join(c['vendorCode'].split())} «{' '.join(c['title'].split())[:45]}»: " + (f"{qty} шт" if present else "нет записи об остатке")
            if used + len(line) > 4200:
                lines.append(f"…ещё {min(len(rows), top) - n} позиций не показано (уточните max_qty или top)")
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    async def propose(items=None, warehouse_id=None):
        if store is None:
            return "Изменения остатков недоступны: не настроен токен WB на запись (WB_WRITE_TOKEN)."
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except ValueError:
                pass
        try:
            aid, preview = await propose_stock_update(wb, store, items, warehouse_id)
        except ProposalError as e:
            return f"Ошибка: {e}. Предложение не создано, ничего не изменено."
        return (f"Предложение #{aid} создано и показано владельцу с кнопками подтверждения. "
                f"ПОКА НИЧЕГО НЕ ИЗМЕНЕНО: нужно, чтобы владелец нажал ✅ в чате.\n{preview}")

    tools = [
        Tool("stocks", "остатки на складах WB: артикул, название и количество каждого товара (названия уже включены, cards для них вызывать не нужно). Аргументы: order='low' (сначала мало) или 'high', max_qty=число (показать только товары с остатком не больше этого числа, например 3 для «меньше 4 штук»), top=число строк (по умолчанию 25)", stocks),
        Tool("sales", "продажи и возвраты за период. Аргумент: days=число дней (по умолчанию 7, максимум 90)", sales),
        Tool("orders", "заказы и отмены за период. Аргумент: days=число дней (по умолчанию 7)", orders),
        Tool("prices", "текущие цены и скидки. Аргументы: search='часть артикула' (необязательно), top=число строк", prices),
        Tool("cards", "карточки товаров (название, бренд, категория). Аргументы: search='текст' (необязательно), top=число", cards),
        Tool("fbs_warehouses", "склады продавца (FBS): номер и название", fbs_warehouses),
        Tool("fbs_stocks", "текущие остатки на складе продавца (FBS) по всем товарам. Аргументы: max_qty=число — оставить только товары с остатком НЕ БОЛЬШЕ этого числа (фраза «не больше 5 штук» = max_qty=5, а не top); top=сколько строк показать (по умолчанию 30, а при max_qty — 100); warehouse_id (если складов несколько)", fbs_stocks),
        Tool("propose_stock_update", "ПРЕДЛОЖИТЬ изменение остатков FBS (ничего не меняет, ждёт подтверждения владельца). Аргументы: items=[{\"article\": \"артикул\", \"amount\": новое количество}] (до 50 товаров; вместо article можно nm; size, если у товара несколько размеров), warehouse_id (если складов несколько)", propose),
        Tool("feedbacks", "последние отзывы покупателей без ответа. Аргумент: top=число (по умолчанию 10)", feedbacks),
    ]
    return Agent("wb", "Wildberries: остатки и склад FBS (в том числе изменение остатков — через подтверждение владельца), "
                       "продажи, заказы, цены, карточки товаров, отзывы", PROMPT, tools, verify=verify_stock)
