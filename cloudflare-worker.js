// Cloudflare Worker: (1) пересылает запросы к Telegram, OpenRouter, Mistral, SambaNova;
// (2) даёт боту ИИ от Cloudflare (Workers AI) и распознавание речи (Whisper).
// Адрес: https://<имя>.workers.dev/<СЕКРЕТ>/<цель>/...  Без правильного секрета отвечает 404.
// Для /ai нужна привязка (Binding) Workers AI с именем переменной AI.
const SECRET = "ВСТАВЬТЕ_СЕКРЕТ";
const TARGETS = {
  tg: "https://api.telegram.org",
  or: "https://openrouter.ai",
  mistral: "https://api.mistral.ai",
  sambanova: "https://api.sambanova.ai",
  llm7: "https://api.llm7.io",
  cohere: "https://api.cohere.com",
};

const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...headers } });

// Ответ Workers AI бывает строкой, объектом или списком частей — приводим к тексту.
function toText(v) {
  if (typeof v === "string") return v;
  if (Array.isArray(v)) return v.map((p) => (typeof p === "string" ? p : p?.text ?? "")).join("");
  if (v && typeof v === "object") return v.text ?? v.content ?? v.response ?? "";
  return "";
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);
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
      const text = toText(out.response ?? out.choices?.[0]?.message?.content ?? out);
      return json({ choices: [{ message: { role: "assistant", content: text } }] });
    }
    if (path === "stt") {
      const bytes = new Uint8Array(await request.arrayBuffer());
      let bin = "";
      for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      const out = await env.AI.run("@cf/openai/whisper-large-v3-turbo", { audio: btoa(bin), language: "ru" });
      return json({ text: toText(out.text ?? out) });
    }
  } catch (e) {
    const msg = String(e);
    if (/4006|daily free allocation/i.test(msg)) return json({ error: msg }, 429, { "Retry-After": "3600" });
    if (/3040|capacity|overloaded/i.test(msg)) return json({ error: msg }, 503, { "Retry-After": "5" });
    return json({ error: msg }, 502);
  }
  return new Response("not found", { status: 404 });
}
