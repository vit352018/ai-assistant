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
    assert out.index("A1 (nm None): 8") < out.index("B2 (nm None): 100")  # сначала те, что заканчиваются


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
        if p == "/api/analytics/v1/stocks-report/wb-warehouses":
            assert req.method == "POST" and req.url.host == "seller-analytics-api.wildberries.ru"
            return httpx.Response(200, json={"data": {"items": STOCKS}})
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
        assert "A1 (nm None): 8 шт" in await tools["stocks"]()
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
    assert h["cohere"] == "https://x.workers.dev/SEC/cohere"
    from app.config import COHERE_MODELS
    assert len(COHERE_MODELS) >= 1


def test_transcribe_posts_audio_to_worker():
    import base64
    from app.voice import transcribe

    def handler(req):
        assert req.url.path == "/SEC/ai/stt" and req.content == base64.b64encode(b"OGG")
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
        r = LLMRouter(client, [Provider("noauth", "https://x.test/v1", "", "m")])
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
        assert out.startswith("Результаты (DuckDuckGo)")
        b._page.els = []
        empty = await b.search("x")
        assert "ничего не вернул" in empty and all(n in empty for n in ("DuckDuckGo:", "Bing:", "Mojeek:")) and "«Тест»" in empty
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


def test_extract_items_handles_shapes_and_new_fields():
    from app.wb import extract_items
    assert extract_items({"data": {"items": [1]}}) == [1] and extract_items({"data": [2]}) == [2] and extract_items([3]) == [3]
    assert extract_items({"error": "x"}) == [] and extract_items({"data": {"other": 1}}) == []
    new = [{"nmId": 7, "vendorCode": "K1", "warehouseName": "Коледино", "quantity": 4, "inWayToClient": 1},
           {"nmId": 7, "vendorCode": "K1", "warehouseName": "Казань", "quantity": 6}]
    out = agg_stocks(new, "high")
    assert "K1 (nm 7): 10 шт, к клиенту 1" in out and "цена" not in out


def test_stocks_tool_reports_unexpected_format():
    def handler(req):
        return httpx.Response(200, json={"weird": 1})

    async def go():
        tools = {t.name: t.fn for t in wb_agent.build(make_wb(handler)).tools}
        assert "формат ответа WB изменился" in await tools["stocks"]()
    run(go())


def test_transcribe_retries_on_network_error_then_succeeds():
    from app.voice import transcribe
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("")
        return httpx.Response(200, json={"text": "готово"})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return await transcribe(client, "https://x/ai", b"a")
    assert run(go()) == "готово" and calls["n"] == 2


def test_transcribe_sends_base64_text():
    import base64
    from app.voice import transcribe

    def handler(req):
        assert req.headers["content-type"].startswith("text/plain")
        assert req.content == base64.b64encode(b"OGGDATA")
        return httpx.Response(200, json={"text": "ок"})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return await transcribe(client, "https://x/ai", b"OGGDATA")
    assert run(go()) == "ок"


def test_fit_messages_shrinks_old_big_messages_and_keeps_system_and_last():
    from app.llm import fit_messages, _size
    msgs = [{"role": "system", "content": "Системная инструкция " * 20},
            {"role": "user", "content": "Задача"},
            {"role": "assistant", "content": '{"tool": "stocks"}'},
            {"role": "user", "content": "Результат: " + "данные " * 1500},
            {"role": "assistant", "content": '{"tool": "sales"}'},
            {"role": "user", "content": "Результат: " + "цифры " * 1500}]
    assert _size(msgs) > 14000
    out = fit_messages(msgs)
    assert _size(out) <= 14400
    assert out[0]["content"] == msgs[0]["content"] and out[1]["content"] == "Задача"
    assert out[3]["content"].endswith("…(обрезано)") and out[-1]["content"].startswith("Результат:")
    small = [{"role": "user", "content": "привет"}]
    assert fit_messages(small) == small


def test_router_request_body_stays_under_limit():
    sizes = []

    def handler(req):
        sizes.append(len(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}]})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        r = LLMRouter(client, [Provider("p", "https://p.test/v1", "k", "m")])
        await r.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "очень длинный текст " * 3000}])
    run(go())
    assert sizes and sizes[0] < 15000


