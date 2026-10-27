"""Один раз обменивает код авторизации Instagram на долгоживущий токен и сохраняет его в зашифрованном виде."""
import datetime as dt
import hashlib
import json
import sys

from collector import IG_STATE, _fernet, call, env


def main():
    code = env("IG_CODE").split("#")[0].strip()  # Instagram дописывает в конец ссылки «#_»
    app_id, secret = env("IG_APP_ID"), env("IG_APP_SECRET")
    pw, redirect = env("TT_STATE_KEY"), env("IG_REDIRECT_URI")
    if not (code and app_id and secret and pw and redirect):
        sys.exit("Не хватает данных: проверьте секреты IG_APP_ID, IG_APP_SECRET, TT_STATE_KEY и сам код.")
    try:
        d = call("POST", "https://api.instagram.com/oauth/access_token", data={
            "client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
            "redirect_uri": redirect, "code": code})
        if isinstance(d.get("data"), list) and d["data"]:
            d = d["data"][0]
        short = d.get("access_token")
        if not short:
            sys.exit("Instagram не вернул токен.")
        long_ = call("GET", "https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": secret, "access_token": short})
    except RuntimeError as e:
        sys.exit(f"Instagram отклонил запрос ({e}). Код живёт около часа и работает один раз: получите новый.")
    token = long_.get("access_token")
    if not token:
        sys.exit("Instagram не вернул долгоживущий токен.")
    state = {"origin": hashlib.sha256(env("IG_TOKEN").encode("utf-8")).hexdigest(), "token": token,
             "refreshed": dt.datetime.now(dt.timezone.utc).date().isoformat()}
    with open(IG_STATE, "wb") as f:
        f.write(_fernet(pw).encrypt(json.dumps(state).encode("utf-8")))
    print("Готово: Instagram подключён, токен сохранён в зашифрованном виде.")


if __name__ == "__main__":
    main()
