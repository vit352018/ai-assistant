"""Изменение остатков FBS. Два разных шага:
  propose_stock_update — только готовит предложение (читает данные токеном на чтение, ничего не меняет);
  execute_stock_update — выполняет его токеном на запись, и только после подтверждения владельца кнопкой."""
import asyncio

KIND = "wb_stock_update"
MAX_ITEMS = 50
MAX_AMOUNT = 100000
BIG_JUMP = 500  # увеличение больше чем на столько штук отмечаем предупреждением


class ProposalError(Exception):
    pass


def _norm(s):
    return " ".join(str(s).split()).lower()


def _amount(value):
    if isinstance(value, bool) or value is None:
        raise ProposalError(f"количество «{value}» не подходит: нужно целое число")
    try:
        f = float(str(value).replace(",", ".").strip())
    except ValueError:
        raise ProposalError(f"количество «{value}» не подходит: нужно целое число")
    if f != int(f) or not 0 <= f <= MAX_AMOUNT:
        raise ProposalError(f"количество «{value}» не подходит: целое число от 0 до {MAX_AMOUNT}")
    return int(f)


def resolve(catalog, items):
    """[{article | nm, amount, size?}] -> [{chrtId, nm, label, amount}]; ошибки говорят, что именно не так."""
    by_art = {}
    for nm, v in catalog.items():
        if v.get("vendorCode"):
            by_art.setdefault(_norm(v["vendorCode"]), []).append(nm)
    rows, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            raise ProposalError("каждый элемент списка должен быть объектом {article, amount}")
        amount = _amount(it.get("amount"))
        art, nm = it.get("article") or it.get("vendorCode"), it.get("nm") or it.get("nmId") or it.get("nmID")
        if nm is None and art is not None:
            found = by_art.get(_norm(art), [])
            if len(found) != 1:
                raise ProposalError(f"артикул «{art}» " + ("не найден в каталоге" if not found else "встречается несколько раз"))
            nm = found[0]
        try:
            nm = int(nm)
        except (TypeError, ValueError):
            raise ProposalError("у каждого товара нужен article (артикул) или nm")
        card = catalog.get(nm)
        if not card:
            raise ProposalError(f"товар nm {nm} не найден в каталоге")
        sizes = card.get("sizes") or []
        if len(sizes) == 1:
            size = sizes[0]
        else:
            size = next((s for s in sizes if it.get("size") and _norm(s["techSize"]) == _norm(it["size"])), None)
            if not size:
                raise ProposalError(f"у «{card['vendorCode']}» размеры: {', '.join(s['techSize'] or '-' for s in sizes) or 'нет'}; укажите size")
        if size["chrtId"] in seen:
            raise ProposalError(f"«{card['vendorCode']}» указан дважды")
        seen.add(size["chrtId"])
        label = f"{' '.join(card['vendorCode'].split())} «{' '.join(card['title'].split())[:45]}»"
        rows.append({"chrtId": size["chrtId"], "nm": nm, "label": label, "amount": amount})
    return rows


async def pick_warehouse(wb, warehouse_id):
    whs = await wb.warehouses()
    if warehouse_id not in (None, ""):
        wh = next((w for w in whs if str(w.get("id")) == str(warehouse_id)), None)
        if not wh:
            raise ProposalError(f"склад {warehouse_id} не найден. Склады: " + "; ".join(f"{w.get('id')} {w.get('name')}" for w in whs))
        return wh
    if len(whs) == 1:
        return whs[0]
    raise ProposalError("нужно указать склад (warehouse_id). Склады: " + "; ".join(f"{w.get('id')} {w.get('name')}" for w in whs))


async def propose_stock_update(wb, store, items, warehouse_id=None):
    if not isinstance(items, list) or not items:
        raise ProposalError("нужен непустой список items: [{article, amount}]")
    if len(items) > MAX_ITEMS:
        raise ProposalError(f"за один раз не больше {MAX_ITEMS} товаров")
    catalog = await wb.catalog()
    wh = await pick_warehouse(wb, warehouse_id)
    rows = resolve(catalog, items)
    current = await wb.fbs_stocks(wh["id"], [r["chrtId"] for r in rows])
    lines, warns = [], []
    for r in rows:
        r["old"] = current.get(r["chrtId"])
        old = "нет данных" if r["old"] is None else r["old"]
        lines.append(f"• {r['label']}: {old} → {r['amount']} шт")
        if r["old"] is None:
            warns.append(f"⚠️ {r['label']}: WB не вернул текущий остаток — прежнее значение неизвестно, откатить автоматически не получится")
        if r["old"] is not None and r["amount"] - r["old"] > BIG_JUMP:
            warns.append(f"⚠️ {r['label']}: увеличение на {r['amount'] - r['old']} шт — проверьте число")
        if r["amount"] >= 1000:
            warns.append(f"⚠️ {r['label']}: остаток {r['amount']} шт — это не опечатка?")
    preview = (f"Обновить остатки на складе «{wh.get('name')}» (id {wh['id']}):\n" + "\n".join(lines)
               + ("\n" + "\n".join(warns) if warns else ""))
    payload = {"warehouse_id": wh["id"], "warehouse": wh.get("name"), "items": rows}
    aid = store.create(KIND, payload, preview)
    return aid, preview


async def execute_stock_update(writer, payload):
    """Выполнить подтверждённое предложение и проверить результат чтением. Вернёт (успех, текст)."""
    wid, items = payload["warehouse_id"], payload["items"]
    await writer.update_stocks(wid, [{"chrtId": r["chrtId"], "amount": r["amount"]} for r in items])
    # WB не проверяет названия полей и на любую ошибку в теле отвечает 204, поэтому сверяем результат чтением
    bad = []
    for attempt in range(2):
        now = await writer.fbs_stocks(wid, [r["chrtId"] for r in items])
        bad = [r for r in items if now.get(r["chrtId"]) != r["amount"]]
        if not bad:
            break
        await asyncio.sleep(2)
    if not bad:
        return True, f"✅ Применено и проверено: {len(items)} из {len(items)}"
    lines = [f"• {r['label']}: ожидалось {r['amount']}, у WB сейчас {now.get(r['chrtId'], 'нет данных')}" for r in bad]
    return False, "⚠️ WB принял запрос, но остатки не совпали:\n" + "\n".join(lines) + "\nПроверьте в кабинете WB."