def test_transcribe_telegram_sends_only_file_id():
    from app.voice import transcribe_telegram
    import json as _json

    def handler(req):
        assert req.url.path == "/SEC/ai/stt-telegram"
        body = _json.loads(req.content)
        assert body == {"token": "TOK", "file_id": "FID"} and len(req.content) < 200
        return httpx.Response(200, json={"text": " покажи остатки "})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return await transcribe_telegram(client, "https://x.workers.dev/SEC/ai", "TOK", "FID")
    assert run(go()) == "покажи остатки"


def test_transcribe_telegram_shows_worker_error_text():
    from app.voice import transcribe_telegram

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(400, json={"error": "decode failed"})))
        with pytest.raises(RuntimeError, match="decode failed"):
            await transcribe_telegram(client, "https://x/ai", "T", "F")
    run(go())


def test_orchestrator_hands_stray_tool_call_to_the_right_agent():
    async def tool():
        return "данные"

    agent = Agent("wb", "магазин", "x", [Tool("cards", "d", tool)])
    llm = FakeLLM(['We need cards.\n{"tool": "cards", "args": {}}', '{"final": "Названия: ..."}'])
    o = Orchestrator(llm, [agent])
    assert run(o.handle("дай названия")) == "Названия: ..."  # а не сырой JSON пользователю
    llm = FakeLLM(['{"что-то": "странное"}'])
    assert "Не понял" in run(Orchestrator(llm, [agent]).handle("?"))


def test_catalog_paginates_and_stocks_get_names():
    pages = [
        {"cards": [{"nmID": i, "vendorCode": f"V{i}", "title": f"Товар {i}", "brand": "Б"} for i in (1, 2)],
         "cursor": {"updatedAt": "t1", "nmID": 2, "total": 100}},  # total == limit: есть следующая страница
        {"cards": [{"nmID": 3, "vendorCode": "V3", "title": "Товар 3", "brand": "Б"}],
         "cursor": {"updatedAt": "t2", "nmID": 3, "total": 1}},
    ]
    seen = []

    def handler(req):
        import json as j
        if req.url.path == "/content/v2/get/cards/list":
            cursor = j.loads(req.content)["settings"]["cursor"]
            seen.append(cursor)
            return httpx.Response(200, json=pages[len(seen) - 1])
        return httpx.Response(200, json={"data": {"items": [
            {"nmId": 3, "warehouseName": "Склад WB", "quantity": 2},
            {"nmId": 1, "warehouseName": "Склад WB", "quantity": 0}]}})

    async def go():
        wb = make_wb(handler)
        names = await wb.catalog(max_pages=5)
        assert set(names) == {1, 2, 3} and seen[1].get("nmID") == 2  # вторая страница запрошена по курсору
        assert await wb.catalog() is names  # из кэша: запросов больше нет
        tools = {t.name: t.fn for t in wb_agent.build(wb).tools}
        out = await tools["stocks"](top=10)
        assert "V1 «Товар 1»: 0 шт" in out and "V3 «Товар 3»: 2 шт" in out
    run(go())


def test_stocks_work_even_if_catalog_fails():
    def handler(req):
        if req.url.path == "/content/v2/get/cards/list":
            return httpx.Response(403)
        return httpx.Response(200, json={"data": {"items": [{"nmId": 5, "quantity": 1}]}})

    async def go():
        tools = {t.name: t.fn for t in wb_agent.build(make_wb(handler)).tools}
        assert "5 (nm 5): 1 шт" in await tools["stocks"]()
    run(go())


def test_stocks_max_qty_filter_and_truncation_note():
    rows = [{"nmId": i, "quantity": i % 6} for i in range(1, 61)]
    out = agg_stocks(rows, "low", top=200, max_qty=3)
    assert "с остатком не больше 3 шт: 40" in out and "(nm 5): 5 шт" not in out
    short = agg_stocks(rows, "low", top=200, max_qty=3, max_chars=300)
    assert "не показано" in short and len(short) < 500


