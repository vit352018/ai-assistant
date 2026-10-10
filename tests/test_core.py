import asyncio
import httpx
import pytest

from app.agents import Agent, Tool, run_agent
from app.jsonutil import extract_json, split_message
from app.llm import LLMError, LLMRouter, Provider
from app.orchestrator import Orchestrator
from app.voice import clean_for_speech
from app.wb import WB, WBError, agg_orders, agg_sales, agg_stocks
from app import wb_agent


def run(coro):
    return asyncio.run(coro)


# ---------- вспомогательное ----------
def test_extract_json_variants():
    assert extract_json('{"final": "ок"}') == {"final": "ок"}
    assert extract_json('Вот:\n```json\n{"tool": "x", "args": {"a": 1}}\n```') == {"tool": "x", "args": {"a": 1}}
    assert extract_json("просто текст") is None
    assert extract_json('мусор {не json} потом {"a": 2}') == {"a": 2}


def test_split_message():
    text = "\n".join(["строка"] * 2000)
    parts = split_message(text)
    assert all(len(p) <= 4000 for p in parts) and len(parts) > 1
    assert "\n".join(parts).count("строка") == 2000


def test_clean_for_speech():
    out = clean_for_speech("**Итого** 🎉 смотри https://x.ru/abc #тег")
    assert "*" not in out and "http" not in out and "🎉" not in out and "Итого" in out


# ---------- роутер моделей ----------
def make_router(handler, n=2):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    providers = [Provider(f"p{i}", f"https://p{i}.test/v1", "k", "m") for i in range(n)]
    return LLMRouter(client, providers), providers


def test_router_falls_back_on_429_and_pauses():
    calls = []

    def handler(req):
        calls.append(req.url.host)
        if req.url.host == "p0.test":
            return httpx.Response(429, headers={"retry-after": "30"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "привет"}}]})

    async def go():
        r, ps = make_router(handler)
        assert await r.chat([{"role": "user", "content": "x"}]) == "привет"
        assert ps[0].pause_until > 0
        await r.chat([{"role": "user", "content": "x"}])  # p0 на паузе — к нему не ходим
    run(go())
    assert calls == ["p0.test", "p1.test", "p1.test"]


def test_router_all_fail():
    async def go():
        r, _ = make_router(lambda req: httpx.Response(500, text="boom"))
        with pytest.raises(LLMError):
            await r.chat([{"role": "user", "content": "x"}])
    run(go())


def test_router_auto_picks_free_openrouter_model():
    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "paid/model", "pricing": {"prompt": "0.1", "completion": "0.2"}},
                {"id": "meta-llama/llama-3.3-70b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}},
            ]})
        assert req.content and b"llama-3.3-70b-instruct:free" in req.content
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}]})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        r = LLMRouter(client, [Provider("openrouter", "https://openrouter.ai/api/v1", "k", "")])
        assert await r.chat([{"role": "user", "content": "x"}]) == "ок"
    run(go())


# ---------- цикл агента и оркестратор ----------
class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def chat(self, messages, **kw):
        self.seen.append(list(messages))
        return self.replies.pop(0)


def test_agent_calls_tool_then_final():
    got = {}

    async def tool(days=7):
        got["days"] = days
        return "10 продаж"

    agent = Agent("t", "тест", "Ты тест.", [Tool("sales", "продажи", tool)])
    llm = FakeLLM(['{"tool": "sales", "args": {"days": 3}}', '{"final": "Продано 10"}'])
    assert run(run_agent(llm, agent, "сколько продаж")) == "Продано 10"
    assert got["days"] == 3
    assert "10 продаж" in llm.seen[1][-1]["content"]  # результат инструмента вернулся модели


def test_agent_survives_bad_tool_and_bad_args():
    async def tool():
        raise RuntimeError("сломано")

    agent = Agent("t", "тест", "x", [Tool("a", "d", tool)])
    llm = FakeLLM(['{"tool": "нет такого"}', '{"tool": "a", "args": {"лишний": 1}}', '{"tool": "a"}', '{"final": "ок"}'])
    assert run(run_agent(llm, agent, "?")) == "ок"
    assert "нет инструмента" in llm.seen[1][-1]["content"]
    assert "Ошибка аргументов" in llm.seen[2][-1]["content"]
    assert "сломано" in llm.seen[3][-1]["content"]


