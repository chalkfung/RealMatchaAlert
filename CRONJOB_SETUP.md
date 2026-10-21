# Hourly scans with cron-job.org

GitHub's own schedule skips many runs, so cron-job.org presses "Run workflow" for you every hour.
Your `scan.yml` stays in the repo exactly as below; nothing in it needs to change.

## 1. Your current `.github/workflows/scan.yml`

(This is the copy in your repo at github.com/chalkfung/RealMatchaAlert, checked 9 Oct.)

> **Update 21 Oct:** the personal-alerts version adds a `commands_only` option to this file. Upload the new `.github/workflows/scan.yml` from this folder; the copy below is the older one.

```yaml
name: Matcha stock scan

on:
  schedule:
    - cron: "7 * * * *" 
  workflow_dispatch:
    inputs:
      summary:
        description: "Send a full stock summary"
        type: boolean
        default: false

permissions:
  contents: write   # to save state.json between runs

concurrency:
  group: matcha-scan
  cancel-in-progress: false

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Scan
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          if [ "${{ inputs.summary }}" = "true" ]; then
            python matcha_alert.py --summary
          else
            python matcha_alert.py
          fi

      - name: Save state
        run: |
          if [ -n "$(git status --porcelain state.json)" ]; then
            git config user.name "matcha-alert-bot"
            git config user.email "matcha-alert-bot@users.noreply.github.com"
            git add state.json
            git commit -m "Update stock state [skip ci]"
            git pull --rebase --autostash || true
            git push
          fi
```

## 2. Create a GitHub token (one time)

1. On github.com, tap your profile picture, then **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Token name:** `cron-job matcha`
3. **Expiration:** 90 days or 1 year (GitHub emails you before it expires).
4. **Repository access:** *Only select repositories* → `chalkfung/RealMatchaAlert`
5. **Permissions → Repository permissions → Actions:** **Read and write**. Leave everything else at "No access".
6. Tap **Generate token** and copy it (starts with `github_pat_`). You can't see it again later.

## 3. Hourly scan job on cron-job.org

1. Sign up at https://cron-job.org (free) and tap **Create cronjob**.
2. **Common** tab:
   - Title: `Matcha hourly scan`
   - URL:
     ```
     https://api.github.com/repos/chalkfung/RealMatchaAlert/actions/workflows/scan.yml/dispatches
     ```
   - Execution schedule: **Every hour**, at minute **7**. Make sure the time zone is Asia/Singapore.
3. **Advanced** tab:
   - Request method: **POST**
   - Headers (add three):

     | Key | Value |
     |---|---|
     | `Authorization` | `Bearer github_pat_...your token...` |
     | `Accept` | `application/vnd.github+json` |
     | `X-GitHub-Api-Version` | `2022-11-28` |

   - Request body:
     ```json
     {"ref":"main"}
     ```
4. Save, then tap **Test run**. You should see status **204** (that means "accepted"), and a new run "via manual request" appears in your repo's Actions tab within a minute.

You don't need a second job for the 7am summary: the script sends it on the first scan at or after 7:00 Singapore time each day.

## Optional: faster replies to Telegram commands

The bot answers commands like `/items` and `/subscribe` when a run starts, so with hourly scans a reply can take up to an hour. To make it reply within about 10 minutes, add a second cron-job.org job that only reads commands (no shop scan, takes ~20 seconds):

- Same URL and the same three headers as above.
- Schedule: every 10 minutes, but skip the scan's minute so the two don't queue up together, e.g. minutes `2,12,22,32,42,52`.
- Request body:
  ```json
  {"ref":"main","inputs":{"commands_only":"true"}}
  ```

Only do this in a public repo (yours is), where runs are free. In a private repo each run is billed as a full minute, which comes to about 4,300 minutes a month, over the 2,000 free.

## If something goes wrong

| cron-job.org shows | Meaning |
|---|---|
| 204 | Working |
| 401 | Token is wrong, expired, or `Bearer ` is missing in front of it |
| 403 | Token doesn't have Actions: Read and write, or not for this repo |
| 404 | URL typo, or the repo/file name doesn't match |
| 422 | Body is wrong; it must be exactly `{"ref":"main"}` |

To stop everything: delete the token on GitHub (Settings → Developer settings → Fine-grained tokens) or disable the job on cron-job.org.
