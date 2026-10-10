import datetime as dt
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .jsonutil import extract_json

MSK = dt.timezone(dt.timedelta(hours=3))
MAX_TOOL_CHARS = 4000

PROTOCOL = """Формат ответа — ТОЛЬКО один JSON-объект, без текста вокруг:
  вызвать инструмент: {"tool": "имя", "args": {"аргумент": значение}}
  дать ответ пользователю: {"final": "текст ответа"}
Правила: числа и факты бери только из результатов инструментов, ничего не выдумывай; если инструмент вернул ошибку — честно скажи об этом. Пиши по-русски, обычным текстом без markdown (без ** и #), коротко и по делу."""


@dataclass
class Tool:
    name: str
    description: str  # что делает и какие у него аргументы
    fn: Callable[..., Awaitable[str]]


@dataclass
class Agent:
    name: str
    description: str  # по нему оркестратор решает, кому поручить задачу
    prompt: str
    tools: list = field(default_factory=list)
    max_steps: int = 5


def today():
    return dt.datetime.now(MSK).date()


def _final(data, raw):
    if data:
        return str(data.get("final") or data.get("answer") or raw)
    return raw


async def run_agent(llm, agent, task, max_steps=None):
    """Цикл агента: модель просит инструмент -> получает результат -> ... -> final."""
    max_steps = max_steps or agent.max_steps
    tools = {t.name: t for t in agent.tools}
    tool_text = "\n".join(f"- {t.name}: {t.description}" for t in agent.tools)
    system = f"{agent.prompt}\nСегодня {today()} (МСК).\n\nИнструменты:\n{tool_text}\n\n{PROTOCOL}"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    for _ in range(max_steps):
        reply = await llm.chat(msgs)
        data = extract_json(reply)
        if not data or "tool" not in data:
            return _final(data, reply)
        name, args = data["tool"], data.get("args")
        args = args if isinstance(args, dict) else {}
        msgs.append({"role": "assistant", "content": reply})
        tool = tools.get(name)
        if tool is None:
            result = f"Ошибка: нет инструмента {name}. Доступны: {', '.join(tools)}"
        else:
            try:
                result = await tool.fn(**args)
            except TypeError as e:
                result = f"Ошибка аргументов: {e}"
            except Exception as e:  # noqa: BLE001 — отдаём ошибку модели, пусть объяснит
                result = f"Ошибка инструмента: {e}"
        msgs.append({"role": "user", "content": f"Результат {name}:\n{str(result)[:MAX_TOOL_CHARS]}"})
    msgs.append({"role": "user", "content": 'Шагов больше нет. Дай итоговый ответ сейчас: {"final": "..."}'})
    reply = await llm.chat(msgs)
    return _final(extract_json(reply), reply)
