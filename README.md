# Stats
## Instagram through Meta Business / Facebook Login

For automatic renewal with a Meta Business system-user token, configure these GitHub Actions secrets:

- `META_SYSTEM_USER_TOKEN` — the expiring (60-day) System User access token created for the Meta app in Business Settings.
- `IG_APP_ID` and `IG_APP_SECRET` — credentials for that same Meta app (already used by the project).
- `TT_STATE_KEY` — encryption password already used by this project.
- `META_PAGE_ID` — optional; set only when the system user can see more than one Facebook Page linked to an Instagram professional account.

Grant the system user access to the Facebook Page connected to the Instagram professional account, and ensure the app/token has `instagram_basic`, `pages_show_list`, and `pages_read_engagement`. The collector uses the system-user token to retrieve the Page access token from `/me/accounts`, then uses that Page token for Instagram Graph API requests.

The collector refreshes an expiring system-user token about every 30 days via Meta's documented `fb_exchange_token` flow, then stores the rotated token in the encrypted `instagram_facebook_state.enc` file. The workflow commits that encrypted state file. The raw token is not written to the repository or workflow logs. The initial `META_SYSTEM_USER_TOKEN` secret acts as a bootstrap value; keep it unchanged unless you intentionally replace the system-user token.
