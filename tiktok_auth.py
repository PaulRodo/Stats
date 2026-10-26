"""Один раз обменивает код авторизации TikTok на токен и сохраняет его в зашифрованном виде."""
import sys

from collector import call, env, state_save, TT_TOKEN_URL


def main():
    code, ck, cs = env("TT_CODE"), env("TT_CLIENT_KEY"), env("TT_CLIENT_SECRET")
    pw, redirect = env("TT_STATE_KEY"), env("TT_REDIRECT_URI")
    if not (code and ck and cs and pw and redirect):
        sys.exit("Не хватает данных: проверьте секреты TT_CLIENT_KEY, TT_CLIENT_SECRET, TT_STATE_KEY и код.")
    try:
        d = call("POST", TT_TOKEN_URL, data={"client_key": ck, "client_secret": cs, "code": code,
                                             "grant_type": "authorization_code", "redirect_uri": redirect})
    except RuntimeError as e:
        sys.exit(f"TikTok отклонил код ({e}). Код живёт несколько минут и работает один раз: получите новый.")
    if not d.get("refresh_token"):
        sys.exit("TikTok не вернул токен для обновления.")
    state_save(pw, {"refresh_token": d["refresh_token"]})
    print("Готово: TikTok подключён, токен сохранён в зашифрованном виде.")


if __name__ == "__main__":
    main()