def test_agent_stops_on_repeated_call_and_never_shows_raw_tool_json():
    async def tool(top=1):
        return "ДАННЫЕ"

    agent = Agent("t", "тест", "x", [Tool("stocks", "d", tool)])
    # зациклился на одном вызове, но после напоминания даёт ответ
    llm = FakeLLM(['{"tool": "stocks", "args": {"top": 5}}'] * 2 + ['{"final": "Готово"}'])
    assert run(run_agent(llm, agent, "?")) == "Готово"
    # даже после напоминания продолжает вызывать инструмент — пользователь получает честное сообщение и данные
    llm = FakeLLM(['{"tool": "stocks", "args": {"top": 5}}'] * 3)
    out = run(run_agent(llm, agent, "?"))
    assert "Не удалось собрать" in out and "ДАННЫЕ" in out and '"tool"' not in out


def test_salvage_truncated_final_answer():
    from app.agents import salvage_final, _final
    raw = '{"final":"Товары:\\n1. Щит – 0 шт\\n2. Плинтус 50*70*'
    out = salvage_final(raw)
    assert out.startswith("Товары:\n1. Щит – 0 шт\n2. Плинтус 50*70*") and "не полностью" in out
    assert _final(None, raw) == out
    assert _final(None, "обычный текст") == "обычный текст"
    assert salvage_final('{"final":"Привет"}') .startswith("Привет")


def test_wants_voice_only_on_explicit_request():
    from app.voice import wants_voice
    for yes in ("ответь голосом", "озвучь ответ", "Пришли голосовое сообщение с остатками", "скажи вслух",
                "покажи остатки и ответь мне голосом"):
        assert wants_voice(yes), yes
    for no in ("покажи остатки", "что с продажами за неделю", "голосование за товар", ""):
        assert not wants_voice(no), no


def test_speak_via_worker_sends_only_text_and_reports_errors():
    from app.voice import speak_via_worker
    import json as _json

    def ok(req):
        assert req.url.path == "/SEC/ai/tts-telegram"
        body = _json.loads(req.content)
        assert body["chat_id"] == 5 and body["reply_to"] == 9 and len(req.content) < 3000 and "**" not in body["text"]
        return httpx.Response(200, json={"ok": True, "method": "sendVoice"})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(ok))
        assert (await speak_via_worker(client, "https://x/SEC/ai", "T", 5, "**Привет**, мир", "v", 9)).startswith("sendVoice")
        bad = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502, json={"error": "Edge TTS: таймаут"})))
        with pytest.raises(RuntimeError, match="Edge TTS: таймаут"):
            await speak_via_worker(bad, "https://x/SEC/ai", "T", 5, "текст", "v")
        assert await speak_via_worker(client, "https://x/SEC/ai", "T", 5, "😀", "v") == "пусто"
    run(go())


def test_degenerate_reply_is_an_error_and_next_model_answers():
    from app.llm import looks_degenerate
    assert looks_degenerate("!" * 500) and looks_degenerate("а" * 90)
    assert not looks_degenerate("Нормальный ответ с цифрами 123 и текстом. " * 5) and not looks_degenerate("!!!")

    def handler(req):
        text = "!" * 800 if req.url.host == "p0.test" else "Нормальный ответ."
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    async def go():
        r, ps = make_router(handler)
        out = await r.chat([{"role": "user", "content": "x"}])
        return out, r.last_model, ps[0].pause_until > 0
    assert run(go()) == ("Нормальный ответ.", "p1", True)


def test_articles_with_newlines_are_cleaned():
    rows = [{"date": "2026-10-09T10:00:00", "saleID": "S1", "supplierArticle": "4656760334105\n", "forPay": 10, "priceWithDisc": 12},
            {"nmId": 5, "vendorCode": "АБВ\n", "quantity": 1}]
    assert "4656760334105: 1 шт" in agg_sales(rows[:1], "2026-10-01")
    assert "АБВ (nm 5): 1 шт" in agg_stocks(rows[1:])


# ---------- этап 2: изменение остатков FBS только по подтверждению ----------
import json as _js  # noqa: E402
from app import bot as botmod  # noqa: E402
from app import stock_actions as sa  # noqa: E402
from app.actions import ActionStore  # noqa: E402
from app.wb_write import WBWriter  # noqa: E402

