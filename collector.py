"""Собирает статистику YouTube, Telegram, Instagram и TikTok и пишет data.json.
Ключи берутся из переменных окружения (в GitHub — из Secrets). Если ключей нет, платформа пропускается."""
import base64
import csv
import datetime as dt
import gzip
import hashlib
import html
import io
import json
import os
import re
import statistics
import sys
import time

import requests

WARN = []  # предупреждения (ключ, текст): токены, доступы. Отправляются раз в сутки
TOK = {}  # статусы токенов для экрана «Профиль»: {платформа: {"expires": unix-время} или {"ok": bool}}


def env(k):
    return os.environ.get(k, "").strip()


def call(method, url, **kw):
    """Запрос без утечки ключей: в тексте ошибки только код и сообщение API, без URL."""
    try:
        r = requests.request(method, url, timeout=30, **kw)
    except requests.RequestException:
        raise RuntimeError("сеть недоступна")
    try:
        d = r.json()
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    err = d.get("error")
    failed = (err.get("code") not in (None, "", "ok")) if isinstance(err, dict) else bool(err)
    if not r.ok or failed or d.get("ok") is False:
        msg = (err.get("message") or err.get("code") if isinstance(err, dict) else err) or d.get("error_message") or d.get("error_description") or d.get("description") or ""
        raise RuntimeError(f"{r.status_code}: {str(msg)[:150]}")
    return d


def cut(s, n=70):
    s = re.sub(r"\s+", " ", s or "").strip()
    return (s[: n - 1] + "…") if len(s) > n else (s or "Без названия")


def day(iso):
    return (iso or "")[:10]


def iso_dur(s):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    h, mi, se = (int(x or 0) for x in m.groups()) if m else (0, 0, 0)
    return h * 3600 + mi * 60 + se


def yt_fmt(v):
    d = iso_dur(v.get("contentDetails", {}).get("duration"))
    return {"dur": d, "fmt": "short" if 0 < d <= 180 else "long"}


def youtube():
    key, ch = env("YT_API_KEY"), env("YT_CHANNEL")
    if not (key and ch):
        return None
    B = "https://www.googleapis.com/youtube/v3/"
    p = {"part": "snippet,statistics,contentDetails", "key": key}
    if re.match(r"^UC[\w-]{20,}$", ch):
        p["id"] = ch
    else:
        p["forHandle"] = ch.lstrip("@")
    items = call("GET", B + "channels", params=p).get("items")
    if not items:
        raise RuntimeError("канал не найден")
    it = items[0]
    s = it["statistics"]
    vd = []
    try:
        pl = it["contentDetails"]["relatedPlaylists"]["uploads"]
        vids = call("GET", B + "playlistItems", params={"part": "contentDetails", "maxResults": 20, "playlistId": pl, "key": key})
        ids = ",".join(x["contentDetails"]["videoId"] for x in vids.get("items", []))
        if ids:
            vd = call("GET", B + "videos", params={"part": "snippet,statistics,contentDetails", "id": ids, "key": key}).get("items", [])
    except (RuntimeError, KeyError):
        pass  # у канала может не быть видео, это не ошибка
    hidden = s.get("hiddenSubscriberCount")
    subs = None if hidden else int(s["subscriberCount"])
    return {
        "title": it["snippet"]["title"], "followers": subs,
        "metrics": [["Подписчики", "скрыты" if hidden else subs], ["Просмотры", int(s["viewCount"])], ["Видео", int(s["videoCount"])]],
        "posts": [{"id": v["id"], "title": cut(v["snippet"]["title"]), "ts": v["snippet"]["publishedAt"],
                   "url": "https://www.youtube.com/watch?v=" + v["id"], **yt_fmt(v),
                   "views": int(v["statistics"].get("viewCount", 0)), "likes": int(v["statistics"].get("likeCount", 0)),
                   "comments": int(v["statistics"].get("commentCount", 0))} for v in vd],
    }


