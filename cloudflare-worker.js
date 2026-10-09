// Cloudflare Worker: (1) пересылает запросы к Telegram и OpenRouter, (2) даёт боту ИИ от Cloudflare (Workers AI).
// Адрес: https://<имя>.workers.dev/<СЕКРЕТ>/<tg|or|ai>/...  Без правильного секрета отвечает 404.
// Для /ai в настройках Worker должна быть привязка (Binding) «Workers AI» с именем переменной AI.
const SECRET = "ВСТАВЬТЕ_СЕКРЕТ";
const TARGETS = {
  tg: "https://api.telegram.org",
  or: "https://openrouter.ai",
};

const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...headers } });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean); // [секрет, цель, ...остальное]
    if (parts[0] !== SECRET) return new Response("not found", { status: 404 });
    if (parts[1] === "ai") return handleAI(request, env, parts.slice(2).join("/"));
    if (!TARGETS[parts[1]]) return new Response("not found", { status: 404 });
    const rest = "/" + parts.slice(2).join("/");
    return fetch(new Request(TARGETS[parts[1]] + rest + url.search, request));
  },
};

async function handleAI(request, env, path) {
  if (request.method !== "POST") return new Response("not found", { status: 404 });
  if (!env.AI) return json({ error: "К Worker не подключён Workers AI (Settings → Bindings → AI)" }, 500);
  try {
    if (path === "v1/chat/completions") {
      const b = await request.json();
      const out = await env.AI.run(b.model, {
        messages: b.messages,
        max_tokens: b.max_tokens || 1000,
        temperature: b.temperature ?? 0.2,
      });
      const text = typeof out === "string" ? out : (out.response ?? out.choices?.[0]?.message?.content ?? "");
      return json({ choices: [{ message: { role: "assistant", content: text } }] });
    }
    if (path === "stt") {
      const bytes = new Uint8Array(await request.arrayBuffer());
      let bin = "";
      for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      const out = await env.AI.run("@cf/openai/whisper-large-v3-turbo", { audio: btoa(bin), language: "ru" });
      return json({ text: out.text || "" });
    }
  } catch (e) {
    const msg = String(e);
    if (/4006|daily free allocation/i.test(msg)) return json({ error: msg }, 429, { "Retry-After": "3600" });
    if (/3040|capacity/i.test(msg)) return json({ error: msg }, 429, { "Retry-After": "10" });
    return json({ error: msg }, 502);
  }
  return new Response("not found", { status: 404 });
}
