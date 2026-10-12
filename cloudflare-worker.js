// Cloudflare Worker: (1) пересылает запросы к Telegram, Mistral, SambaNova, Cohere;
// (2) даёт боту ИИ от Cloudflare (Workers AI), распознавание речи (Whisper) и озвучку ответов (Edge TTS).
// Адрес: https://<имя>.workers.dev/<СЕКРЕТ>/<цель>/...  Без правильного секрета отвечает 404.
// Для /ai нужна привязка (Binding) Workers AI с именем переменной AI.
const SECRET = "ВСТАВЬТЕ_СЕКРЕТ";
const TARGETS = {
  tg: "https://api.telegram.org",
  mistral: "https://api.mistral.ai",
  sambanova: "https://api.sambanova.ai",
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

// Звук в base64: встроенный метод, если есть, иначе кусками (так дешевле по процессорному времени).
function toBase64(bytes) {
  if (typeof bytes.toBase64 === "function") return bytes.toBase64();
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
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

// ---------- Озвучка: Microsoft Edge TTS прямо в Worker, готовый звук уходит в Telegram ----------
// Сервер в России не может отправить в Cloudflare файл больше ~25 КБ, поэтому присылает только текст,
// а звук получает и отправляет сам Worker. Протокол перенесён из библиотеки edge-tts (Python).
const TTS_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4";
const TTS_CHROME = "143.0.3650.75";
const TTS_URL = "https://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1";

async function secMsGec(nowMs = Date.now()) {
  let ticks = Math.floor(nowMs / 1000) + 11644473600; // секунды с 1601 года
  ticks -= ticks % 300;                               // округление вниз до 5 минут
  const text = String(BigInt(ticks) * 10000000n) + TTS_TOKEN;
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
}

function tsString(d = new Date()) {
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const p = (n) => String(n).padStart(2, "0");
  return `${days[d.getUTCDay()]} ${months[d.getUTCMonth()]} ${p(d.getUTCDate())} ${d.getUTCFullYear()} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())} GMT+0000 (Coordinated Universal Time)`;
}

function randomHex(bytes) {
  return [...crypto.getRandomValues(new Uint8Array(bytes))].map((b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
}

async function edgeTTS(text, voice) {
  const clean = text.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, " ")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const url = `${TTS_URL}?TrustedClientToken=${TTS_TOKEN}&ConnectionId=${crypto.randomUUID().replaceAll("-", "")}` +
    `&Sec-MS-GEC=${await secMsGec()}&Sec-MS-GEC-Version=1-${TTS_CHROME}`;
  const resp = await fetch(url, {
    headers: {
      Upgrade: "websocket",
      Origin: "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
      Pragma: "no-cache",
      "Cache-Control": "no-cache",
      "Accept-Language": "en-US,en;q=0.9",
      Cookie: `muid=${randomHex(16)};`,
    },
  });
  const ws = resp.webSocket;
  if (!ws) throw new Error("Edge TTS не принял соединение, HTTP " + resp.status);
  ws.accept();

  return await new Promise((resolve, reject) => {
    const chunks = [];
    let done = false;
    const end = (fn, value) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try { ws.close(); } catch (e) { /* уже закрыт */ }
      fn(value);
    };
    const timer = setTimeout(() => end(reject, new Error("Edge TTS: таймаут")), 25000);
    ws.addEventListener("message", (ev) => {
      if (typeof ev.data === "string") {
        if (/Path:turn\.end/.test(ev.data)) {
          if (!chunks.length) return end(reject, new Error("Edge TTS не вернул звук"));
          const out = new Uint8Array(chunks.reduce((n, c) => n + c.length, 0));
          let pos = 0;
          for (const c of chunks) { out.set(c, pos); pos += c.length; }
          end(resolve, out);
        }
        return;
      }
      const u8 = new Uint8Array(ev.data);
      const headerLength = (u8[0] << 8) | u8[1];
      const head = new TextDecoder().decode(u8.subarray(2, 2 + headerLength));
      const body = u8.subarray(2 + headerLength);
      if (/Path:audio/.test(head) && body.length) chunks.push(body.slice());
    });
    ws.addEventListener("error", () => end(reject, new Error("Edge TTS: ошибка соединения")));
    ws.addEventListener("close", () => end(reject, new Error("Edge TTS: соединение закрыто до конца ответа")));
    ws.send(`X-Timestamp:${tsString()}\r\nContent-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n` +
      '{"context":{"synthesis":{"audio":{"metadataoptions":{"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"false"},' +
      '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n');
    ws.send(`X-RequestId:${crypto.randomUUID().replaceAll("-", "")}\r\nContent-Type:application/ssml+xml\r\n` +
      `X-Timestamp:${tsString()}Z\r\nPath:ssml\r\n\r\n` +
      `<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'><voice name='${voice}'>` +
      `<prosody pitch='+0Hz' rate='+0%' volume='+0%'>${clean}</prosody></voice></speak>`);
  });
}

async function sendTelegramAudio(token, chatId, mp3, replyTo) {
  let last = "";
  for (const [method, field] of [["sendVoice", "voice"], ["sendAudio", "audio"]]) {
    const form = new FormData();
    form.set("chat_id", String(chatId));
    if (replyTo) form.set("reply_to_message_id", String(replyTo));
    if (method === "sendAudio") form.set("title", "Ответ помощника");
    form.set(field, new Blob([mp3], { type: "audio/mpeg" }), "reply.mp3");
    const r = await (await fetch(`https://api.telegram.org/bot${token}/${method}`, { method: "POST", body: form })).json();
    if (r.ok) return method;
    last = `${method}: ${JSON.stringify(r).slice(0, 200)}`;
  }
  throw new Error("Telegram не принял звук. " + last);
}

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
    if (path === "stt-telegram") {
      // Сервер в России не может отправить в Cloudflare запись больше ~25 КБ (сеть обрывает соединение),
      // поэтому присылает только file_id: запись скачивает сам Worker (Cloudflare -> Telegram работает без ограничений).
      const { token, file_id } = await request.json();
      const info = await (await fetch(`https://api.telegram.org/bot${token}/getFile?file_id=${encodeURIComponent(file_id)}`)).json();
      if (!info.ok) return json({ error: "Telegram getFile: " + JSON.stringify(info).slice(0, 200) }, 502);
      const file = await fetch(`https://api.telegram.org/file/bot${token}/${info.result.file_path}`);
      if (!file.ok) return json({ error: "Telegram file HTTP " + file.status }, 502);
      const audio = toBase64(new Uint8Array(await file.arrayBuffer()));
      const out = await env.AI.run("@cf/openai/whisper-large-v3-turbo", { audio, language: "ru" });
      return json({ text: toText(out.text ?? out) });
    }
    if (path === "tts-telegram") {
      const { token, chat_id, text, voice, reply_to } = await request.json();
      const mp3 = await edgeTTS(String(text).slice(0, 1200), voice || "ru-RU-DmitryNeural");
      const method = await sendTelegramAudio(token, chat_id, mp3, reply_to);
      return json({ ok: true, bytes: mp3.length, method });
    }
    if (path === "stt") {
      // Звук приходит уже в base64 (готовит сервер): здесь только передаём его в Whisper.
      const audio = (await request.text()).trim();
      const out = await env.AI.run("@cf/openai/whisper-large-v3-turbo", { audio, language: "ru" });
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