def parse_views(s):
    s = s.strip().replace(",", ".").replace(" ", "")
    mult = {"K": 1000, "M": 1000000}.get(s[-1:].upper(), 1)
    if mult != 1:
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def tg_posts(name):
    """Просмотры последних постов из публичной веб-версии канала (t.me/s/имя). Числа там округлены (1.2K)."""
    try:
        r = requests.get(f"https://t.me/s/{name}", timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException:
        return []
    if not r.ok:
        return []
    posts = []
    for chunk in r.text.split('data-post="')[1:]:
        pid = re.match(r'[^/"]+/(\d+)"', chunk)
        v = re.search(r'tgme_widget_message_views">([^<]+)<', chunk)
        if not (pid and v):
            continue
        t = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        tx = re.search(r'js-message_text"[^>]*>(.*?)</div>', chunk, re.S)
        text = html.unescape(re.sub(r"<[^>]+>", " ", tx.group(1))) if tx else ""
        posts.append({"id": pid.group(1), "title": cut(text), "ts": t.group(1) if t else "",
                      "url": f"https://t.me/{name}/{pid.group(1)}", "views": parse_views(v.group(1))})
    return posts[::-1]


def telegram():
    tok, chat = env("TG_BOT_TOKEN"), env("TG_CHAT")
    if not (tok and chat):
        return None
    B = f"https://api.telegram.org/bot{tok}/"
    n = call("GET", B + "getChatMemberCount", params={"chat_id": chat})["result"]
    title = chat
    try:
        title = call("GET", B + "getChat", params={"chat_id": chat})["result"].get("title", chat)
    except RuntimeError:
        pass
    res = {"title": title, "followers": n, "metrics": [["Подписчики", n]]}
    posts = tg_posts(chat.lstrip("@")) if chat.startswith("@") else []
    if posts:
        res["metrics"].append(["Просмотров на пост (в среднем)", sum(p["views"] for p in posts) // len(posts)])
        res["posts"] = posts
        res["note"] = "Просмотры постов взяты с публичной страницы канала, у больших чисел они округлены."
    else:
        res["note"] = "Просмотры постов получить не удалось: канал должен быть публичным (с @именем)."
    return res


IG_STATE = "instagram_state.enc"


def _ig_state(pw):
    if not os.path.exists(IG_STATE):
        return {}
    try:
        with open(IG_STATE, "rb") as f:
            return json.loads(_fernet(pw).decrypt(f.read()))
    except Exception:
        return {}


def ig_token():
    """Токен Instagram. Раз в неделю продлевает его ещё на 60 дней и хранит новый в зашифрованном виде.
    Токен можно задать секретом IG_TOKEN или получить один раз через ig_auth.py (по коду авторизации)."""
    tok, pw = env("IG_TOKEN"), env("TT_STATE_KEY")
    if env("IG_USER_ID") or not pw:
        return tok  # старый вариант через Facebook или нет ключа шифрования: берём токен как есть
    st = _ig_state(pw)
    if tok:
        origin = hashlib.sha256(tok.encode("utf-8")).hexdigest()
        if st.get("origin") != origin:  # в секрет вставили новый токен
            st = {"origin": origin, "token": tok, "refreshed": ""}
    elif not st.get("token"):
        return ""
    today = dt.datetime.now(dt.timezone.utc).date()
    last = st.get("refreshed")
    if not (last and (today - dt.date.fromisoformat(last)).days < 7):
        try:
            d = call("GET", "https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": st["token"]})
            if d.get("access_token"):
                st.update(token=d["access_token"], refreshed=today.isoformat(),
                          expires=int(time.time()) + int(d.get("expires_in") or 0))
                with open(IG_STATE, "wb") as f:
                    f.write(_fernet(pw).encrypt(json.dumps(st).encode("utf-8")))
        except RuntimeError:
            pass  # например, токену меньше суток: попробуем при следующем запуске
    left = (st.get("expires", 0) - time.time()) / 86400 if st.get("expires") else None
    if st.get("expires"):
        TOK["ig"] = {"expires": int(st["expires"])}
    if left is not None and left < 10:
        WARN.append(("tok_ig", f"🔑 Instagram: токен истекает через {max(int(left), 0)} дн. Если он не продлится сам, получите новый код (workflow, поле instagram_code)."))
    return st["token"]


def instagram():
    """Работает с токеном «Instagram API with Instagram Login» (ID не нужен)
    и со старым вариантом через Facebook (тогда нужен ещё IG_USER_ID)."""
    tok, uid = ig_token(), env("IG_USER_ID")
    if not tok:
        return None
    if uid:
        B, node = "https://graph.facebook.com/v25.0/", uid
    else:
        B, node = "https://graph.instagram.com/v25.0/", "me"
    u = call("GET", B + node, params={"fields": "username,followers_count,follows_count,media_count", "access_token": tok})
    rows = []
    try:
        m = call("GET", B + node + "/media", params={"fields": "id,caption,like_count,comments_count,timestamp,permalink,media_product_type", "limit": 20, "access_token": tok})
        rows = [{"id": x["id"], "title": cut(x.get("caption")), "ts": iso(x.get("timestamp", "")), "url": x.get("permalink"), "fmt": "reel" if x.get("media_product_type") == "REELS" else "post",
                 "likes": x.get("like_count", 0), "comments": x.get("comments_count", 0)} for x in m["data"]]
    except (RuntimeError, KeyError):
        pass
    return {
        "title": "@" + u["username"], "followers": u["followers_count"],
        "metrics": [["Подписчики", u["followers_count"]], ["Подписки", u.get("follows_count", 0)], ["Публикации", u.get("media_count", 0)]],
        "posts": rows,
    }


def tiktok_manual():
    """Запасной вариант без API: цифры вносятся вручную в файл tiktok.json."""
    if not os.path.exists("tiktok.json"):
        return None
    try:
        with open("tiktok.json", encoding="utf-8") as f:
            d = json.load(f)
        followers = int(d.get("followers") or 0)
        if not followers:
            return None  # шаблон ещё не заполнен
        metrics = [["Подписчики", followers]]
        for key, label in (("likes", "Лайки"), ("views", "Просмотры"), ("videos", "Видео")):
            if d.get(key):
                metrics.append([label, int(d[key])])
        rows = [{"title": cut(x.get("title")), "ts": str(x.get("date", "")) + "T12:00:00+00:00", "views": int(x.get("views") or 0),
                 "likes": int(x.get("likes") or 0), "comments": int(x.get("comments") or 0)} for x in d.get("recent", [])]
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("в tiktok.json ошибка: проверьте запятые, кавычки и что в числах нет пробелов")
    note = "Цифры введены вручную" + (f" (обновлено {d['updated']})." if d.get("updated") else ".")
    res = {"title": d.get("username", ""), "followers": followers, "metrics": metrics, "note": note}
    if rows:
        res["posts"] = rows
    return res


TT_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
STATE_FILE = "tiktok_state.enc"


def _fernet(pw):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(pw.encode("utf-8")).digest()))


