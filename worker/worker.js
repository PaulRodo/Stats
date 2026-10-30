// Cloudflare Worker: бот со сводками из summary.enc, ИИ-разбором недели и короткими ссылками со счётчиком кликов.
// Переменные (Settings → Variables and Secrets): BOT_TOKEN, WEBHOOK_SECRET, SUMMARY_KEY, OWNER_ID, DATA_URL, APP_URL (необязательно)
// Привязки (Settings → Bindings), обе необязательные:
//   LINKS  KV-хранилище: короткие ссылки и клики (команды /link, /links, /unlink)
//   AI     Workers AI: ИИ-разбор недели (команда /digest и автоотправка по понедельникам)
// Cron Trigger (Settings → Trigger Events): например «0 6 * * 1», тогда разбор приходит сам.
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const nf = n => Math.round(n).toLocaleString('ru-RU').replace(/\u00a0/g, ' ');
const sg = n => (n > 0 ? '+' : n < 0 ? '−' : '') + nf(Math.abs(n));
const SLUG = /^[a-z0-9_-]{1,32}$/;

async function summary(env) {
  const r = await fetch(env.DATA_URL + '?t=' + Date.now(), { cf: { cacheTtl: 0 } });
  if (!r.ok) throw new Error('summary.enc недоступен (' + r.status + ')');
  const box = await r.json();
  const raw = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(env.SUMMARY_KEY));
  const key = await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['decrypt']);
  return JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: b64(box.iv) }, key, b64(box.ct))));
}
function text(s) {
  const t = { f: 0, d1: 0, d7: 0, d30: 0 };
  const lines = ['📊 Сводка'];
  s.rows.forEach(r => { ['f', 'd1', 'd7', 'd30'].forEach(k => { t[k] += r[k]; }); lines.push(`${r.name}: ${nf(r.f)} (${sg(r.d1)} сутки · ${sg(r.d7)} неделя · ${sg(r.d30)} месяц)`); });
  lines.push(`Всего: ${nf(t.f)} (${sg(t.d1)} сутки · ${sg(t.d7)} неделя · ${sg(t.d30)} месяц)`);
  if (s.best) lines.push('', `🔥 Лучшая публикация недели: ${s.best.title} (${s.best.pid}), ${nf(s.best.views)} просмотров`);
  const when = new Date(s.updated).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow', day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
  lines.push('', `Данные от ${when} (МСК)`);
  return lines.join('\n');
}
const top = s => !s.top || !s.top.length ? 'Пока нет данных по публикациям за неделю.'
  : '🏆 Лучшие публикации недели\n' + s.top.map((p, i) => `${i + 1}. ${p.title} · ${p.pid} · ${nf(p.views)}${p.url ? '\n' + p.url : ''}`).join('\n');
function forecast(s, goal) {
  const d = s.days || [];
  if (d.length < 8) return 'Для прогноза нужно хотя бы 8 дней истории.';
  const a = d.slice(-15), now = a[a.length - 1][1], per = (a[a.length - 1][1] - a[0][1]) / (a.length - 1);
  const out = ['📈 Прогноз по темпу последних ' + (a.length - 1) + ' дней', `Сейчас: ${nf(now)}`, `Темп: ${sg(per)} в день`];
  if (per > 0) out.push(`Через 30 дней: ~${nf(now + per * 30)}`, `Через 90 дней: ~${nf(now + per * 90)}`);
  else out.push('Роста сейчас нет, поэтому прогноз вперёд не строю.');
  if (goal > now) out.push(per > 0 ? `До ${nf(goal)}: около ${Math.ceil((goal - now) / per)} дн., это ${new Date(Date.now() + (goal - now) / per * 864e5).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' })}` : `До ${nf(goal)}: при нулевом росте цель не достигается.`);
  out.push('Это грубая оценка по линейному темпу.');
  return out.join('\n');
}
const MODELS = ['@cf/zai-org/glm-4.7-flash', '@cf/google/gemma-4-26b-a4b-it']; // если одну отключат, берётся следующая
async function digest(env, s) {
  const base = text(s);
  if (!env.AI) return base + '\n\n(ИИ-разбор не подключён: добавьте привязку AI в Cloudflare)';
  const messages = [
    { role: 'system', content: 'Ты аналитик соцсетей. Отвечай по-русски, коротко и по делу. Используй только числа из данных, ничего не выдумывай.' },
    { role: 'user', content: 'Данные:\n' + base + '\n\n' + top(s) + '\n\nНапиши разбор недели: 1) что выросло или просело, 2) что сработало, 3) три гипотезы на следующую неделю. Не больше 900 знаков.' }];
  let err = '';
  for (const model of MODELS) {
    try {
      const r = await env.AI.run(model, { messages, max_tokens: 1000 });
      const t = String(r.response || (r.choices && r.choices[0] && r.choices[0].message && r.choices[0].message.content) || '').trim();
      if (t) return '🧠 Разбор недели\n' + t + '\n\n' + base;
      err = 'пустой ответ модели';
    } catch (e) { err = e.message; }
  }
  return base + '\n\n(ИИ-разбор не удался: ' + err + ')';
}
const send = (env, chat, t) => fetch('https://api.telegram.org/bot' + env.BOT_TOKEN + '/sendMessage', {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ chat_id: chat, text: t.slice(0, 4000), disable_web_page_preview: true })
});
const dayKey = n => new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);