CARDS = {"cards": [
    {"nmID": 100, "vendorCode": "A-1", "title": "Щит 1", "brand": "Б", "sizes": [{"chrtID": 1001, "techSize": "0"}]},
    {"nmID": 200, "vendorCode": "B-2", "title": "Плинтус 2", "brand": "Б",
     "sizes": [{"chrtID": 2001, "techSize": "S"}, {"chrtID": 2002, "techSize": "M"}]}],
    "cursor": {"total": 2}}


class FakeWB:
    """Подставной WB: хранит остатки, считает запросы на запись, проверяет токены."""

    def __init__(self, warehouses=None, noop=False, put_status=204):
        self.fbs = {1001: 0, 2001: 5, 2002: 7}
        self.puts, self.noop, self.put_status = [], noop, put_status
        self.warehouses = [{"id": 7, "name": "Мой склад"}] if warehouses is None else warehouses

    def __call__(self, req):
        p = req.url.path
        if p == "/content/v2/get/cards/list":
            return httpx.Response(200, json=CARDS)
        if p == "/api/v3/warehouses":
            return httpx.Response(200, json=self.warehouses)
        if p.startswith("/api/v3/stocks/") and req.method == "POST":
            ids = _js.loads(req.content)["chrtIds"]
            return httpx.Response(200, json={"stocks": [{"chrtId": i, "amount": self.fbs[i]} for i in ids if i in self.fbs]})
        if p.startswith("/api/v3/stocks/") and req.method == "PUT":
            self.puts.append((req.headers["authorization"], _js.loads(req.content)))
            if self.put_status >= 400:
                return httpx.Response(self.put_status, json={"error": "bad"})
            if not self.noop:
                for s in _js.loads(req.content)["stocks"]:
                    self.fbs[s["chrtId"]] = s["amount"]
            return httpx.Response(204)
        return httpx.Response(404)


def stage2(api=None):
    api = api or FakeWB()
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return api, WB(client, "READ"), WBWriter(client, "WRITE"), ActionStore()


def test_store_claims_once_expires_and_cancels():
    st = ActionStore()
    a = st.create("k", {"x": 1}, "превью")
    assert st.pop_new() == [a] and st.pop_new() == []
    assert st.claim(a) == ("k", {"x": 1}) and st.claim(a) is None  # выполнить можно только один раз
    st.finish(a, True, "ok")
    assert st.get(a)["status"] == "done" and st.recent(5)[0]["id"] == a
    old = ActionStore(ttl=-1)
    b = old.create("k", {}, "p")
    assert old.claim(b) is None and old.get(b)["status"] == "expired"
    c = st.create("k", {}, "p")
    assert st.cancel(c) and st.claim(c) is None and not st.cancel(c)


def test_propose_changes_nothing_and_shows_old_and_new():
    async def go():
        api, wb, _, st = stage2()
        aid, preview = await sa.propose_stock_update(wb, st, [{"article": "a-1", "amount": 20}])
        assert "A-1 «Щит 1»: 0 → 20 шт" in preview and "Мой склад" in preview
        assert api.puts == [] and api.fbs[1001] == 0  # ничего не записано
        a = st.get(aid)
        assert a["status"] == "pending" and a["payload"]["items"][0]["chrtId"] == 1001 and a["payload"]["warehouse_id"] == 7
    run(go())


def test_propose_validation_errors():
    async def bad(items, warehouses=None, wid=None):
        api, wb, _, st = stage2(FakeWB(warehouses=warehouses))
        with pytest.raises(sa.ProposalError) as e:
            await sa.propose_stock_update(wb, st, items, wid)
        assert api.puts == [] and st.recent(5) == []
        return str(e.value)

    async def go():
        assert "не найден в каталоге" in await bad([{"article": "НЕТ", "amount": 1}])
        for wrong in (-1, True, 2.5, "много", None, 10**9):
            assert "не подходит" in await bad([{"article": "A-1", "amount": wrong}]), wrong
        assert "размеры: S, M" in await bad([{"article": "B-2", "amount": 1}])
        assert "указан дважды" in await bad([{"article": "A-1", "amount": 1}, {"nm": 100, "amount": 2}])
        assert "не больше 50" in await bad([{"article": "A-1", "amount": 1}] * 51)
        assert "непустой список" in await bad([])
        two = [{"id": 7, "name": "Один"}, {"id": 8, "name": "Два"}]
        assert "нужно указать склад" in await bad([{"article": "A-1", "amount": 1}], warehouses=two)
        assert "склад 99 не найден" in await bad([{"article": "A-1", "amount": 1}], warehouses=two, wid=99)
        # размер указан — всё в порядке, склад выбран явно
        api, wb, _, st = stage2(FakeWB(warehouses=two))
        aid, preview = await sa.propose_stock_update(wb, st, [{"article": "B-2", "size": "M", "amount": 9}], 8)
        assert "7 → 9 шт" in preview and st.get(aid)["payload"]["items"][0]["chrtId"] == 2002
    run(go())