def state_load(pw):
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "rb") as f:
            return json.loads(_fernet(pw).decrypt(f.read()))
    except Exception:
        raise RuntimeError("не удалось расшифровать tiktok_state.enc: проверьте секрет TT_STATE_KEY")


def state_save(pw, state):
    with open(STATE_FILE, "wb") as f:
        f.write(_fernet(pw).encrypt(json.dumps(state).encode("utf-8")))


def tt_token():
    """Обновляет доступ к TikTok по сохранённому refresh token. None, если TikTok не подключён."""
    ck, cs, pw = env("TT_CLIENT_KEY"), env("TT_CLIENT_SECRET"), env("TT_STATE_KEY")
    if not (ck and cs and pw):
        return None
    st = state_load(pw)
    if not st:
        return None
    d = call("POST", TT_TOKEN_URL, data={"client_key": ck, "client_secret": cs,
                                         "grant_type": "refresh_token", "refresh_token": st["refresh_token"]})
    if not d.get("access_token"):
        raise RuntimeError("не удалось обновить токен TikTok, подключите его заново")
    rexp = d.get("refresh_expires_in")
    state_save(pw, {"refresh_token": d.get("refresh_token") or st["refresh_token"],
                    **({"refresh_expires": int(time.time()) + int(rexp)} if rexp else {})})
    if rexp:
        TOK["tt"] = {"expires": int(time.time()) + int(rexp)}
    if rexp and int(rexp) < 14 * 86400:
        WARN.append(("tok_tt", f"🔑 TikTok: доступ истекает через {int(rexp) // 86400} дн. Подключите заново (workflow, поле tiktok_code)."))
    return d["access_token"]


def tiktok():
    token = tt_token()
    if not token:
        return tiktok_manual()
    H = {"Authorization": "Bearer " + token}
    U = "https://open.tiktokapis.com/v2/user/info/"
    note = None
    try:
        u = call("GET", U, headers=H, params={"fields": "display_name,follower_count,likes_count,video_count"})["data"]["user"]
    except RuntimeError:
        # нет права user.info.stats: хотя бы имя
        u = call("GET", U, headers=H, params={"fields": "display_name"})["data"]["user"]
        note = "Нет доступа к статистике аккаунта: в приложении TikTok нужно право user.info.stats."
    vids = []
    F = "id,title,create_time,view_count,like_count,comment_count,share_count"
    for fields in (F + ",share_url", F):  # со ссылкой на видео; если не разрешено, то без неё
        try:
            vids = call("POST", "https://open.tiktokapis.com/v2/video/list/", headers=H, json={"max_count": 20},
                        params={"fields": fields})["data"].get("videos", [])
            break
        except RuntimeError:
            continue
    rows = [{"id": x.get("id"), "title": cut(x.get("title")), "ts": dt.datetime.fromtimestamp(x["create_time"], dt.timezone.utc).isoformat(),
             "url": x.get("share_url"),
             "views": x.get("view_count", 0), "likes": x.get("like_count", 0), "comments": x.get("comment_count", 0),
             "shares": x.get("share_count", 0)} for x in vids]
    metrics = []
    if "follower_count" in u:
        metrics += [["Подписчики", u["follower_count"]], ["Лайки", u.get("likes_count", 0)], ["Видео", u.get("video_count", 0)]]
    if vids:
        metrics.append([f"Просмотры (последние {len(vids)} видео)", sum(x.get("view_count", 0) for x in vids)])
    res = {"title": u.get("display_name", ""), "followers": u.get("follower_count"), "metrics": metrics or [["Аккаунт", "подключён"]],
           "posts": rows}
    if note:
        res["note"] = note
    return res


