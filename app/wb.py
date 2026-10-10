import time
from collections import defaultdict

HOSTS = {
    "stats": "https://statistics-api.wildberries.ru",
    "prices": "https://discounts-prices-api.wildberries.ru",
    "content": "https://content-api.wildberries.ru",
    "feedbacks": "https://feedbacks-api.wildberries.ru",
    "common": "https://common-api.wildberries.ru",
    "analytics": "https://seller-analytics-api.wildberries.ru",
}


class WBError(Exception):
    pass


class WB:
    """Тонкий клиент WB API. Только чтение: методов, меняющих данные, здесь нет."""

    def __init__(self, client, token):
        self.client = client
        self.token = token
        self._cache = {}

    async def get(self, host, path, params=None, ttl=0):
        return await self._req("GET", host, path, params=params, ttl=ttl)

    async def post_read(self, host, path, body, params=None, ttl=0):
        # POST используется WB для некоторых запросов на чтение (например, список карточек)
        return await self._req("POST", host, path, params=params, body=body, ttl=ttl)

    async def _req(self, method, host, path, params=None, body=None, ttl=0):
        key = (method, host, path, tuple(sorted((params or {}).items())), repr(body))
        hit = self._cache.get(key)
        if ttl and hit and time.time() - hit[0] < ttl:
            return hit[1]
        if not self.token:
            raise WBError("WB-токен не задан в .env")
        r = await self.client.request(
            method, HOSTS[host] + path, params=params, json=body,
            headers={"Authorization": self.token}, timeout=60,
        )
        if r.status_code == 401:
            raise WBError("WB не принял токен (401)")
        if r.status_code == 403:
            raise WBError("у токена нет доступа к этому разделу (403)")
        if r.status_code == 429:
            raise WBError("лимит запросов WB (429), повторите через минуту")
        if r.status_code >= 400:
            raise WBError(f"WB вернул {r.status_code}: {r.text[:150]}")
        data = r.json()
        if ttl:
            self._cache[key] = (time.time(), data)
        return data

    async def seller(self):
        data = await self.get("common", "/api/v1/seller-info", ttl=60)
        return data.get("tradeMark") or data.get("name") or "?"


def _day(s):
    return (s or "")[:10]


def _money(x):
    return f"{x:,.0f}".replace(",", " ")


def extract_items(resp):
    """Список строк из ответа WB: бывает {"data": {"items": [...]}}, {"data": [...]} или просто список."""
    if isinstance(resp, list):
        return resp
    data = resp.get("data", resp) if isinstance(resp, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "rows", "stocks"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _first(r, *keys, default=None):
    for k in keys:
        if r.get(k) not in (None, ""):
            return r[k]
    return default


def agg_stocks(rows, order="low", top=25):
    """Остатки по артикулам. Названия полей берём с запасом: формат ответа WB менялся."""
    by = defaultdict(lambda: {"qty": 0, "to": 0, "back": 0, "price": None, "disc": None, "wh": set(), "nm": None})
    for r in rows:
        nm = _first(r, "nmId", "nmID")
        a = by[str(_first(r, "vendorCode", "supplierArticle", default=nm))]
        a["qty"] += _first(r, "quantity", default=0)
        a["to"] += _first(r, "inWayToClient", default=0)
        a["back"] += _first(r, "inWayFromClient", default=0)
        a["price"] = _first(r, "Price", "price", default=a["price"])
        a["disc"] = _first(r, "Discount", "discount", default=a["disc"])
        a["nm"] = nm
        if r.get("warehouseName"):
            a["wh"].add(r["warehouseName"])
    if not by:
        return "Остатков на складах WB нет."
    items = sorted(by.items(), key=lambda kv: kv[1]["qty"], reverse=(order == "high"))
    lines = [f"Артикулов: {len(by)}, штук на складах WB: {sum(v['qty'] for v in by.values())}"]
    for art, v in items[:top]:
        extra = f", цена {v['price']}₽, скидка {v['disc']}%" if v["price"] is not None else ""
        lines.append(f"{art} (nm {v['nm']}): {v['qty']} шт на {len(v['wh'])} складах "
                     f"(к клиенту {v['to']}, возвраты {v['back']}){extra}")
    return "\n".join(lines)


def agg_sales(rows, since, top=15):
    arts, days = defaultdict(lambda: [0, 0.0]), defaultdict(lambda: [0, 0.0])
    sales = returns = 0
    revenue = pay = 0.0
    for r in rows:
        d = _day(r.get("date"))
        if d < since:
            continue
        sign = -1 if str(r.get("saleID", "")).startswith("R") else 1
        fp = float(r.get("forPay") or 0) * sign
        revenue += float(r.get("priceWithDisc") or 0) * sign
        pay += fp
        sales += sign > 0
        returns += sign < 0
        a = arts[r.get("supplierArticle") or str(r.get("nmId"))]
        a[0] += sign
        a[1] += fp
        days[d][0] += sign
        days[d][1] += fp
    if not sales and not returns:
        return f"С {since} продаж нет."
    lines = [
        f"Продажи с {since}: {sales} шт, возвратов {returns}; "
        f"выручка (цена со скидкой продавца) {_money(revenue)}₽; к перечислению {_money(pay)}₽ (с учётом возвратов)",
        "Топ артикулов (шт, к перечислению):",
    ]
    for art, (n, p) in sorted(arts.items(), key=lambda kv: -kv[1][1])[:top]:
        lines.append(f"{art}: {n} шт, {_money(p)}₽")
    lines.append("По дням: " + "; ".join(f"{d}: {n} шт / {_money(p)}₽" for d, (n, p) in sorted(days.items())[-14:]))
    return "\n".join(lines)


def agg_orders(rows, since, top=15):
    arts, days = defaultdict(lambda: [0, 0.0]), defaultdict(int)
    ok = cancelled = 0
    total = 0.0
    for r in rows:
        d = _day(r.get("date"))
        if d < since:
            continue
        if r.get("isCancel"):
            cancelled += 1
            continue
        ok += 1
        price = float(r.get("priceWithDisc") or 0)
        total += price
        a = arts[r.get("supplierArticle") or str(r.get("nmId"))]
        a[0] += 1
        a[1] += price
        days[d] += 1
    if not ok and not cancelled:
        return f"С {since} заказов нет."
    lines = [f"Заказы с {since}: {ok} шт на {_money(total)}₽, отмен {cancelled}", "Топ артикулов (шт, сумма):"]
    for art, (n, s) in sorted(arts.items(), key=lambda kv: -kv[1][0])[:top]:
        lines.append(f"{art}: {n} шт, {_money(s)}₽")
    lines.append("По дням: " + "; ".join(f"{d}: {n}" for d, n in sorted(days.items())[-14:]))
    return "\n".join(lines)