def test_propose_warns_about_suspicious_numbers():
    async def go():
        _, wb, _, st = stage2()
        _, preview = await sa.propose_stock_update(wb, st, [{"article": "A-1", "amount": 1200}])
        assert preview.count("⚠️") == 2  # и большой скачок, и очень большое число
    run(go())


def test_execute_sends_exact_body_with_write_token_and_verifies():
    async def go():
        api, wb, writer, st = stage2()
        aid, _ = await sa.propose_stock_update(wb, st, [{"article": "A-1", "amount": 20}, {"nm": 200, "size": "S", "amount": 0}])
        kind, payload = st.claim(aid)
        ok, text = await sa.execute_stock_update(writer, payload)
        assert ok and "проверено: 2 из 2" in text
        assert api.puts == [("WRITE", {"stocks": [{"chrtId": 1001, "amount": 20}, {"chrtId": 2001, "amount": 0}]})]
    run(go())


def test_execute_detects_silent_204_that_changed_nothing(monkeypatch):
    async def fast(_): pass
    monkeypatch.setattr(sa.asyncio, "sleep", fast)

    async def go():
        api, wb, writer, st = stage2(FakeWB(noop=True))  # WB «принял» запрос (204), но ничего не изменил
        aid, _ = await sa.propose_stock_update(wb, st, [{"article": "A-1", "amount": 20}])
        ok, text = await sa.execute_stock_update(writer, st.claim(aid)[1])
        assert not ok and "не совпали" in text and "ожидалось 20, у WB сейчас 0" in text
    run(go())


def test_button_executes_once_and_cancel_executes_nothing():
    async def go():
        api, wb, writer, st = stage2()
        a1, _ = await sa.propose_stock_update(wb, st, [{"article": "A-1", "amount": 20}])
        first = await botmod.decide(st, writer, f"ok:{a1}")
        second = await botmod.decide(st, writer, f"ok:{a1}")  # повторное нажатие
        assert "проверено" in first and "уже выполнено" in second and len(api.puts) == 1
        a2, _ = await sa.propose_stock_update(wb, st, [{"article": "A-1", "amount": 30}])
        assert "Отменено" in await botmod.decide(st, writer, f"no:{a2}")
        assert "истекло" in await botmod.decide(st, writer, f"ok:{a2}") and len(api.puts) == 1
        assert api.fbs[1001] == 20 and st.get(a1)["status"] == "done" and st.get(a2)["status"] == "cancelled"
        assert "не настроен" in await botmod.decide(st, None, f"ok:{a2}")
    run(go())


def test_wb_error_on_write_is_reported_and_logged():
    async def go():
        api, wb, writer, st = stage2(FakeWB(put_status=400))
        aid, _ = await sa.propose_stock_update(wb, st, [{"article": "A-1", "amount": 20}])
        text = await botmod.decide(st, writer, f"ok:{aid}")
        assert "Ошибка при выполнении" in text and "проверьте остатки в кабинете" in text
        assert st.get(aid)["status"] == "failed"
    run(go())


