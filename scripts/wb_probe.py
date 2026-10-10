"""Проверка адресов WB API настоящим токеном: печатает статус и устройство ответа каждого метода."""
import datetime as dt
import json
import os
import time

import httpx

H = {"Authorization": os.environ["WB_TOKEN"]}
DAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()


def first_list(obj, depth=0):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and depth < 3:
        for v in obj.values():
            found = first_list(v, depth + 1)
            if found is not None:
                return found
    return None


def show(name, r, keys_only=False):
    print(f"== {name}: HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        print("   не JSON:", r.text[:200])
        return
    if r.status_code >= 400:
        print("   ", json.dumps(data, ensure_ascii=False)[:300])
        return
    print("    верхний уровень:", list(data)[:8] if isinstance(data, dict) else f"список из {len(data)}")
    rows = first_list(data)
    if rows:
        print(f"    строк в списке: {len(rows)}; поля первой:", sorted(rows[0]) if isinstance(rows[0], dict) else rows[0])
        if not keys_only:
            print("    первая строка:", json.dumps(rows[0], ensure_ascii=False)[:400])


with httpx.Client(timeout=60, headers=H) as c:
    show("seller-info", c.get("https://common-api.wildberries.ru/api/v1/seller-info"))
    show("остатки (analytics)", c.post("https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses",
                                       json={"limit": 3, "offset": 0}))
    show("продажи (statistics)", c.get("https://statistics-api.wildberries.ru/api/v1/supplier/sales", params={"dateFrom": DAY}))
    show("заказы (statistics)", c.get("https://statistics-api.wildberries.ru/api/v1/supplier/orders", params={"dateFrom": DAY}))
    show("цены", c.get("https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter", params={"limit": 3, "offset": 0}))
    show("карточки", c.post("https://content-api.wildberries.ru/content/v2/get/cards/list", params={"locale": "ru"},
                            json={"settings": {"sort": {"ascending": False}, "filter": {"withPhoto": -1}, "cursor": {"limit": 3}}}))
    show("отзывы", c.get("https://feedbacks-api.wildberries.ru/api/v1/feedbacks",
                         params={"isAnswered": "false", "take": 2, "skip": 0, "order": "dateDesc"}), keys_only=True)
    time.sleep(0.1)