def test_agent_step_limit_forces_final():
    async def tool():
        return "x"

    agent = Agent("t", "тест", "x", [Tool("a", "d", tool)])
    llm = FakeLLM(['{"tool": "a"}'] * 2 + ['{"final": "хватит"}'])
    assert run(run_agent(llm, agent, "?", max_steps=2)) == "хватит"


def test_orchestrator_routes_and_answers_directly():
    async def tool():
        return "данные"

    agent = Agent("wb", "магазин", "x", [Tool("a", "d", tool)])
    llm = FakeLLM([
        '{"agent": "wb", "task": "покажи данные"}', '{"tool": "a"}', '{"final": "Вот данные"}',
        '{"answer": "Привет!"}',
    ])
    o = Orchestrator(llm, [agent])
    assert run(o.handle("что с остатками")) == "Вот данные"
    assert run(o.handle("привет")) == "Привет!"
    assert "Вот данные" in llm.seen[-1][-1]["content"]  # история попала в следующий запрос


# ---------- WB ----------
STOCKS = [
    {"supplierArticle": "A1", "quantity": 5, "inWayToClient": 1, "inWayFromClient": 0, "Price": 1000, "Discount": 10},
    {"supplierArticle": "A1", "quantity": 3, "inWayToClient": 0, "inWayFromClient": 2, "Price": 1000, "Discount": 10},
    {"supplierArticle": "B2", "quantity": 100, "Price": 500, "Discount": 0},
]


def test_agg_stocks():
    out = agg_stocks(STOCKS, "low")
    assert "Артикулов: 2" in out and "штук на складах WB: 108" in out
    assert out.index("A1: 8") < out.index("B2: 100")  # сначала те, что заканчиваются


def test_agg_sales_counts_returns_and_filters_dates():
    rows = [
        {"date": "2026-10-08T10:00:00", "saleID": "S1", "supplierArticle": "A1", "forPay": 700, "priceWithDisc": 900},
        {"date": "2026-10-08T11:00:00", "saleID": "S2", "supplierArticle": "A1", "forPay": 700, "priceWithDisc": 900},
        {"date": "2026-10-09T09:00:00", "saleID": "R3", "supplierArticle": "A1", "forPay": 700, "priceWithDisc": 900},
        {"date": "2026-09-01T09:00:00", "saleID": "S4", "supplierArticle": "OLD", "forPay": 999, "priceWithDisc": 999},
    ]
    out = agg_sales(rows, "2026-10-01")
    assert "2 шт, возвратов 1" in out and "A1: 1 шт, 700₽" in out and "OLD" not in out
    assert agg_sales([], "2026-10-01") == "С 2026-10-01 продаж нет."


def test_agg_orders():
    rows = [
        {"date": "2026-10-08", "supplierArticle": "A1", "priceWithDisc": 500},
        {"date": "2026-10-08", "supplierArticle": "A1", "priceWithDisc": 500, "isCancel": True},
    ]
    out = agg_orders(rows, "2026-10-01")
    assert "1 шт на 500₽, отмен 1" in out


def make_wb(handler, token="tok"):
    return WB(httpx.AsyncClient(transport=httpx.MockTransport(handler)), token)


def test_wb_sends_token_caches_and_maps_errors():
    seen = []

    def handler(req):
        seen.append(req.headers["authorization"])
        return httpx.Response(200, json={"tradeMark": "Мой магазин"})

    async def go():
        wb = make_wb(handler)
        assert await wb.seller() == "Мой магазин"
        await wb.seller()  # из кэша
    run(go())
    assert seen == ["tok"]

    for code, word in [(401, "токен"), (403, "доступа"), (429, "лимит")]:
        async def bad(code=code):
            wb = make_wb(lambda req: httpx.Response(code))
            await wb.seller()
        with pytest.raises(WBError, match=word):
            run(bad())
    with pytest.raises(WBError, match="не задан"):
        run(make_wb(lambda r: httpx.Response(200, json={}), token="").seller())


