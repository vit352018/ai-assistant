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


def test_hosts_via_proxy():
    from app.config import Config
    base = dict(bot_token="t", owner_id=1, proxy_base="https://x.workers.dev/SEC/", wb_token="w",
                cf_models=("m",), tts_voice="v")
    h = Config(**base).hosts()
    assert h["tg"] == "https://x.workers.dev/SEC/tg" and h["ai"] == "https://x.workers.dev/SEC/ai"
    assert h["mistral"] == "https://x.workers.dev/SEC/mistral"
    assert h["llm7"] == "https://x.workers.dev/SEC/llm7"
    assert h["cohere"] == "https://x.workers.dev/SEC/cohere"
    from app.config import COHERE_MODELS
    assert len(COHERE_MODELS) >= 1


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


def test_router_remembers_which_model_answered():
    def handler(req):
        if req.url.host == "p0.test":
            return httpx.Response(429, headers={"retry-after": "30"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}]})

    async def go():
        r, _ = make_router(handler)
        await r.chat([{"role": "user", "content": "x"}])
        return r.last_model
    assert run(go()) == "p1"


# ---------- браузер ----------
from app import browser as br  # noqa: E402


def test_host_is_public_blocks_internal_addresses():
    for bad in ("127.0.0.1", "localhost", "10.1.2.3", "192.168.0.5", "169.254.169.254", "::1", "100.64.0.1"):
        assert not br.host_is_public(bad), bad
    assert br.host_is_public("8.8.8.8")
    assert not br.host_is_public("нет-такого-сайта.invalid")


def test_url_problem():
    assert br.url_problem("https://example.com/a") is None
    for bad in ("file:///etc/passwd", "ftp://x.ru", "javascript:alert(1)", "https://user:pw@x.ru", "https://"):
        assert br.url_problem(bad), bad


def item(**kw):
    base = dict(id=0, tag="a", type="", role="", text="", name="", href="", inForm=False, formGet=False)
    base.update(kw)
    return base


def test_click_policy():
    assert br.click_block_reason(item(tag="a", text="Купить")) is None  # ссылки — навигация
    assert br.click_block_reason(item(tag="button", inForm=True, type="submit", text="Найти"))
    assert br.click_block_reason(item(tag="button", text="Оформить заказ"))
    assert br.click_block_reason(item(tag="input", type="submit"))
    assert br.click_block_reason(item(tag="button", text="Следующая страница")) is None
    assert br.click_block_reason(item(tag="button", text="Принять cookies")) is None


def test_type_policy():
    assert br.type_block_reason(item(tag="input", type="text", text="Поиск"), submit=False) is None
    assert br.type_block_reason(item(tag="input", type="password"), False)
    assert br.type_block_reason(item(tag="input", type="text", name="cardnumber cc-number"), False)
    assert br.type_block_reason(item(tag="button"), False)
    assert br.type_block_reason(item(tag="input", type="text", formGet=False), submit=True)  # POST-форма
    assert br.type_block_reason(item(tag="input", type="search"), submit=True) is None
    assert br.type_block_reason(item(tag="input", type="text", formGet=True), submit=True) is None


def test_decode_ddg_and_clean_text():
    assert br.decode_ddg("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=x") == "https://example.com/a"
    assert br.decode_ddg("https://x.ru/") == "https://x.ru/"
    assert br.clean_text("  а   б \n\n\n в ") == "а б\nв"


class FakeLoc:
    def __init__(self, page): self.page = page
    @property
    def first(self): return self
    async def click(self, timeout=0): self.page.log.append("click")
    async def fill(self, text): self.page.log.append(("fill", text))
    async def press(self, key): self.page.log.append(("press", key))


class FakePage:
    def __init__(self):
        self.url, self.log, self.els = "https://example.com/", [], []
        self.body = "Привет. " * 1000

    async def goto(self, url, **kw):
        self.url = url
        return types.SimpleNamespace(status=200)
    async def wait_for_load_state(self, *a, **kw): pass
    async def title(self): return "Тест"
    async def inner_text(self, sel): return self.body
    async def evaluate(self, js): return self.els
    def locator(self, sel):
        self.log.append(sel)
        return FakeLoc(self)


