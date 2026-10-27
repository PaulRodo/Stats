"""Собирает статистику YouTube, Telegram, Instagram и TikTok и пишет data.json.
Ключи берутся из переменных окружения (в GitHub — из Secrets). Если ключей нет, платформа пропускается."""
import base64
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys

import requests


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
            vd = call("GET", B + "videos", params={"part": "snippet,statistics", "id": ids, "key": key}).get("items", [])
    except (RuntimeError, KeyError):
        pass  # у канала может не быть видео, это не ошибка
    hidden = s.get("hiddenSubscriberCount")
    subs = None if hidden else int(s["subscriberCount"])
    return {
        "title": it["snippet"]["title"], "followers": subs,
        "metrics": [["Подписчики", "скрыты" if hidden else subs], ["Просмотры", int(s["viewCount"])], ["Видео", int(s["videoCount"])]],
        "posts": [{"id": v["id"], "title": cut(v["snippet"]["title"]), "ts": v["snippet"]["publishedAt"],
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
        posts.append({"id": pid.group(1), "title": cut(text), "ts": t.group(1) if t else "", "views": parse_views(v.group(1))})
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
    if last and (today - dt.date.fromisoformat(last)).days < 7:
        return st["token"]
    try:
        d = call("GET", "https://graph.instagram.com/refresh_access_token",
                 params={"grant_type": "ig_refresh_token", "access_token": st["token"]})
        if d.get("access_token"):
            st.update(token=d["access_token"], refreshed=today.isoformat())
            with open(IG_STATE, "wb") as f:
                f.write(_fernet(pw).encrypt(json.dumps(st).encode("utf-8")))
    except RuntimeError:
        pass  # например, токену меньше суток: попробуем при следующем запуске
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
        m = call("GET", B + node + "/media", params={"fields": "id,caption,like_count,comments_count,timestamp", "limit": 20, "access_token": tok})
        rows = [{"id": x["id"], "title": cut(x.get("caption")), "ts": iso(x.get("timestamp", "")),
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
    state_save(pw, {"refresh_token": d.get("refresh_token") or st["refresh_token"]})
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
    try:
        vids = call("POST", "https://open.tiktokapis.com/v2/video/list/", headers=H, json={"max_count": 20},
                    params={"fields": "id,title,create_time,view_count,like_count,comment_count,share_count"})["data"].get("videos", [])
    except RuntimeError:
        pass
    rows = [{"id": x.get("id"), "title": cut(x.get("title")), "ts": dt.datetime.fromtimestamp(x["create_time"], dt.timezone.utc).isoformat(),
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


def notify(text):
    tok, chat = env("TG_BOT_TOKEN"), env("TG_ALERT_CHAT")
    if not (tok and chat):
        return False
    try:
        call("GET", f"https://api.telegram.org/bot{tok}/sendMessage", params={"chat_id": chat, "text": text})
        return True
    except RuntimeError:
        return False


def main():
    pw = env("DATA_PASSWORD")
    old = load_old(pw)
    hist, alerts = old.get("history", {}), old.get("alerts", {})
    arch = {(p["pid"], p.get("id") or p["title"]): p for p in old.get("archive", [])}
    now = dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    out = {"updated": now.isoformat(timespec="seconds"), "platforms": {}}
    for pid, fn in (("yt", youtube), ("tg", telegram), ("ig", instagram), ("tt", tiktok)):
        try:
            res = fn()
        except Exception as e:  # одна платформа не должна ломать остальные
            res = {"title": "", "error": str(e)[:200]}
        if res is None:
            continue
        for p in res.pop("posts", []):  # свежие публикации попадают в архив и обновляют свои цифры
            arch[(pid, p.get("id") or p["title"])] = {**p, "pid": pid}
        out["platforms"][pid] = res
        if res.get("followers") is not None:
            hist.setdefault(today, {})[pid] = res["followers"]
        if res.get("error"):
            if alerts.get(pid) != today and notify(f"⚠️ {NAMES[pid]}: {res['error']}"):
                alerts[pid] = today  # не чаще раза в сутки
        else:
            alerts.pop(pid, None)
    out["history"] = {d: hist[d] for d in sorted(hist)[-400:]}
    out["alerts"] = alerts
    out["archive"] = sorted(arch.values(), key=lambda p: p.get("ts", ""), reverse=True)[:1000]
    if pw:
        with open(ENC_FILE, "w", encoding="utf-8") as f:
            json.dump(encrypt_data(out, pw), f)
        if os.path.exists(DATA_FILE):
            os.remove(DATA_FILE)  # открытая копия больше не нужна
    else:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    print("Готово:", ", ".join(out["platforms"]) or "ни одна платформа не подключена")


if __name__ == "__main__":
    main()
