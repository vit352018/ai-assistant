import collections

from .agents import run_agent, today
from .jsonutil import extract_json

ROUTER = """Ты — оркестратор личного ИИ-помощника владельца. Сегодня {today}.
Агенты-специалисты:
{agents}

Ответь ТОЛЬКО одним JSON-объектом:
- нужны данные или действия агента: {{"agent": "имя", "task": "самодостаточная формулировка задачи по-русски"}}
- можно ответить самому (разговор, общие вопросы): {{"answer": "ответ"}}
Если не уверен, нужны ли данные магазина — выбирай агента. Пиши по-русски, обычным текстом без markdown."""


class Orchestrator:
    """Выбирает агента под задачу и возвращает его ответ. Новый агент = ещё один элемент в списке agents."""

    def __init__(self, llm, agents, history_len=8):
        self.llm = llm
        self.agents = {a.name: a for a in agents}
        self.history = collections.deque(maxlen=history_len)

    def _context(self):
        if not self.history:
            return ""
        lines = [f"{who}: {text[:300]}" for who, text in self.history]
        return "Недавний диалог:\n" + "\n".join(lines) + "\n\n"

    async def handle(self, text):
        ctx = self._context()
        agents = "\n".join(f"- {a.name}: {a.description}" for a in self.agents.values())
        reply = await self.llm.chat([
            {"role": "system", "content": ROUTER.format(today=today(), agents=agents)},
            {"role": "user", "content": f"{ctx}Новое сообщение: {text}"},
        ])
        data = extract_json(reply)
        if data and data.get("agent") in self.agents:
            task = f"{ctx}Задача: {data.get('task') or text}"
            answer = await run_agent(self.llm, self.agents[data["agent"]], task)
        elif data and "answer" in data:
            answer = str(data["answer"])
        else:
            answer = reply
        self.history.append(("Владелец", text))
        self.history.append(("Помощник", answer))
        return answer
