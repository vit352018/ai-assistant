import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .jsonutil import extract_json

log = logging.getLogger("agent")
MSK = dt.timezone(dt.timedelta(hours=3))
MAX_TOOL_CHARS = 4600

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
    verify: object = None  # необязательно: verify(answer, called_tools) -> текст замечания или None


def today():
    return dt.datetime.now(MSK).date()


def salvage_final(raw):
    """Ответ вида {"final": "текст..." оборвался на лимите длины, JSON не закрыт: достаём текст как есть."""
    m = re.search(r'"final"\s*:\s*"', raw)
    if not m:
        return None
    body = raw[m.end():].rstrip()
    if body.endswith('"}'):
        body = body[:-2]
    for _ in range(12):  # отрезаем недописанный хвост (обрыв посреди \n, \uXXXX и т.п.)
        try:
            text = json.loads('"' + body + '"')
            break
        except ValueError:
            body = body[:-1]
    else:
        text = body.replace("\\n", "\n").replace('\\"', '"')
    return text.rstrip() + "\n\n…(ответ мог прийти не полностью; если список короче ожидаемого — попросите показать остаток)"


def _final(data, raw):
    if data:
        return str(data.get("final") or data.get("answer") or raw)
    return salvage_final(raw) or raw


async def run_agent(llm, agent, task, max_steps=None):
    """Цикл агента: модель просит инструмент -> получает результат -> ... -> final."""
    max_steps = max_steps or agent.max_steps
    tools = {t.name: t for t in agent.tools}
    tool_text = "\n".join(f"- {t.name}: {t.description}" for t in agent.tools)
    system = f"{agent.prompt}\nСегодня {today()} (МСК).\n\nИнструменты:\n{tool_text}\n\n{PROTOCOL}"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    seen, last_result = set(), ""
    called, nudged = set(), False
    for _ in range(max_steps):
        reply = await llm.chat(msgs)
        data = extract_json(reply)
        if not data or "tool" not in data:
            answer = _final(data, reply)
            nudge = agent.verify(answer, called) if agent.verify and not nudged else None
            if nudge:  # модель заявила о действии, которого не было — один раз просим сделать его по-настоящему
                nudged = True
                msgs.append({"role": "assistant", "content": reply})
                msgs.append({"role": "user", "content": nudge})
                continue
            return answer
        name, args = data["tool"], data.get("args")
        args = args if isinstance(args, dict) else {}
        msgs.append({"role": "assistant", "content": reply})
        key = (name, str(sorted(args.items())))
        if key in seen:  # модель зациклилась на одном и том же вызове — данные уже есть
            msgs.append({"role": "user", "content": 'Этот вызов уже был, результат выше. Не вызывай инструменты, '
                                                    'дай итоговый ответ сейчас: {"final": "..."}'})
            break
        seen.add(key)
        tool = tools.get(name)
        called.add(name)
        log.info("%s вызывает %s %s", agent.name, name, str(args)[:200])
        if tool is None:
            result = f"Ошибка: нет инструмента {name}. Доступны: {', '.join(tools)}"
        else:
            try:
                result = await tool.fn(**args)
            except TypeError as e:
                result = f"Ошибка аргументов: {e}"
            except Exception as e:  # noqa: BLE001 — отдаём ошибку модели, пусть объяснит
                result = f"Ошибка инструмента: {e}"
        last_result = str(result)
        msgs.append({"role": "user", "content": f"Результат {name}:\n{last_result[:MAX_TOOL_CHARS]}"})
    else:
        msgs.append({"role": "user", "content": 'Шагов больше нет. Дай итоговый ответ сейчас: {"final": "..."}'})
    reply = await llm.chat(msgs)
    data = extract_json(reply)
    if data and "tool" in data and "final" not in data:  # модель так и не дала ответ — не показываем сырой вызов
        tail = f"\n\nПоследние полученные данные:\n{last_result[:1500]}" if last_result else ""
        return "Не удалось собрать итоговый ответ. Попробуйте уточнить вопрос." + tail
    return _final(data, reply)
