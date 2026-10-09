// Cloudflare Worker: пересылает запросы к Telegram, Groq и OpenRouter.
// Адрес: https://<имя>.workers.dev/<СЕКРЕТ>/<tg|groq|or>/...  Без правильного секрета отвечает 404.
const SECRET = "ВСТАВЬТЕ_СЕКРЕТ";
const TARGETS = {
  tg: "https://api.telegram.org",
  groq: "https://api.groq.com",
  or: "https://openrouter.ai",
};

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean); // [секрет, цель, ...остальное]
    if (parts[0] !== SECRET || !TARGETS[parts[1]]) {
      return new Response("not found", { status: 404 });
    }
    const rest = "/" + parts.slice(2).join("/");
    return fetch(new Request(TARGETS[parts[1]] + rest + url.search, request));
  },
};
