"""Первое подключение YouTube Analytics: меняет код авторизации Google на refresh-токен
и сохраняет его в зашифрованном виде (youtube_state.enc). Запускается из workflow."""
import json
import sys
import urllib.parse

import requests

from collector import YT_STATE, _fernet, env

code = urllib.parse.unquote(env("YT_CODE"))
pw, cid, cs = env("TT_STATE_KEY"), env("YT_CLIENT_ID"), env("YT_CLIENT_SECRET")
if not (code and pw and cid and cs):
    sys.exit("Не хватает данных: нужны код и секреты YT_CLIENT_ID, YT_CLIENT_SECRET, TT_STATE_KEY.")
d = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
    "code": code, "client_id": cid, "client_secret": cs, "grant_type": "authorization_code",
    "redirect_uri": env("YT_REDIRECT_URI") or "https://paulrodo.github.io/Stats/"}).json()
if "refresh_token" not in d:
    sys.exit("Google не выдал токен: " + str(d.get("error_description") or d.get("error") or "неизвестная ошибка")[:200])
with open(YT_STATE, "wb") as f:
    f.write(_fernet(pw).encrypt(json.dumps({"refresh_token": d["refresh_token"]}).encode("utf-8")))
print("Готово: доступ к YouTube Analytics сохранён")
