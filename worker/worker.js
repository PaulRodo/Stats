// Cloudflare Worker: бот со сводками из summary.enc, недельным отчётом, контролем сбора и короткими ссылками со счётчиком кликов.
// Переменные (Settings → Variables and Secrets): BOT_TOKEN, WEBHOOK_SECRET, SUMMARY_KEY, OWNER_ID, DATA_URL, APP_URL (необязательно)
// Привязка LINKS (KV): короткие ссылки, клики и память контроля сбора. Расписание задано в wrangler.toml.
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
function week(s) {
  const rows = s.rows.filter(r => r.f > 0), o = ['🗓 Итоги недели'];
  const f = rows.reduce((a, r) => a + r.f, 0), d7 = rows.reduce((a, r) => a + r.d7, 0);
  o.push(`Подписчики: ${nf(f)} (${sg(d7)} за неделю)`);
  const up = [...rows].sort((a, b) => b.d7 - a.d7)[0];
  if (up && up.d7 > 0) o.push(`Быстрее всех растёт: ${up.name} (${sg(up.d7)})`);
  const flat = rows.filter(r => r.d7 === 0).map(r => r.name), down = rows.filter(r => r.d7 < 0).map(r => `${r.name} (${sg(r.d7)})`);
  if (down.length) o.push('⚠️ Убыль: ' + down.join(', '));
  if (flat.length) o.push('Без роста: ' + flat.join(', '));
  if (s.best) o.push('', `🔥 Лучшая публикация: ${s.best.title} (${s.best.pid}), ${nf(s.best.views)} просмотров`);
  const tips = [];
  if (s.best) tips.push('повторить формат лучшей публикации');
  if (flat.length) tips.push('выпустить новое на платформах без роста: ' + flat.join(', '));
  if (tips.length) o.push('', 'Что можно сделать: ' + tips.join('; ') + '.');
  const id = s.ideas || {};
  if (id.q && id.q.length) o.push('', '💡 Идеи из комментариев (вопросы без ответа):', ...id.q.map(q => '• ' + q), id.w && id.w.length ? 'Частые темы: ' + id.w.join(', ') : '');
  if ((s.days || []).length >= 8) o.push('', forecast(s, 0).split('\n').slice(1, 3).join(' · '));
  return o.join('\n');
}
// контроль сбора: сообщает один раз при поломке и один раз при восстановлении
async function health(env) {
  if (!env.LINKS) return;
  let cat = '', msg = '';
  try {
    const s = await summary(env), age = (Date.now() - new Date(s.updated)) / 36e5;
    if (age > 3) { cat = 'stale'; msg = `⚠️ Данные не обновлялись уже ${Math.floor(age)} ч. Проверьте запуск update-stats в GitHub Actions.`; }
    else if (s.bad && s.bad.length) { cat = 'bad:' + s.bad.join(','); msg = '⚠️ Не отвечают платформы: ' + s.bad.join(', ') + '. Остальные данные обновляются.'; }
  } catch (e) { cat = 'read'; msg = '⚠️ Не удаётся прочитать данные: ' + e.message; }
  const prev = await env.LINKS.get('health');
  if (cat && cat !== prev) { await send(env, env.OWNER_ID, msg); await env.LINKS.put('health', cat); }
  else if (!cat && prev) { await send(env, env.OWNER_ID, '✅ Сбор данных снова работает.'); await env.LINKS.delete('health'); }
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
// журнал экспериментов: сравнивает ежедневный прирост за 7 дней до и после даты записи
async function tests(env, s, cmd, arg) {
  if (!env.LINKS) return 'Журнал не подключён: нужна привязка LINKS.';
  if (cmd === 'test') {
    if (!arg.trim()) return 'Формат: /test что вы изменили, например /test новая обложка';
    await env.LINKS.put('t:' + Date.now(), JSON.stringify({ d: dayKey(0), x: arg.trim().slice(0, 120) }));
    return '🧪 Записал. Через 7 дней покажу, как изменился прирост подписчиков: /tests';
  }
  const ks = (await env.LINKS.list({ prefix: 't:', limit: 20 })).keys, days = s.days || [], out = ['🧪 Эксперименты'];
  for (const k of ks) {
    const t = JSON.parse(await env.LINKS.get(k.name)), age = Math.floor((Date.now() - new Date(t.d)) / 864e5), i = days.findIndex(x => x[0] >= t.d);
    let r;
    if (age < 7) r = `ждём ещё ${7 - age} дн.`;
    else if (i < 7 || i + 7 >= days.length) r = 'данных за эти даты уже или ещё нет';
    else { const b = (days[i][1] - days[i - 7][1]) / 7, a = (days[i + 7][1] - days[i][1]) / 7; r = `прирост в день ${b.toFixed(1)} → ${a.toFixed(1)}`; }
    out.push(`${t.d} · ${t.x}\n   ${r}`);
  }
  return ks.length ? out.join('\n') : 'Экспериментов нет. Запишите: /test что вы изменили';
}
function dayInfo(s, arg) {
  const m = arg.trim().match(/^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$/);
  if (!m) return 'Формат: /day 12.10 (число.месяц, можно с годом)';
  const key = `${m[3] || new Date().getFullYear()}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`, d = s.days || [], i = d.findIndex(x => x[0] === key);
  if (i < 0) return 'За эту дату данных в боте нет: он помнит последние 31 день.';
  const o = [`📅 ${m[1]}.${m[2]}: всего ${nf(d[i][1])}`];
  if (i > 0) o.push(`За день: ${sg(d[i][1] - d[i - 1][1])}`);
  if (i >= 7) o.push(`За неделю до этой даты: ${sg(d[i][1] - d[i - 7][1])}`);
  if (i < d.length - 1) o.push(`С тех пор: ${sg(d[d.length - 1][1] - d[i][1])}`);
  return o.join('\n');
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
      else if (cmd === 'week' || cmd === 'digest') out = week(await summary(env));
      else if (cmd === 'forecast') out = forecast(await summary(env), parseInt(arg.replace(/\s/g, ''), 10) || 0);
      else if (cmd === 'test' || cmd === 'tests') out = await tests(env, cmd === 'tests' ? await summary(env) : null, cmd, arg);
      else if (cmd === 'day') out = dayInfo(await summary(env), arg);
      else if (cmd === 'link' || cmd === 'links' || cmd === 'unlink') out = await links(env, u.origin, cmd, arg);
      else if (cmd === 'help') out = 'Команды:\n/stats сводка сейчас\n/top лучшие публикации недели\n/week итоги и советы недели\n/forecast [цель] прогноз роста\n/day 12.10 как выглядел этот день\n/test что изменили · /tests журнал экспериментов\n/link метка адрес создать короткую ссылку\n/links клики по ссылкам\n/unlink метка удалить';
      else return new Response('ok');
    } catch (e) { out = '⚠️ Не получилось: ' + e.message; }
    const reply = { method: 'sendMessage', chat_id: m.chat.id, text: out.slice(0, 4000), disable_notification: true, disable_web_page_preview: true };
    if (app && env.APP_URL && m.chat.type === 'private') reply.reply_markup = { inline_keyboard: [[{ text: 'Открыть статистику', web_app: { url: env.APP_URL } }]] };
    return Response.json(reply);
  },
  async scheduled(ev, env, ctx) {
    ctx.waitUntil((async () => {
      try {
        if (ev.cron === '0 6 * * 1') await send(env, env.OWNER_ID, week(await summary(env)));
        else await health(env);
      } catch (e) { /* следующий запуск повторит */ }
    })());
  }
};