def test_only_owner_can_press_buttons_and_buttons_vanish():
    class Msg:
        def __init__(self): self.sent = []
        async def reply_text(self, text, reply_markup=None): self.sent.append((text, reply_markup))

    class Q:
        def __init__(self, uid, data):
            self.from_user, self.data, self.message, self.calls = types.SimpleNamespace(id=uid), data, Msg(), []
        async def answer(self, text=None, show_alert=False): self.calls.append(("answer", text))
        async def edit_message_reply_markup(self, reply_markup=None): self.calls.append(("markup", reply_markup))

    async def go():
        api, wb, writer, st = stage2()
        aid, _ = await sa.propose_stock_update(wb, st, [{"article": "A-1", "amount": 20}])
        botmod.cfg = types.SimpleNamespace(owner_id=1)
        ctx = types.SimpleNamespace(application=types.SimpleNamespace(bot_data={"store": st, "writer": writer}))
        stranger = Q(2, f"ok:{aid}")
        await botmod.on_action(types.SimpleNamespace(callback_query=stranger), ctx)
        assert api.puts == [] and st.get(aid)["status"] == "pending" and stranger.message.sent == []
        owner = Q(1, f"ok:{aid}")
        await botmod.on_action(types.SimpleNamespace(callback_query=owner), ctx)
        assert ("markup", None) in owner.calls and len(api.puts) == 1 and "проверено" in owner.message.sent[0][0]
    run(go())


def test_proposal_is_shown_with_two_buttons():
    class Msg:
        def __init__(self): self.sent = []
        async def reply_text(self, text, reply_markup=None): self.sent.append((text, reply_markup))

    async def go():
        _, wb, _, st = stage2()
        aid, _ = await sa.propose_stock_update(wb, st, [{"article": "A-1", "amount": 20}])
        msg = Msg()
        ctx = types.SimpleNamespace(application=types.SimpleNamespace(bot_data={"store": st}))
        await botmod.send_proposals(types.SimpleNamespace(message=msg), ctx)
        text, kb = msg.sent[0]
        assert "ничего не изменится" in text and "0 → 20 шт" in text
        assert [b.callback_data for b in kb.inline_keyboard[0]] == [f"ok:{aid}", f"no:{aid}"]
        await botmod.send_proposals(types.SimpleNamespace(message=msg), ctx)
        assert len(msg.sent) == 1  # второй раз не показываем
    run(go())


def test_agent_tools_propose_without_executing_and_need_write_token():
    async def go():
        api, wb, _, st = stage2()
        with_store = {t.name: t.fn for t in wb_agent.build(wb, st).tools}
        out = await with_store["propose_stock_update"](items=[{"article": "A-1", "amount": 5}])
        assert "ПОКА НИЧЕГО НЕ ИЗМЕНЕНО" in out and api.puts == [] and len(st.new_ids) == 1
        out = await with_store["propose_stock_update"](items='[{"article": "НЕТ", "amount": 5}]')
        assert "Предложение не создано" in out and len(st.new_ids) == 1
        assert "A-1 «Щит 1»: 0 шт" in await with_store["fbs_stocks"](max_qty=0)
        assert "7: Мой склад" in await with_store["fbs_warehouses"]()
        no_store = {t.name: t.fn for t in wb_agent.build(wb, None).tools}
        assert "WB_WRITE_TOKEN" in await no_store["propose_stock_update"](items=[{"article": "A-1", "amount": 5}])
    run(go())


def test_model_can_never_reach_the_write_client():
    # инструменты агента получают только клиента на чтение и хранилище предложений
    import inspect
    assert "writer" not in inspect.signature(wb_agent.build).parameters
    assert not hasattr(WB, "update_stocks") and hasattr(WBWriter, "update_stocks")


def test_search_falls_back_to_next_engine_and_decodes_bing_links():
    import base64 as b64
    target = "https://shop.ru/p?id=1"
    enc = "a1" + b64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert br.decode_bing(f"https://www.bing.com/ck/a?!&&p=x&u={enc}&ntb=1") == target
    assert br.decode_bing("https://example.com/") == "https://example.com/"

    async def go():
        b = fake_browser()
        visited = []
        orig = b._page.goto

        async def goto(url, **kw):
            visited.append(url.split("/")[2])
            return await orig(url, **kw)

        async def evaluate(js):  # первый поисковик «не вернул ничего», второй (Bing) — вернул
            return [] if len(visited) == 1 else [{"t": "Магазин", "h": f"https://www.bing.com/ck/a?u={enc}", "s": "описание"}]
        b._page.goto, b._page.evaluate = goto, evaluate
        out = await b.search("кружка")
        assert visited == ["html.duckduckgo.com", "www.bing.com"]
        assert out.startswith("Результаты (Bing)") and target in out
    run(go())


