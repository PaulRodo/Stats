"""Собирает статистику YouTube, Telegram, Instagram и TikTok и пишет data.json.
Ключи берутся из переменных окружения (в GitHub — из Secrets). Если ключей нет, платформа пропускается."""
import datetime as dt
import json
import os
import re

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
    err = d.get("error") if isinstance(d, dict) else None
    if not r.ok or err or (isinstance(d, dict) and d.get("ok") is False):
        msg = (err.get("message") if isinstance(err, dict) else err) or d.get("description") or ""
        if isinstance(err, dict) and err.get("code") not in (None, "ok"):
            msg = msg or str(err.get("code"))
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
        vids = call("GET", B + "playlistItems", params={"part": "contentDetails", "maxResults": 8, "playlistId": pl, "key": key})
        ids = ",".join(x["contentDetails"]["videoId"] for x in vids.get("items", []))
        if ids:
            vd = call("GET", B + "videos", params={"part": "snippet,statistics", "id": ids, "key": key}).get("items", [])
    except RuntimeError:
        pass
    hidden = s.get("hiddenSubscriberCount")
    subs = None if hidden else int(s["subscriberCount"])
    return {
        "title": it["snippet"]["title"], "followers": subs,
        "metrics": [["Подписчики", "скрыты" if hidden else subs], ["Просмотры", int(s["viewCount"])], ["Видео", int(s["videoCount"])]],
        "cols": ["Видео", "Дата", "Просмотры", "Лайки", "Комм."],
        "rows": [[cut(v["snippet"]["title"]), day(v["snippet"]["publishedAt"]), int(v["statistics"].get("viewCount", 0)),
                  int(v["statistics"].get("likeCount", 0)), int(v["statistics"].get("commentCount", 0))] for v in vd],
    }


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
    return {"title": title, "followers": n, "metrics": [["Подписчики", n]],
            "note": "Bot API отдаёт только число подписчиков. Просмотры постов можно добавить через Telethon."}


def instagram():
    tok, uid = env("IG_TOKEN"), env("IG_USER_ID")
    if not (tok and uid):
        return None
    B = "https://graph.facebook.com/v21.0/"
    u = call("GET", B + uid, params={"fields": "username,followers_count,follows_count,media_count", "access_token": tok})
    rows = []
    try:
        m = call("GET", B + uid + "/media", params={"fields": "caption,like_count,comments_count,timestamp", "limit": 8, "access_token": tok})
        rows = [[cut(x.get("caption")), day(x.get("timestamp")), x.get("like_count", 0), x.get("comments_count", 0)] for x in m["data"]]
    except RuntimeError:
        pass
    return {
        "title": "@" + u["username"], "followers": u["followers_count"],
        "metrics": [["Подписчики", u["followers_count"]], ["Подписки", u["follows_count"]], ["Публикации", u["media_count"]]],
        "cols": ["Публикация", "Дата", "Лайки", "Комм."], "rows": rows,
    }


def tiktok():
    ck, cs, rt = env("TT_CLIENT_KEY"), env("TT_CLIENT_SECRET"), env("TT_REFRESH_TOKEN")
    token = env("TT_ACCESS_TOKEN")
    if not token:
        if not (ck and cs and rt):
            return None
        d = call("POST", "https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": ck, "client_secret": cs, "grant_type": "refresh_token", "refresh_token": rt})
        token = d.get("access_token")
        if not token:
            raise RuntimeError("не удалось обновить токен, получите новый refresh token")
    H = {"Authorization": "Bearer " + token}
    u = call("GET", "https://open.tiktokapis.com/v2/user/info/", headers=H,
             params={"fields": "display_name,follower_count,likes_count,video_count"})["data"]["user"]
    rows = []
    try:
        v = call("POST", "https://open.tiktokapis.com/v2/video/list/", headers=H, json={"max_count": 8},
                 params={"fields": "title,create_time,view_count,like_count,comment_count"})["data"]["videos"]
        rows = [[cut(x.get("title")), dt.datetime.fromtimestamp(x["create_time"], dt.timezone.utc).strftime("%Y-%m-%d"),
                 x.get("view_count", 0), x.get("like_count", 0), x.get("comment_count", 0)] for x in v]
    except RuntimeError:
        pass
    return {
        "title": u["display_name"], "followers": u["follower_count"],
        "metrics": [["Подписчики", u["follower_count"]], ["Лайки", u["likes_count"]], ["Видео", u["video_count"]]],
        "cols": ["Видео", "Дата", "Просмотры", "Лайки", "Комм."], "rows": rows,
    }


def main():
    old = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", encoding="utf-8") as f:
                old = json.load(f)
        except ValueError:
            pass
    hist = old.get("history", {})
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    out = {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "platforms": {}, "history": hist}
    for pid, fn in (("yt", youtube), ("tg", telegram), ("ig", instagram), ("tt", tiktok)):
        try:
            res = fn()
        except Exception as e:  # одна платформа не должна ломать остальные
            res = {"title": "", "error": str(e)[:200]}
        if res is None:
            continue
        out["platforms"][pid] = res
        if res.get("followers") is not None:
            hist.setdefault(today, {})[pid] = res["followers"]
    out["history"] = {d: hist[d] for d in sorted(hist)[-120:]}
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("Готово:", ", ".join(out["platforms"]) or "ни одна платформа не подключена")


if __name__ == "__main__":
    main()
