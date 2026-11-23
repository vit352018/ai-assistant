"""Реклама WB: только чтение и проверки лимитов. Изменений здесь нет: запись появится позже,
после проверки методов на ваших кампаниях, и только через подтверждение владельца."""
import datetime as dt
import time

BASE = "https://advert-api.wildberries.ru"

# Лимиты владельца (по договорённости)
DAILY_TOTAL_CAP = 3000      # ₽ на всю рекламу в день
PER_CAMPAIGN_CAP = 1000     # ₽ на одну кампанию в день
BID_UP_MAX = 0.10           # ставка вверх не больше чем на 10% за раз
BID_DOWN_MAX = 0.20         # ставка вниз не больше чем на 20% за раз

STATUS = {-1: "удаляется", 4: "готова к запуску", 7: "завершена", 8: "отклонена", 9: "активна", 11: "на паузе"}


class AdsError(Exception):
    pass


def status_name(code):
    return STATUS.get(code, f"статус {code}")


def check_bid_change(old, new):
    """None, если изменение ставки в пределах лимитов, иначе текст причины."""
    if old is None or old <= 0:
        return "текущая ставка неизвестна, изменение не проверить"
    if new <= 0:
        return "ставка должна быть больше нуля"
    change = (new - old) / old
    if change > BID_UP_MAX + 1e-9:
        return f"ставка вверх на {change:.0%}: больше лимита {BID_UP_MAX:.0%} за раз"
    if change < -BID_DOWN_MAX - 1e-9:
        return f"ставка вниз на {-change:.0%}: больше лимита {BID_DOWN_MAX:.0%} за раз"
    return None


def check_budget(daily_now, new_daily, others_total):
    """Проверка дневного бюджета: на кампанию и на всю рекламу вместе. others_total — сумма остальных кампаний."""
    if new_daily < 0:
        return "бюджет не может быть отрицательным"
    if new_daily > PER_CAMPAIGN_CAP:
        return f"бюджет {new_daily} ₽ больше лимита {PER_CAMPAIGN_CAP} ₽ на кампанию"
    if others_total + new_daily > DAILY_TOTAL_CAP:
        return (f"сумма бюджетов станет {others_total + new_daily} ₽, это больше общего лимита "
                f"{DAILY_TOTAL_CAP} ₽ в день")
    return None


class AdsClient:
    """Клиент рекламного API только на чтение. Методов изменения здесь нет."""

    def __init__(self, client, token):
        self.client = client
        self.token = token

    async def get(self, path, **params):
        if not self.token:
            raise AdsError("токен рекламы (WB_ADS_TOKEN) не задан")
        r = await self.client.get(BASE + path, params=params or None,
                                  headers={"Authorization": self.token}, timeout=60)
        if r.status_code == 401:
            raise AdsError("WB не принял токен рекламы (401)")
        if r.status_code == 403:
            raise AdsError("у токена рекламы нет доступа к этому методу (403)")
        if r.status_code == 429:
            raise AdsError("лимит запросов WB к рекламе (429), повторите через минуту")
        if r.status_code >= 400:
            raise AdsError(f"WB вернул {r.status_code}: {r.text[:150]}")
        return r.json() if r.content else {}

    async def campaigns(self):
        """Все кампании: [{id, status, name, bid_type}]."""
        data = await self.get("/api/advert/v2/adverts")
        out = []
        for a in data.get("adverts") or []:
            out.append({"id": a.get("id"), "status": a.get("status"),
                        "name": (a.get("settings") or {}).get("name") or "",
                        "bid_type": a.get("bid_type")})
        return out

    async def budget(self, advert_id):
        data = await self.get("/adv/v1/budget", id=advert_id)
        return float(data.get("total") or 0)

    async def balance(self):
        return await self.get("/adv/v1/balance")

    async def settings(self):
        """Общие настройки рекламы: минимальная ставка, шаг, минимальный дневной лимит."""
        return await self.get("/api/advert/v1/config")

    async def stats(self, ids, days=7):
        if not ids:
            return []
        today = dt.date.today()
        data = await self.get("/adv/v3/fullstats", ids=",".join(str(i) for i in ids[:50]),
                              beginDate=(today - dt.timedelta(days=days)).isoformat(), endDate=today.isoformat())
        return data if isinstance(data, list) else []


def summarize_stats(rows):
    """Итоги по кампаниям: показы, клики, расход, заказы. Пустой список — честно говорим, что данных нет."""
    if not rows:
        return "за выбранный период статистики по кампаниям нет (показов и расходов не было)."
    views = clicks = orders = 0
    spent = 0.0
    for r in rows:
        views += int(r.get("views") or 0)
        clicks += int(r.get("clicks") or 0)
        orders += int(r.get("orders") or 0)
        spent += float(r.get("sum") or 0)
    ctr = f"{clicks / views:.2%}" if views else "—"
    return f"Кампаний со статистикой: {len(rows)}. Показы: {views}, клики: {clicks} (CTR {ctr}), заказы: {orders}, расход: {spent:.0f} ₽."
