"""Только чтение: какие методы рекламы WB отвечают токену «Продвижение». Ничего не меняет.
Запуск: cd /opt/assistant && set -a && . ./.env && set +a && venv/bin/python scripts/ads_probe.py"""
import datetime as dt
import json
import os
import time

import httpx

BASE = "https://advert-api.wildberries.ru"
TOKEN = os.environ.get("WB_ADS_TOKEN", "")
if not TOKEN:
    raise SystemExit("WB_ADS_TOKEN не задан в .env")


def first_list(obj, depth=0):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and depth < 3:
        for v in obj.values():
            found = first_list(v, depth + 1)
            if found is not None:
                return found
    return None


def show(name, r):
    print(f"== {name}: HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        print("   не JSON:", r.text[:200])
        return None
    if r.status_code >= 400:
        print("   ", json.dumps(data, ensure_ascii=False)[:300])
        return None
    print("    верхний уровень:", list(data)[:10] if isinstance(data, dict) else f"список из {len(data)}")
    rows = first_list(data)
    if rows:
        print(f"    строк: {len(rows)}; поля первой:", sorted(rows[0]) if isinstance(rows[0], dict) else rows[0])
    return data


today = dt.date.today()
with httpx.Client(timeout=60, headers={"Authorization": TOKEN}) as c:
    def get(name, path, **params):
        r = c.get(BASE + path, params=params or None)
        time.sleep(1.2)  # у WB лимиты на запросы
        return show(name, r)

    get("ping", "/ping")
    counts = get("список кампаний по типам и статусам (promotion/count)", "/adv/v1/promotion/count")
    get("количество медиакампаний (v1/count)", "/adv/v1/count")
    get("конфигурация продвижения (v1/config)", "/api/advert/v1/config")
    get("баланс (v1/balance)", "/adv/v1/balance")
    get("кампании v2 (может быть устаревшим)", "/api/advert/v2/adverts")

    ids = []
    if isinstance(counts, dict):
        for group in counts.get("adverts") or []:
            for a in group.get("advert_list") or []:
                ids.append((a.get("advertId"), group.get("status"), group.get("type")))
    print(f"\nНайдено кампаний: {len(ids)}; по статусам:", sorted({(s, t) for _, s, t in ids}))
    for advert_id, status, kind in ids[:2]:
        get(f"бюджет кампании {advert_id} (статус {status}, тип {kind})", "/adv/v1/budget", id=advert_id)
    if ids:
        time.sleep(1)
        get("статистика за 7 дней (v3/fullstats)", "/adv/v3/fullstats", ids=",".join(str(i[0]) for i in ids[:3]),
            beginDate=(today - dt.timedelta(days=7)).isoformat(), endDate=today.isoformat())
print("\nГотово. Ничего не изменялось.")