def test_wb_agent_tools_end_to_end_with_fake_api():
    def handler(req):
        p = req.url.path
        if p == "/api/v1/supplier/stocks":
            return httpx.Response(200, json=STOCKS)
        if p == "/api/v2/list/goods/filter":
            return httpx.Response(200, json={"data": {"listGoods": [
                {"nmID": 1, "vendorCode": "A1", "discount": 10, "sizes": [{"price": 1000, "discountedPrice": 900}]}]}})
        if p == "/content/v2/get/cards/list":
            assert req.method == "POST"
            return httpx.Response(200, json={"cards": [{"nmID": 1, "vendorCode": "A1", "title": "Кружка", "brand": "Б", "subjectName": "Кружки"}]})
        if p == "/api/v1/feedbacks":
            return httpx.Response(200, json={"data": {"countUnanswered": 1, "feedbacks": [
                {"productValuation": 2, "text": "Плохо", "createdDate": "2026-10-08T10:00:00Z",
                 "productDetails": {"productName": "Кружка", "supplierArticle": "A1"}, "userName": "СЕКРЕТ"}]}})
        return httpx.Response(404)

    async def go():
        agent = wb_agent.build(make_wb(handler))
        tools = {t.name: t.fn for t in agent.tools}
        assert "A1: 8 шт" in await tools["stocks"]()
        assert "итого 900₽" in await tools["prices"](search="a1")
        assert "Кружка" in await tools["cards"](search="кружка")
        fb = await tools["feedbacks"]()
        assert "2★" in fb and "СЕКРЕТ" not in fb  # имена покупателей не попадают к модели
    run(go())


def test_bot_module_imports():
    import app.bot  # noqa: F401


def test_hosts_via_proxy_and_openrouter_override():
    from app.config import Config
    base = dict(bot_token="t", owner_id=1, proxy_base="https://x.workers.dev/SEC/", wb_token="w",
                openrouter_key="o", cf_models=("m",), openrouter_model="", tts_voice="v")
    h = Config(**base).hosts()
    assert h["tg"] == "https://x.workers.dev/SEC/tg" and h["ai"] == "https://x.workers.dev/SEC/ai" and h["or"] == "https://x.workers.dev/SEC/or"
    assert Config(**base, openrouter_base="https://openrouter.ai/").hosts()["or"] == "https://openrouter.ai"
    assert h["mistral"] == "https://x.workers.dev/SEC/mistral"
    assert h["llm7"] == "https://x.workers.dev/SEC/llm7"


def test_transcribe_posts_audio_to_worker():
    from app.voice import transcribe

    def handler(req):
        assert req.url.path == "/SEC/ai/stt" and req.content == b"OGG"
        return httpx.Response(200, json={"text": " покажи остатки "})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert await transcribe(client, "https://x.workers.dev/SEC/ai", b"OGG") == "покажи остатки"
    run(go())


def test_shutdown_without_client_does_not_crash():
    import types
    from app.bot import post_shutdown
    run(post_shutdown(types.SimpleNamespace(bot_data={})))


def test_content_as_list_and_bad_format():
    from app.llm import _text, LLMError
    assert _text([{"type": "text", "text": "при"}, {"type": "text", "text": "вет"}]) == "привет"
    assert _text("  ок ") == "ок"
    with pytest.raises(LLMError):
        _text({"unexpected": "dict"})


def test_router_retries_after_short_busy_pause():
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}]})

    async def go():
        r, _ = make_router(handler, n=1)
        return await r.chat([{"role": "user", "content": "x"}])
    assert run(go()) == "ок"


def test_provider_without_key_sends_no_auth_header():
    def handler(req):
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}]})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        r = LLMRouter(client, [Provider("llm7", "https://x.test/v1", "", "gpt-4o-mini")])
        assert await r.chat([{"role": "user", "content": "x"}]) == "ок"
    run(go())
