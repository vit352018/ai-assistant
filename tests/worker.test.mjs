// Проверка Worker на подставных ответах: node tests/worker.test.mjs
// Печатает Sec-MS-GEC, чтобы сверить его с Python-библиотекой edge-tts (см. tests/test_worker_gec.py).
import fs from "node:fs";
import assert from "node:assert/strict";

const FIXED = 1791750000000;
const src = fs.readFileSync(new URL("../cloudflare-worker.js", import.meta.url), "utf8").replace("ВСТАВЬТЕ_СЕКРЕТ", "SEC");
fs.writeFileSync("/tmp/worker_under_test.mjs", src);
const worker = (await import("/tmp/worker_under_test.mjs")).default;
Date.now = () => FIXED;

const enc = (s) => new TextEncoder().encode(s);
function audioFrame(bytes) {
  const head = enc("X-RequestId:abc\r\nContent-Type:audio/mpeg\r\nX-Timestamp:t\r\nPath:audio\r\n");
  const out = new Uint8Array(2 + head.length + bytes.length);
  out[0] = head.length >> 8; out[1] = head.length & 255;
  out.set(head, 2); out.set(bytes, 2 + head.length);
  return out.buffer;
}

class FakeWS {
  constructor(frames) { this.l = {}; this.sent = []; this.frames = frames; }
  accept() {}
  close() {}
  addEventListener(t, f) { (this.l[t] ||= []).push(f); }
  send(m) {
    this.sent.push(m);
    if (m.includes("Path:ssml")) setTimeout(() => {
      for (const f of this.frames) (this.l.message || []).forEach((h) => h({ data: f }));
      (this.l.message || []).forEach((h) => h({ data: "X-Timestamp:t\r\nPath:turn.end\r\n\r\n{}" }));
    }, 0);
  }
}

function setup({ voiceFails = false, noAudio = false, edge403 = false, googleFails = false } = {}) {
  const log = { urls: [], forms: [], ws: null };
  const frames = noAudio ? [] : [audioFrame(new Uint8Array(30000).fill(1)), audioFrame(new Uint8Array(25000).fill(2))];
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url); log.urls.push(u);
    if (u.startsWith("https://translate.google.com")) {
      log.google = (log.google || []).concat(decodeURIComponent(u.split("&q=")[1]));
      return googleFails ? new Response("no", { status: 429 }) : new Response(new Uint8Array(1000).fill(3));
    }
    if (u.startsWith("https://speech.platform.bing.com")) {
      if (edge403) return new Response("Forbidden by policy", { status: 403 });
      assert.equal(init.headers.Upgrade, "websocket");
      assert.match(init.headers.Origin, /^chrome-extension:/);
      log.ws = new FakeWS(frames);
      return { webSocket: log.ws, status: 101 };
    }
    if (u.includes("api.telegram.org")) {
      const method = u.split("/").pop(); log.forms.push([method, init.body]);
      const ok = !(voiceFails && method === "sendVoice");
      return new Response(JSON.stringify(ok ? { ok: true } : { ok: false, description: "bad format" }));
    }
    return new Response("?", { status: 404 });
  };
  return log;
}
const call = (body) => worker.fetch(new Request("https://x.workers.dev/SEC/ai/tts-telegram", { method: "POST", body: JSON.stringify(body) }), { AI: {} });

// 1) обычная озвучка: звук склеен, отправлен как voice, текст экранирован
let log = setup();
let r = await call({ token: "TOK", chat_id: 5, text: "Щит <18> & плинтус", voice: "ru-RU-DmitryNeural", reply_to: 9 });
let body = await r.json();
assert.equal(r.status, 200); assert.deepEqual([body.ok, body.bytes, body.method, body.engine], [true, 55000, "sendVoice", "edge"]);
const gec = log.urls[0].match(/Sec-MS-GEC=([0-9A-F]+)&/)[1];
assert.match(log.urls[0], /TrustedClientToken=6A5AA1D4EAFF4E9FB37E23D68491D6F4/);
assert.match(log.urls[0], /Sec-MS-GEC-Version=1-143\.0\.3650\.75/);
assert.match(log.ws.sent[0], /Path:speech\.config/);
assert.match(log.ws.sent[1], /voice name='ru-RU-DmitryNeural'.*Щит &lt;18&gt; &amp; плинтус/s);
const [method, form] = log.forms[0];
assert.equal(method, "sendVoice");
assert.equal(form.get("chat_id"), "5"); assert.equal(form.get("reply_to_message_id"), "9");
assert.equal(form.get("voice").size, 55000);

// 2) Telegram не принял voice -> запасной sendAudio
log = setup({ voiceFails: true });
r = await call({ token: "TOK", chat_id: 5, text: "привет" });
assert.equal((await r.json()).method, "sendAudio");
assert.deepEqual(log.forms.map((f) => f[0]), ["sendVoice", "sendAudio"]);

// 3) Microsoft не вернул звук, запасной голос тоже недоступен -> одна понятная ошибка 502
setup({ noAudio: true, googleFails: true });
r = await call({ token: "TOK", chat_id: 5, text: "привет" });
assert.equal(r.status, 502); assert.match((await r.json()).error, /не вернул звук.*запасной голос/);

// 3a) Microsoft не вернул звук, но запасной голос работает -> ответ всё равно придёт
setup({ noAudio: true });
r = await call({ token: "TOK", chat_id: 5, text: "привет" });
assert.equal((await r.json()).engine, "google");

// 3b) Microsoft ответил 403 -> запасной голос Google: текст режется на куски до 180 знаков, куски склеиваются
log = setup({ edge403: true });
const long = "Первое предложение про остатки на складе. ".repeat(8) + "Конец.";
r = await call({ token: "TOK", chat_id: 5, text: long });
body = await r.json();
assert.equal(r.status, 200); assert.equal(body.engine, "google");
assert.ok(log.google.length >= 2 && log.google.every((c) => c.length <= 180), "куски до 180 знаков");
assert.equal(body.bytes, log.google.length * 1000);
assert.equal(log.forms[0][0], "sendVoice");

// 3c) не работают оба голоса -> одна ошибка с причинами обоих
setup({ edge403: true, googleFails: true });
r = await call({ token: "TOK", chat_id: 5, text: "привет" });
const err = (await r.json()).error;
assert.equal(r.status, 502); assert.match(err, /HTTP 403 Forbidden by policy/); assert.match(err, /запасной голос: Google TTS HTTP 429/);

// 4) чужой секрет не пускает
r = await worker.fetch(new Request("https://x.workers.dev/WRONG/ai/tts-telegram", { method: "POST", body: "{}" }), { AI: {} });
assert.equal(r.status, 404);

console.log("GEC=" + gec);
console.log("worker: все проверки пройдены");
