#!/usr/bin/env python3
"""Дополнительный анализ для приложения: обложки, слабые места роликов и идеи из комментариев.

Что делает: расшифровывает data.enc (пароль DATA_PASSWORD), считает три вещи и пишет результат
в insights.enc тем же способом шифрования. Новых ключей и токенов не нужно: используется
YT_API_KEY, который уже есть у collector.py. Запускать после collector.py, не чаще раза в сутки
(скрипт сам пропускает запуск, если результат свежий; принудительно: python analyze.py --force;
проверка без тяжёлых пакетов: python analyze.py --due, код возврата 1 значит «пока не нужно»).

Нужные пакеты: pip install cryptography numpy pillow opencv-python-headless scikit-learn youtube-transcript-api
(последний необязателен: без него слабые места показываются без текста из ролика).
"""
import base64
import datetime as dt
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter

ENC, OUT = "data.enc", "insights.enc"
FRESH_HOURS = 20


def env(k):
    return os.environ.get(k, "").strip()


# ---------- шифрование: тот же формат, что у data.enc ----------
def _aes_key(pw, salt, it):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=it).derive(pw.encode("utf-8"))


def decrypt_data(box, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    b = base64.b64decode
    return json.loads(AESGCM(_aes_key(pw, b(box["salt"]), box["iter"])).decrypt(b(box["iv"]), b(box["ct"]), None))


def encrypt_data(obj, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_aes_key(pw, salt, 200000)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "iter": 200000, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def http(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (stats-analyzer)"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def median(a):
    s = sorted(a)
    return (s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2) if s else 0


# ---------- 1. анализ обложек ----------
def thumb_features(raw):
    """Простые признаки картинки: яркость, контраст, насыщенность, пестрота, тон, детали, лицо."""
    import numpy as np
    from PIL import Image
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize((320, 180))
    a = np.asarray(img, dtype=float)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    sat = np.asarray(img.convert("HSV"), dtype=float)[..., 1].mean() / 255
    rg, yb = r - g, 0.5 * (r + g) - b
    colorful = (np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean())) / 100
    f = {"brightness": lum.mean() / 255, "contrast": lum.std() / 255, "sat": sat, "color": colorful, "warm": (r.mean() - b.mean()) / 255}
    try:
        import cv2
        gray = lum.astype("uint8")
        f["edges"] = float(cv2.Canny(gray, 100, 200).mean() / 255)
        cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cas.detectMultiScale(gray, 1.1, 5, minSize=(24, 24))
        f["face"] = max((w * h for (_, _, w, h) in faces), default=0) / (320 * 180)
    except Exception:
        gy, gx = np.gradient(lum)
        f["edges"] = float(np.hypot(gx, gy).mean() / 255)
    return {k: round(float(v), 4) for k, v in f.items()}


def perm_p(a, b, n=1000):
    import numpy as np
    rng = np.random.default_rng(0)
    obs = abs(np.mean(a) - np.mean(b))
    allv, na, hits = np.array(list(a) + list(b)), len(a), 0
    for _ in range(n):
        rng.shuffle(allv)
        hits += abs(allv[:na].mean() - allv[na:].mean()) >= obs
    return (hits + 1) / (n + 1)


def cover_analysis(d, cache):
    vids = [p for p in d.get("archive", []) if p.get("pid") == "yt" and p.get("id")]
    vids.sort(key=lambda p: p.get("ts", ""), reverse=True)
    vids = vids[:80]
    use_ctr = sum(isinstance(p.get("ctr"), (int, float)) for p in vids) >= 8
    metric = "ctr" if use_ctr else "views"
    # результат считаем относительно обычного видео того же формата (короткие и длинные отличаются сами по себе)
    base = {}
    for p in vids:
        v = p.get("ctr") if use_ctr else p.get("views")
        if isinstance(v, (int, float)) and v > 0:
            base.setdefault(p.get("fmt") or "", []).append(v)
    base = {k: median(v) for k, v in base.items()}
    rows = []
    for p in vids:
        v = p.get("ctr") if use_ctr else p.get("views")
        if not isinstance(v, (int, float)) or v <= 0 or not base.get(p.get("fmt") or ""):
            continue
        vid = p["id"]
        if vid not in cache:
            try:
                cache[vid] = thumb_features(http("https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid))
            except Exception as e:
                print("обложка %s не загружена: %s" % (vid, e))
                continue
        rows.append({"id": vid, "t": p.get("title", ""), "v": v, "rel": v / base[p.get("fmt") or ""], "f": cache[vid]})
    if len(rows) < 10:
        return None
    findings = []
    labels = ["brightness", "contrast", "sat", "color", "warm", "edges"]
    for k in labels + ["face"]:
        if k == "face":
            hi = [r["rel"] for r in rows if r["f"].get("face", 0) > 0.02]
            lo = [r["rel"] for r in rows if r["f"].get("face", 0) <= 0.02]
        else:
            med = median([r["f"][k] for r in rows if k in r["f"]])
            hi = [r["rel"] for r in rows if k in r["f"] and r["f"][k] > med]
            lo = [r["rel"] for r in rows if k in r["f"] and r["f"][k] <= med]
        if len(hi) < 4 or len(lo) < 4:
            continue
        mh, ml = sum(hi) / len(hi), sum(lo) / len(lo)
        if mh <= 0 or ml <= 0:
            continue
        ratio = mh / ml
        if 1 / 1.15 < ratio < 1.15:
            continue
        findings.append({"k": k, "side": "hi" if ratio > 1 else "lo", "x": round(max(ratio, 1 / ratio), 2), "n": [len(hi), len(lo)], "p": round(perm_p(hi, lo), 3)})
    findings.sort(key=lambda f: -f["x"])
    rs = sorted(rows, key=lambda r: -r["rel"])
    pick = lambda r: {"id": r["id"], "t": r["t"][:80], "v": r["v"]}
    return {"metric": metric, "n": len(rows), "findings": findings[:6], "best": [pick(r) for r in rs[:3]], "worst": [pick(r) for r in rs[-3:][::-1]]}


# ---------- 2. слабые места роликов: что говорили в этот момент ----------
def get_transcript(vid):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception:
        return None
    langs = ["ru", "en"]
    try:
        tr = YouTubeTranscriptApi().fetch(vid, languages=langs)   # новые версии пакета
        return [(s.start, s.text) for s in tr]
    except AttributeError:
        tr = YouTubeTranscriptApi.get_transcript(vid, languages=langs)   # старые версии
        return [(s["start"], s["text"]) for s in tr]
    except Exception:
        return None


def watch_analysis(d):
    rt = d.get("retention") or {}
    out = {}
    for vid, v in list(rt.items())[:12]:
        tr = get_transcript(vid)
        if not tr:
            continue
        at = (v.get("w") or {}).get("at")
        near = " ".join(t for s, t in tr if at is not None and at - 8 <= s <= at + 4)
        hook = " ".join(t for s, t in tr if s <= 20)
        item = {k: re.sub(r"\s+", " ", x).strip()[:220] for k, x in (("near", near), ("hook", hook)) if x.strip()}
        if item:
            out[vid] = item
    return out or None


# ---------- 3. идеи из комментариев ----------
STOP = set("это как что для или при так все его еще уже там тут вот про над под без чем нет они она оно мне нас вас был была были будет можно очень просто только потому когда если надо тоже даже спасибо видео канал пожалуйста сделайте сделай сделать чтобы хочу хотел хотелось покажите покажи расскажите расскажи снимите сними снять разберите разбери добавьте прошу полезен нужно нужен круто было the and for with this that from your you are was will have has not but can how what why they them their just very".split())
REQ = re.compile(r"(сдела(й|йте|ть)|сними(те)?|снять|расскаж(и|ите)|разбер(и|ите)|обзор|покаж(и|ите)|можно ли|а можно|хочу,? чтобы|хотел(ось)? бы|было бы круто|добавьте|прошу|make a video|can you|could you|please (make|do|show)|do a (video|review)|tutorial|review)", re.I)


def fetch_comments(vids, key):
    items = []
    for p in vids:
        q = {"part": "snippet", "videoId": p["id"], "maxResults": 100, "order": "relevance", "textFormat": "plainText", "key": key}
        try:
            j = json.loads(http("https://www.googleapis.com/youtube/v3/commentThreads?" + urllib.parse.urlencode(q)))
        except Exception as e:   # комментарии выключены или квота
            print("комментарии %s недоступны: %s" % (p["id"], e))
            continue
        for it in j.get("items", []):
            s = it["snippet"]["topLevelComment"]["snippet"]
            t = re.sub(r"https?://\S+", "", s.get("textDisplay", "")).strip()
            if len(t) >= 12:
                items.append({"v": p["id"], "t": t, "lk": int(s.get("likeCount", 0))})
    seen, uniq = set(), []
    for x in items:
        k = x["t"].lower()[:80]
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def tokens(t):
    ws = [w for w in re.findall(r"[а-яёa-z]{4,}", t.lower()) if w not in STOP]
    return [(w[:6], w) for w in ws]


def cluster(items, k_max=8):
    from sklearn.cluster import KMeans
    from sklearn.feature_extraction.text import TfidfVectorizer
    toks = [tokens(x["t"]) for x in items]
    stems = [[s for s, _ in t] for t in toks]
    forms = {}
    for t in toks:
        for s, w in t:
            forms.setdefault(s, Counter())[w] += 1
    if len(items) < 10:
        return []
    vec = TfidfVectorizer(analyzer=lambda x: x, min_df=2)
    try:
        X = vec.fit_transform(stems)
    except ValueError:
        return []
    if X.shape[1] < 3:
        return []
    k = max(2, min(k_max, len(items) // 6))
    km = KMeans(n_clusters=k, n_init=5, random_state=0).fit(X)
    names = vec.get_feature_names_out()
    out = []
    for c in range(k):
        idx = [i for i, l in enumerate(km.labels_) if l == c]
        if len(idx) < 3:
            continue
        top = km.cluster_centers_[c].argsort()[::-1][:3]
        label = ", ".join(forms[names[i]].most_common(1)[0][0] for i in top if names[i] in forms)
        mem = sorted((items[i] for i in idx), key=lambda x: -x["lk"])
        out.append({"t": label, "n": len(idx), "lk": sum(x["lk"] for x in mem), "ex": [x["t"][:160] for x in mem[:2]],
                    "v": [v for v, _ in Counter(x["v"] for x in mem).most_common(2)]})
    return sorted(out, key=lambda c: -c["n"])


def comments_analysis(d):
    key = env("YT_API_KEY")
    if not key:
        return None
    vids = [p for p in d.get("archive", []) if p.get("pid") == "yt" and p.get("id")]
    vids.sort(key=lambda p: p.get("ts", ""), reverse=True)
    items = fetch_comments(vids[:15], key)
    if len(items) < 10:
        return None
    reqs = [x for x in items if REQ.search(x["t"])]
    return {"n": len(items), "ideas": cluster(reqs, 6), "themes": cluster(items, 8)}


# ---------- запуск ----------
def main():
    pw = env("DATA_PASSWORD")
    if not pw or not os.path.exists(ENC):
        print("Нет data.enc или DATA_PASSWORD, анализ пропущен")
        return
    with open(ENC, encoding="utf-8") as f:
        d = decrypt_data(json.load(f), pw)
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = decrypt_data(json.load(f), pw)
        except Exception:
            old = {}
    if "--force" not in sys.argv and old.get("ts"):
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(old["ts"])).total_seconds() / 3600
        if age < FRESH_HOURS:
            print("Результат свежий (%.1f ч), анализ пропущен" % age)
            sys.exit(1 if "--due" in sys.argv else 0)   # --due: код 1 = запускать не нужно
    if "--due" in sys.argv:
        return
    out = dict(old)
    cache = out.get("cover_cache", {})
    for name, fn in (("cover", lambda: cover_analysis(d, cache)), ("watch", lambda: watch_analysis(d)), ("comments", lambda: comments_analysis(d))):
        try:
            r = fn()
        except Exception as e:   # один сбой не должен стирать остальное
            print("%s: пропущено (%s)" % (name, e))
            continue
        if r:
            out[name] = r
    out["cover_cache"] = cache
    out["ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(encrypt_data(out, pw), f)
    print("Анализ готов:", ", ".join(k for k in ("cover", "watch", "comments") if k in out) or "ничего нового")


if __name__ == "__main__":
    main()