async function links(env, origin, cmd, arg) {
  if (!env.LINKS) return 'Короткие ссылки не подключены: создайте KV-хранилище и привяжите его к воркеру с именем LINKS.';
  if (cmd === 'link') {
    const [slug, url] = arg.trim().split(/\s+/);
    if (!slug || !url || !SLUG.test(slug) || !/^https?:\/\//i.test(url)) return 'Формат: /link метка https://адрес\nМетка: латиница, цифры, - и _ (до 32 знаков).';
    await env.LINKS.put('l:' + slug, url);
    return `✅ Готово: ${origin}/go/${slug}\nведёт на ${url}`;
  }
  if (cmd === 'unlink') { await env.LINKS.delete('l:' + arg.trim()); return 'Удалено (если такая метка была).'; }
  const ks = (await env.LINKS.list({ prefix: 'l:', limit: 30 })).keys.map(k => k.name.slice(2));
  if (!ks.length) return 'Ссылок пока нет. Создайте: /link метка https://адрес';
  const rows = [];
  for (const k of ks) {
    let w = 0, m = 0;
    for (let i = 0; i < 30; i++) { const v = +(await env.LINKS.get(`c:${k}:${dayKey(i)}`)) || 0; m += v; if (i < 7) w += v; }
    rows.push(`${origin}/go/${k}\n   ${w} за 7 дн. · ${m} за 30 дн.`);
  }
  return '🔗 Ссылки и клики\n' + rows.join('\n');
}
async function click(env, slug) {
  try { const k = `c:${slug}:${dayKey(0)}`; await env.LINKS.put(k, String((+(await env.LINKS.get(k)) || 0) + 1), { expirationTtl: 40 * 864e5 }); } catch (e) {}
}

export default {
  async fetch(req, env, ctx) {
    const u = new URL(req.url);
    const g = u.pathname.match(/^\/go\/([a-z0-9_-]{1,32})\/?$/);
    if (g && req.method === 'GET') {
      const to = env.LINKS && await env.LINKS.get('l:' + g[1]);
      if (!to) return new Response('Not found', { status: 404 });
      ctx.waitUntil(click(env, g[1]));
      return Response.redirect(to, 302);
    }
    if (req.method !== 'POST' || req.headers.get('X-Telegram-Bot-Api-Secret-Token') !== env.WEBHOOK_SECRET) return new Response('ok');
    let up; try { up = await req.json(); } catch (e) { return new Response('ok'); }
    const m = up.message;
    if (!m || !m.text || String(m.from && m.from.id) !== String(env.OWNER_ID)) return new Response('ok');
    const c = m.text.match(/^\/(\w+)(?:@\w+)?(?:\s+([\s\S]*))?$/);
    if (!c) return new Response('ok');
    const cmd = c[1].toLowerCase(), arg = c[2] || '';
    let out, app = false;
    try {
      if (cmd === 'stats' || cmd === 'start') { out = text(await summary(env)); app = true; }
      else if (cmd === 'top') out = top(await summary(env));
      else if (cmd === 'week') { const s = await summary(env); out = text(s) + '\n\n' + top(s); }
      else if (cmd === 'forecast') out = forecast(await summary(env), parseInt(arg.replace(/\s/g, ''), 10) || 0);
      else if (cmd === 'digest') out = await digest(env, await summary(env));
      else if (cmd === 'link' || cmd === 'links' || cmd === 'unlink') out = await links(env, u.origin, cmd, arg);
      else if (cmd === 'help') out = 'Команды:\n/stats сводка\n/top лучшие публикации недели\n/week сводка и топ\n/forecast [цель] прогноз роста\n/digest ИИ-разбор недели\n/link метка адрес создать короткую ссылку\n/links клики по ссылкам\n/unlink метка удалить';
      else return new Response('ok');
    } catch (e) { out = '⚠️ Не получилось: ' + e.message; }
    const reply = { method: 'sendMessage', chat_id: m.chat.id, text: out.slice(0, 4000), disable_notification: true, disable_web_page_preview: true };
    if (app && env.APP_URL && m.chat.type === 'private') reply.reply_markup = { inline_keyboard: [[{ text: 'Открыть статистику', web_app: { url: env.APP_URL } }]] };
    return Response.json(reply);
  },
  async scheduled(ev, env, ctx) {
    ctx.waitUntil((async () => { try { await send(env, env.OWNER_ID, await digest(env, await summary(env))); } catch (e) { await send(env, env.OWNER_ID, '⚠️ Не удалось собрать разбор недели: ' + e.message); } })());
  }
};