import types  # noqa: E402


def fake_browser(els=()):
    b = br.Browser(allow_private=True)
    b._page = FakePage()
    b._page.els = list(els)
    b._ctx = types.SimpleNamespace(pages=[b._page])
    return b


def test_browser_open_read_paging_and_untrusted_marker():
    async def go():
        b = fake_browser()
        out = await b.open("https://example.com/")
        assert "HTTP 200" in out and "данные из интернета" in out and "offset=3500" in out
        assert "offset=" not in await b.read(offset=b._text and len(b._text) - 100)
        with pytest.raises(br.BrowserError):
            await b.open("file:///etc/passwd")
    run(go())


def test_browser_blocks_form_submit_but_allows_link_and_get_search():
    els = [item(id=0, tag="a", text="Каталог", href="https://example.com/c"),
           item(id=1, tag="button", type="submit", inForm=True, text="Оплатить"),
           item(id=2, tag="input", type="search", text="Поиск", inForm=True, formGet=True),
           item(id=3, tag="input", type="password", inForm=True)]

    async def go():
        b = fake_browser(els)
        listing = await b.elements()
        assert "ссылка «Каталог»" in listing and "→ https://example.com/c" in listing
        assert "Страница" in await b.click(0)
        with pytest.raises(br.BrowserError, match="заблокирован"):
            await b.click(1)
        assert "Текст введён" in await b.type(2, "кружка")
        assert "Страница" in await b.type(2, "кружка", submit="true")
        with pytest.raises(br.BrowserError, match="пароли"):
            await b.type(3, "secret")
        with pytest.raises(br.BrowserError, match="нет элемента"):
            await b.click(99)
        assert ("press", "Enter") in b._page.log and "click" in b._page.log
    run(go())


def test_browser_search_formats_results():
    async def go():
        b = fake_browser()
        b._page.els = [{"t": "Кружки", "h": "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fshop.ru%2F", "s": "Купить кружки"}]
        out = await b.search("кружка")
        assert "1. Кружки" in out and "https://shop.ru/" in out
        b._page.els = []
        assert "ничего не вернул" in await b.search("x")
    run(go())


def test_guard_aborts_internal_and_images_but_allows_public():
    class Req:
        def __init__(self, url, rt="document"): self.url, self.resource_type = url, rt

    class Route:
        def __init__(self, req): self.request, self.done = req, None
        async def abort(self): self.done = "abort"
        async def continue_(self): self.done = "ok"

    async def go():
        b = br.Browser()  # защита включена
        res = {}
        for url, rt in [("http://127.0.0.1:8000/admin", "document"), ("http://169.254.169.254/", "xhr"),
                        ("https://8.8.8.8/x", "document"), ("https://8.8.8.8/i.png", "image"),
                        ("data:text/plain,hi", "other")]:
            r = Route(Req(url, rt))
            await b._guard(r)
            res[url] = r.done
        return res
    res = run(go())
    assert res["http://127.0.0.1:8000/admin"] == "abort" and res["http://169.254.169.254/"] == "abort"
    assert res["https://8.8.8.8/x"] == "ok" and res["https://8.8.8.8/i.png"] == "abort" and res["data:text/plain,hi"] == "ok"


def test_browser_refuses_to_start_without_memory(monkeypatch):
    monkeypatch.setattr(br, "mem_available_mb", lambda: 50)

    async def go():
        with pytest.raises(br.BrowserError, match="мало свободной памяти"):
            await br.Browser(allow_private=True).open("https://example.com/")
    run(go())


def test_browser_agent_has_expected_tools_and_more_steps():
    from app.browser_agent import build
    a = build(br.Browser())
    assert [t.name for t in a.tools] == ["search", "open", "read", "elements", "click", "type"] and a.max_steps == 8