YT_STATE = "youtube_state.enc"
REP = "https://youtubereporting.googleapis.com/v1/"


def yt_token():
    """Токен для YouTube Analytics. None, если углублённая аналитика не подключена."""
    cid, cs, pw = env("YT_CLIENT_ID"), env("YT_CLIENT_SECRET"), env("TT_STATE_KEY")
    if not (cid and cs and pw and os.path.exists(YT_STATE)):
        return None
    try:
        with open(YT_STATE, "rb") as f:
            st = json.loads(_fernet(pw).decrypt(f.read()))
    except Exception:
        raise RuntimeError("не удалось расшифровать youtube_state.enc: проверьте секрет TT_STATE_KEY")
    d = call("POST", "https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": cs, "refresh_token": st["refresh_token"], "grant_type": "refresh_token"})
    return d.get("access_token")


def yt_report(tok, **p):
    return call("GET", "https://youtubeanalytics.googleapis.com/v2/reports",
                headers={"Authorization": "Bearer " + tok}, params={"ids": "channel==MINE", **p})


def yt_deep(tok, today):
    """Среднее время просмотра, удержание, пол и возраст, приход и уход подписчиков, цифры по видео."""
    end, start = today.isoformat(), (today - dt.timedelta(days=28)).isoformat()
    start90 = (today - dt.timedelta(days=90)).isoformat()
    deep, vids = {"days": 28}, {}
    r = yt_report(tok, startDate=start, endDate=end,
                  metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,subscribersLost")
    row = (r.get("rows") or [[]])[0]
    if len(row) == 6:
        deep.update(zip(("views", "watch", "avd", "avp", "gained", "lost"), (round(x, 1) for x in row)))
    try:
        r = yt_report(tok, startDate=start90, endDate=end, dimensions="ageGroup,gender", metrics="viewerPercentage")
        g, a = {}, {}
        for age, gen, pct in r.get("rows", []):
            g[gen] = g.get(gen, 0) + pct
            a[age] = a.get(age, 0) + pct
        if g:
            deep["gender"] = {k: round(v, 1) for k, v in g.items()}
            deep["age"] = {k: round(v, 1) for k, v in sorted(a.items())}
    except RuntimeError:
        pass  # мало просмотров: YouTube скрывает демографию
    try:
        r = yt_report(tok, startDate=start, endDate=end, dimensions="day", metrics="subscribersGained,subscribersLost", sort="day")
        deep["daily"] = [[d, int(g), int(l)] for d, g, l in r.get("rows", [])]
    except RuntimeError:
        pass
    try:  # источники трафика
        r = yt_report(tok, startDate=start, endDate=end, dimensions="insightTrafficSourceType", metrics="views", sort="-views")
        rows = [[k, int(v)] for k, v in r.get("rows", [])]
        if rows:
            deep["src"] = rows[:10]
    except RuntimeError:
        pass
    try:  # страны зрителей
        r = yt_report(tok, startDate=start, endDate=end, dimensions="country", metrics="views", sort="-views", maxResults=10)
        rows = [[k, int(v)] for k, v in r.get("rows", [])]
        if rows:
            deep["cn"] = rows
    except RuntimeError:
        pass
    try:
        r = yt_report(tok, startDate=start90, endDate=end, dimensions="video",
                      metrics="views,averageViewDuration,averageViewPercentage", sort="-views", maxResults=50)
        for vid, _, avd, avp in r.get("rows", []):
            vids[vid] = {"avd": round(avd), "avp": round(avp, 1)}
    except RuntimeError:
        pass
    return deep, vids


def yt_ctr(tok, x):
    """CTR значков (показы и клики) через Reporting API. Задача создаётся один раз, отчёты копятся в x."""
    H = {"Authorization": "Bearer " + tok}
    jobs = call("GET", REP + "jobs", headers=H).get("jobs", [])
    job = next((j for j in jobs if j.get("reportTypeId", "").startswith("channel_reach_basic")), None) \
        or next((j for j in jobs if j.get("reportTypeId", "").startswith("channel_reach")), None)
    if not job:
        types = [t["id"] for t in call("GET", REP + "reportTypes", headers=H).get("reportTypes", [])]
        tid = next((t for t in types if t.startswith("channel_reach_basic")), None) or next((t for t in types if t.startswith("channel_reach")), None)
        if not tid:
            raise RuntimeError("отчёт о показах и CTR недоступен для этого канала")
        job = call("POST", REP + "jobs", headers=H, json={"reportTypeId": tid, "name": "stats-reach"})
    seen = set(x.get("seen", []))
    reps = call("GET", REP + f"jobs/{job['id']}/reports", headers=H, params={"pageSize": 100}).get("reports", [])
    for rep in sorted(reps, key=lambda z: z.get("createTime", "")):
        if rep["id"] in seen:
            continue
        try:
            resp = requests.get(rep["downloadUrl"], headers=H, timeout=60)
        except requests.RequestException:
            continue
        if not resp.ok:
            continue
        raw = resp.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        vals = []
        for r in rows:
            try:
                vals.append((r["date"], r["video_id"], float(r["video_thumbnail_impressions"]), float(r["video_thumbnail_impressions_ctr"])))
            except (KeyError, ValueError):
                continue
        scale = 1 if vals and max(v[3] for v in vals) <= 1 else 100  # CTR пришёл долей (0,05) или процентами (5)
        for date, vid, i, c in vals:
            clicks = i * c / scale
            date = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date
            dd = x.setdefault("days", {}).setdefault(date, [0, 0])
            dd[0] += i
            dd[1] += clicks
            vv = x.setdefault("vids", {}).setdefault(vid, [0, 0])
            vv[0] += i
            vv[1] += clicks
        seen.add(rep["id"])
    x["seen"] = sorted(seen)[-400:]
    x["days"] = {k: x["days"][k] for k in sorted(x.get("days", {}))[-120:]}


def yt_extra(x, now):
    tok = yt_token()
    if not tok:
        return None, {}
    today = now.date()
    deep, vids = yt_deep(tok, today)
    try:
        yt_ctr(tok, x)
        lim = (today - dt.timedelta(days=28)).isoformat()
        imp = sum(v[0] for k, v in x.get("days", {}).items() if k >= lim)
        clk = sum(v[1] for k, v in x.get("days", {}).items() if k >= lim)
        if imp:
            deep["impr"], deep["ctr"] = int(imp), round(clk / imp * 100, 2)
        else:
            deep["ctr_wait"] = True
    except RuntimeError as e:
        deep["ctr_error"] = str(e)[:120]
    return deep, vids


def competitors():
    """Публичные цифры чужих каналов из competitors.json: {"youtube": ["@handle"], "telegram": ["name"]}."""
    if not os.path.exists("competitors.json"):
        return []
    try:
        with open("competitors.json", encoding="utf-8") as f:
            cfg = json.load(f)
    except ValueError:
        return []
    rows, key = [], env("YT_API_KEY")
    for hdl in (cfg.get("youtube") or [])[:10]:
        if not key:
            break
        p = {"part": "snippet,statistics", "key": key}
        if re.match(r"^UC[\w-]{20,}$", hdl):
            p["id"] = hdl
        else:
            p["forHandle"] = hdl.lstrip("@")
        try:
            it = (call("GET", "https://www.googleapis.com/youtube/v3/channels", params=p).get("items") or [None])[0]
            if it and not it["statistics"].get("hiddenSubscriberCount"):
                rows.append({"key": "yt:" + hdl, "pid": "yt", "name": it["snippet"]["title"], "followers": int(it["statistics"]["subscriberCount"])})
        except (RuntimeError, KeyError, ValueError):
            continue
    for name in (cfg.get("telegram") or [])[:10]:
        name = name.lstrip("@")
        try:
            r = requests.get(f"https://t.me/{name}", timeout=30, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
        except requests.RequestException:
            continue
        m = re.search(r'tgme_page_extra">\s*([\d\s\xa0]+?)\s*(?:subscribers?|members?|подписчик\w*|участник\w*)', r.text)
        t = re.search(r'tgme_page_title[^>]*>\s*(?:<span[^>]*>)?([^<]+)', r.text)
        if m:
            rows.append({"key": "tg:" + name, "pid": "tg", "name": html.unescape((t.group(1) if t else name).strip()),
                         "followers": int(re.sub(r"\D", "", m.group(1)))})
    return rows


def iso(ts):
    """Instagram отдаёт время как +0000, а браузеры Safari такое не читают: приводим к +00:00."""
    try:
        return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").isoformat()
    except ValueError:
        return ts


DATA_FILE, ENC_FILE = "data.json", "data.enc"
NAMES = {"yt": "YouTube", "tg": "Telegram", "ig": "Instagram", "tt": "TikTok"}


def _aes_key(pw, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200000).derive(pw.encode("utf-8"))


def encrypt_data(obj, pw):
    """AES-GCM с ключом из пароля (PBKDF2). Страница расшифровывает это средствами браузера."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_aes_key(pw, salt)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "iter": 200000, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt_data(box, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode
    return json.loads(AESGCM(_aes_key(pw, raw(box["salt"]))).decrypt(raw(box["iv"]), raw(box["ct"]), None))


def load_old(pw):
    if pw and os.path.exists(ENC_FILE):
        try:
            with open(ENC_FILE, encoding="utf-8") as f:
                return decrypt_data(json.load(f), pw)
        except Exception:
            sys.exit("Не удалось расшифровать data.enc: пароль DATA_PASSWORD изменился. "
                     "Верните прежний пароль или удалите data.enc, чтобы начать историю заново.")
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            pass
    return {}


APP_URL = env("APP_URL") or "https://paulrodo.github.io/Stats/"


def notify(text, loud=False):
    """Сообщение в Telegram с кнопкой «Открыть статистику». Без звука, если loud=False.
    Если заданы NOTIFY_BOT_TOKEN и NOTIFY_CHAT_ID, шлёт через отдельного бота,
    иначе через прежнего (TG_BOT_TOKEN и TG_ALERT_CHAT)."""
    tok = env("NOTIFY_BOT_TOKEN") or env("TG_BOT_TOKEN")
    chat = env("NOTIFY_CHAT_ID") or env("TG_ALERT_CHAT")
    if not (tok and chat):
        return False
    private = chat.lstrip("-").isdigit() and not chat.startswith("-")  # web_app-кнопки работают только в личке
    btn = {"text": "Открыть статистику", **({"web_app": {"url": APP_URL}} if private else {"url": APP_URL})}
    base = {"chat_id": chat, "text": text, "disable_notification": not loud, "link_preview_options": {"is_disabled": True}}
    for extra in ({"reply_markup": {"inline_keyboard": [[btn]]}}, {}):
        try:
            call("POST", f"https://api.telegram.org/bot{tok}/sendMessage", json={**base, **extra})
            return True
        except RuntimeError:
            continue
    return False


def _num(n):
    return f"{n:,}".replace(",", " ")


def _base(hist, ref):
    past = [d for d in hist if d <= ref]
    return hist[max(past)] if past else hist[min(hist)]


def _lines(out, base):
    lines, total, dtot = [], 0, 0
    for pid, r in out["platforms"].items():
        f = r.get("followers")
        if isinstance(f, (int, float)):
            d = f - base.get(pid, f)
            total += f
            dtot += d
            lines.append(f"{NAMES.get(pid, pid)}: {_num(int(f))} ({int(d):+,})".replace(",", " "))
    lines.append(f"Всего: {_num(int(total))} ({int(dtot):+,})".replace(",", " "))
    return lines


def _top(out, pred):
    c = [p for p in out["archive"] if isinstance(p.get("views"), int) and pred(p.get("ts", "")[:10])]
    if not c:
        return []
    b = max(c, key=lambda p: p["views"])
    return [f"🔥 Лучшая публикация: {b['title']} ({NAMES.get(b['pid'], b['pid'])}), {_num(b['views'])} просмотров"] + ([b["url"]] if b.get("url") else [])


def periodic(out, now):
    """Тихие сводки: месяц (1-го числа), неделя (по понедельникам), сутки (если NOTIFY_DAILY=1)."""
    alerts, hist = out["alerts"], out["history"]
    today, ym = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    if not hist:
        return
    if now.day == 1 and alerts.get("monthly") != ym:
        first = (now.date().replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        lines = ["🗓 Итоги месяца"] + _lines(out, _base(hist, first.isoformat())) + _top(out, lambda d: d[:7] == first.strftime("%Y-%m"))
        if notify("\n".join(lines)):
            alerts["monthly"] = ym
    if now.weekday() == 0 and alerts.get("digest") != today:
        ref = (now.date() - dt.timedelta(days=7)).isoformat()
        lines = ["📊 Итоги недели"] + _lines(out, _base(hist, ref)) + _top(out, lambda d: d >= ref)
        if notify("\n".join(lines)):
            alerts["digest"] = today
    elif env("NOTIFY_DAILY") == "1" and now.day != 1 and 5 <= now.hour < 12 and alerts.get("daily") != today:
        ref = (now.date() - dt.timedelta(days=1)).isoformat()
        if notify("\n".join(["☀️ Сводка за сутки"] + _lines(out, _base(hist, ref)))):
            alerts["daily"] = today


def viral(out, now):
    """Публикация младше 48 часов набрала втрое больше просмотров, чем обычно набирают прежние."""
    sent = out["alerts"].setdefault("viral", [])
    for pid in ("yt", "tg", "tt"):
        ps = []
        for p in out["archive"]:
            if p["pid"] != pid or not isinstance(p.get("views"), int) or not p.get("ts"):
                continue
            try:
                t = dt.datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))
            except ValueError:
                continue
            ps.append((now - (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)), p))
        old = [p["views"] for a, p in ps if a >= dt.timedelta(hours=48)][:15]
        if len(old) < 5:
            continue
        med = statistics.median(old)
        for a, p in ps:
            key = f"{pid}:{p.get('id') or p['title']}"
            if a >= dt.timedelta(hours=48) or key in sent or p["views"] < max(3 * med, 100):
                continue
            hrs = max(int(a.total_seconds() // 3600), 1)
            ratio = str(round(p["views"] / med, 1)).replace(".", ",")
            txt = f"🚀 {NAMES.get(pid, pid)}: «{p['title']}» уже {_num(p['views'])} просмотров за {hrs} ч, это в {ratio} раза выше обычного"
            if notify(txt + ("\n" + p["url"] if p.get("url") else ""), loud=True):
                sent.append(key)
    out["alerts"]["viral"] = sent[-200:]


def anomaly(out, now):
    """Резкий отток или всплеск подписчиков за прошлый день: сравнение с тем же днём недели в прошлые недели."""
    hist, alerts = out["history"], out["alerts"]
    days = sorted(hist)
    if days and days[-1] == now.strftime("%Y-%m-%d"):
        days = days[:-1]  # сегодняшний день ещё не закончился
    if len(days) < 22:
        return
    keys = lambda d: {k for k, v in hist[d].items() if not k.startswith("c:") and isinstance(v, (int, float))}
    tot = lambda d: sum(hist[d][k] for k in keys(d))
    day, prev = days[-1], days[-2]
    if alerts.get("anom") == day or keys(day) != keys(prev):
        return  # уже сообщали, либо в какой-то из дней платформа не ответила
    wd = dt.date.fromisoformat(day).weekday()
    ref = [tot(days[i]) - tot(days[i - 1]) for i in range(1, len(days) - 1)
           if dt.date.fromisoformat(days[i]).weekday() == wd and keys(days[i]) == keys(days[i - 1])][-8:]
    if len(ref) < 3:
        return
    med = statistics.median(ref)
    spread = max(statistics.median(abs(x - med) for x in ref), 3)
    d = tot(day) - tot(prev)
    if d < 0 and d < med - 4 * spread:
        txt = f"📉 Отток подписчиков за {day}: {d:+.0f}, обычно в этот день недели {med:+.0f}"
    elif d >= 10 and d > med + 4 * spread:
        txt = f"🚀 Всплеск подписчиков за {day}: {d:+.0f}, обычно {med:+.0f}"
    else:
        return
    if notify(txt, loud=d < 0):
        alerts["anom"] = day


def make_summary(out, now):
    """Короткая сводка для бота /stats (бот на Cloudflare Workers, см. worker.js)."""
    hist = out["history"]
    if not hist:
        return None
    base = lambda n: _base(hist, (now.date() - dt.timedelta(days=n)).isoformat())
    b1, b7, b30 = base(1), base(7), base(30)
    rows = []
    for pid, r in out["platforms"].items():
        f = r.get("followers")
        if isinstance(f, (int, float)):
            rows.append({"id": pid, "name": NAMES.get(pid, pid), "f": int(f), "d1": int(f - b1.get(pid, f)),
                         "d7": int(f - b7.get(pid, f)), "d30": int(f - b30.get(pid, f))})
    lim = (now.date() - dt.timedelta(days=7)).isoformat()
    c = [p for p in out["archive"] if isinstance(p.get("views"), int) and p.get("ts", "")[:10] >= lim]
    best = max(c, key=lambda p: p["views"]) if c else None
    return {"updated": out["updated"], "rows": rows,
            "best": {"title": best["title"], "pid": NAMES.get(best["pid"], best["pid"]), "views": best["views"]} if best else None}


def write_summary(out, now):
    """summary.enc: AES-GCM со случайным ключом SUMMARY_KEY (хранится в секретах GitHub и Cloudflare). Пароль не нужен."""
    key = env("SUMMARY_KEY")
    sm = make_summary(out, now) if key else None
    if not sm:
        return
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(hashlib.sha256(key.encode("utf-8")).digest()).encrypt(iv, json.dumps(sm, ensure_ascii=False).encode("utf-8"), None)
    with open("summary.enc", "w", encoding="utf-8") as f:
        json.dump({"iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()}, f)


def main():
    pw = env("DATA_PASSWORD")
    old = load_old(pw)
    hist, alerts = old.get("history", {}), old.get("alerts", {})
    ytx, vmap = old.get("ytx", {}), {}
    arch = {(p["pid"], p.get("id") or p["title"]): p for p in old.get("archive", [])}
    now = dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    out = {"updated": now.isoformat(timespec="seconds"), "platforms": {}}
    for pid, fn in (("yt", youtube), ("tg", telegram), ("ig", instagram), ("tt", tiktok)):
        try:
            res = fn()
        except Exception as e:  # одна платформа не должна ломать остальные
            res = {"title": "", "error": str(e)[:200]}
        if pid == "yt" and res and not res.get("error"):
            try:
                deep, vmap = yt_extra(ytx, now)
                if deep:
                    res["deep"] = deep
                    TOK["yt"] = {"ok": True}
            except RuntimeError as e:
                res["deep_error"] = str(e)[:150]
                if "invalid_grant" in str(e):
                    TOK["yt"] = {"ok": False}
                    WARN.append(("tok_yt", "🔑 YouTube: доступ к аналитике больше не действует. Подключите заново (workflow, поле youtube_code)."))
        if res is None:
            continue
        for p in res.pop("posts", []):  # свежие публикации попадают в архив и обновляют свои цифры
            key = (pid, p.get("id") or p["title"])
            new = {k: v for k, v in p.items() if v is not None}
            if not new.get("url") and arch.get(key, {}).get("url"):
                new["url"] = arch[key]["url"]  # не теряем ссылку, если сейчас её получить не удалось
            arch[key] = {**arch.get(key, {}), **new, "pid": pid}
        out["platforms"][pid] = res
        if res.get("followers") is not None:
            hist.setdefault(today, {})[pid] = res["followers"]
        if res.get("error"):
            if alerts.get(pid) != today and notify(f"⚠️ {NAMES[pid]}: {res['error']}", loud=True):
                alerts[pid] = today  # не чаще раза в сутки
        else:
            alerts.pop(pid, None)
    vc = ytx.get("vids", {})
    for p in arch.values():  # цифры YouTube Analytics и CTR для видео
        if p["pid"] != "yt":
            continue
        p.update(vmap.get(p.get("id"), {}))
        v = vc.get(p.get("id"))
        if v and v[0] > 0:
            p["impr"], p["ctr"] = int(v[0]), round(v[1] / v[0] * 100, 2)
    tgc = env("TG_CHAT")
    for p in arch.values():  # ссылки для старых публикаций, у которых их ещё нет
        if p.get("url") or not p.get("id"):
            continue
        if p["pid"] == "yt":
            p["url"] = "https://www.youtube.com/watch?v=" + str(p["id"])
        elif p["pid"] == "tg" and tgc.startswith("@"):
            p["url"] = f"https://t.me/{tgc[1:]}/{p['id']}"
    comps = competitors()
    for c in comps:
        hist.setdefault(today, {})["c:" + c["key"]] = c["followers"]
    if comps:
        out["competitors"] = comps
    if ytx:
        out["ytx"] = ytx
    out["history"] = {d: hist[d] for d in sorted(hist)[-400:]}
    out["alerts"] = alerts
    out["tokens"] = TOK
    curves = old.get("curves", {})  # почасовая динамика просмотров новых публикаций (первые 48 часов)
    for p in arch.values():
        v = p.get("views")
        if not p.get("ts") or not isinstance(v, int):
            continue
        try:
            t = dt.datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))
        except ValueError:
            continue
        age = (now - (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc))).total_seconds() / 3600
        if 0 <= age <= 48:
            cv = curves.setdefault(f"{p['pid']}:{p.get('id') or p['title']}", {"t": p.get("title", ""), "pid": p["pid"], "pts": []})
            if not cv["pts"] or age - cv["pts"][-1][0] >= 0.5:
                cv["pts"].append([round(age, 1), v])
    out["curves"] = dict(list(curves.items())[-60:])
    out["archive"] = sorted(arch.values(), key=lambda p: p.get("ts", ""), reverse=True)[:1000]
    for fn in (periodic, viral, anomaly):
        try:
            fn(out, now)
        except Exception as e:  # уведомления не должны ломать сбор данных
            print("Уведомление не отправлено:", e)
    for key, text in WARN:
        if alerts.get(key) != today and notify(text, loud=True):
            alerts[key] = today
    if pw:
        with open(ENC_FILE, "w", encoding="utf-8") as f:
            json.dump(encrypt_data(out, pw), f)
        if os.path.exists(DATA_FILE):
            os.remove(DATA_FILE)  # открытая копия больше не нужна
    else:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    try:
        write_summary(out, now)
    except Exception as e:  # сводка для бота не должна ломать сбор данных
        print("Сводка для бота не записана:", e)
    print("Готово:", ", ".join(out["platforms"]) or "ни одна платформа не подключена")


if __name__ == "__main__":
    main()