def test_router_sends_stock_change_to_wb_agent_and_describes_capabilities():
    from app.wb_agent import build as build_wb
    from app.orchestrator import ROUTER
    desc = build_wb(FakeWB and make_wb(lambda r: httpx.Response(200, json={}))).description
    assert "только чтение" not in desc and "изменение остатков" in desc
    assert "Wildberries" in ROUTER and "не утверждай" in ROUTER.lower() or "Не утверждай" in ROUTER
    llm = FakeLLM(['{"agent": "wb", "task": "установить остаток"}', '{"final": "Предложение создано"}'])
    agent = Agent("wb", desc, "x", [])
    o = Orchestrator(llm, [agent])
    assert run(o.handle("установи остаток артикула X равным 20")) == "Предложение создано"


def test_agent_never_asks_for_current_stock():
    from app.wb_agent import PROMPT
    assert "Никогда не проси владельца сообщить текущий остаток" in PROMPT
    assert "Для предложения нужны только" in PROMPT


def test_model_claiming_proposal_without_tool_is_called_out():
    import asyncio as _a
    import types as _t
    class M:
        def __init__(self): self.sent = []; self.message_id = 1
        async def reply_text(self, text, reply_markup=None): self.sent.append(text)
    async def go(answer, store_ids):
        st = ActionStore()
        if store_ids:
            st.create("k", {}, "p")
        class L:
            last_model = "m"
            async def chat(self, *a, **k): return ""
        orch = types.SimpleNamespace(handle=lambda text: _a.sleep(0, result=answer))
        msg = M()
        upd = _t.SimpleNamespace(message=msg, effective_chat=_t.SimpleNamespace(id=1))
        ctx = _t.SimpleNamespace(bot=_t.SimpleNamespace(send_chat_action=lambda *a, **k: _a.sleep(0)),
                                 application=_t.SimpleNamespace(bot_data={"orch": orch, "llm": L(), "store": st,
                                                                         "client": None, "always_voice": False}))
        botmod.cfg = _t.SimpleNamespace(owner_id=1, hosts=lambda: {"ai": ""}, tts_voice="v", bot_token="t")
        await botmod.respond(upd, ctx, "x", voice_in=False)
        return msg.sent
    fake = run(go("Предложение #abc подготовлено", store_ids=False))
    assert any("НЕ создано" in x for x in fake)
    real = run(go("Предложение подготовлено", store_ids=True))
    assert not any("НЕ создано" in x for x in real)


def test_agent_is_forced_to_call_propose_when_it_only_claims_it():
    from app.wb_agent import build as build_wb
    api, wb, _, st = stage2()
    llm = FakeLLM([
        '{"final": "Предложение подготовлено. Пока ничего не изменено."}',       # заявила, но не вызвала
        '{"tool": "propose_stock_update", "args": {"items": [{"article": "A-1", "amount": 20}]}}',
        '{"final": "Предложение создано, ждёт ✅"}',
    ])
    agent = build_wb(wb, st)
    out = run(run_agent(llm, agent, "установи остаток A-1 равным 20"))
    assert out == "Предложение создано, ждёт ✅" and len(st.new_ids) == 1 and api.puts == []
    assert "НЕ вызывался" in llm.seen[1][-1]["content"]  # замечание действительно попало в диалог


def test_no_nudge_when_tool_was_called_or_no_claim():
    from app.wb_agent import build as build_wb
    api, wb, _, st = stage2()
    agent = build_wb(wb, st)
    llm = FakeLLM(['{"final": "Остатков на складе нет."}'])
    assert run(run_agent(llm, agent, "?")) == "Остатков на складе нет." and len(llm.seen) == 1
    llm = FakeLLM(['{"tool": "propose_stock_update", "args": {"items": [{"article": "A-1", "amount": 20}]}}',
                   '{"final": "Предложение подготовлено."}'])
    assert run(run_agent(llm, agent, "?")) == "Предложение подготовлено." and len(st.new_ids) == 1
