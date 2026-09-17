# Quality-assessment poll bot

A Telegram bot (aiogram) that sends daily quality-assessment polls to two
Telegram groups and records the votes into a Google Sheet with two tabs.

## What it does

Every day at a configurable time (default **10:00 Asia/Tashkent**) the bot sends
**6 polls to each of the two groups** (12 polls total):

| # | Poll | Nasiya group | Konveyer group |
|---|------|--------------|----------------|
| 1 | Own service | "Bugungu kun holatiga Nasiya platformasi xizmatini baholang" | "Bugungu kun holatiga Kredit Konveyer platformasi xizmatini baholang" |
| 2–6 | Partner (KATM, EGOV, MYID, MULTICARD, PLAYMOBILE) | "Bugungi kun holatiga {PARTNER} xizmatining ishini qanday baholaysiz?" | same |

All polls use the same 10 options ("1 — Juda yomon" … "10 — Mukammal / Zo'r"),
are **non-anonymous**, single-choice, and stay open until the bot closes them
(default **19:00**) — votes are still processed in real time regardless.

Voters may **change or retract** their vote; the bot treats every
`poll_answer` as an overwrite of that person's cell, and an empty answer as
clearing the cell.

### Where votes land

* **Partner polls** → the tab named in `config.json` → `sheets.partners_tab`
  (currently `Оценка эффективности Сторонних Сервисов`). One row per partner
  per day. Votes for the same partner from *either* group merge into the
  **same row**. The poll text says `EGOV` but the sheet row (and
  `partners[].sheet_name` in config) uses **`ERI`** — same partner, matching
  the existing header. `KATM` is written as the sheet's own **`КАТМ`**
  (Cyrillic) and `PLAYMOBILE` as **`Playmobile`** — match `config.json` to
  whatever your sheet actually has if you rename anything there.
* **Own-service poll** → the tab named in `sheets.projects_tab` (currently
  `Оценка эффективности проектов`). The Nasiya group's poll writes the
  `Fortuna Nasiya` row, the Konveyer group's poll writes the
  `Кредитный Конвейер` row.

Every day's row for each partner/project is created the moment the polls are
sent (blank, average formula only) — not only once the first vote comes in —
so today's date always shows up in the sheet immediately, even before anyone
has voted.

Each voter's score goes into the column whose header matches their name in
`config.json` → `voter_map`. The `Средняя оценка` column is never written to for
existing rows (a new row gets a fresh `AVERAGE` formula on creation if
`add_average_formula_on_new_row` is true).

## Files

| File | Purpose |
|------|---------|
| `bot.py` | entry point: env loading, dispatcher, APScheduler jobs, long-polling |
| `polls.py` | poll building/sending/closing + the `poll_answer` → sheet router |
| `sheets.py` | Google Sheets client: header lookup, find-row / append-row / update-cell, retry/backoff |
| `storage.py` | SQLite: `poll_id → metadata` map (survives restart) + last vote per user |
| `webserver.py` | tiny HTTP server (`/`, `/healthz`), only started when `$PORT` is set — see "Deploying to Render (free)" |
| `config.json` | groups, partners, poll options, schedule, sheet header names, `voter_map` |
| `.env` / `.env.example` | secrets (token, sheet id, credentials path or JSON) |
| `requirements.txt` | dependencies |
| `render.yaml` | Render Blueprint for a one-click free Web Service deploy |
| `quality-poll-bot.service.example` | example systemd unit for a plain VM / Background Worker deploy |

## Setup

### 1. Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token.
2. Add the bot to **both groups** and make it an **admin** (the simplest way to
   guarantee it can send polls; a plain member works only if the group grants
   "Send Polls" to everyone). The bot never calls any Telegram API that needs
   admin rights (no ban/kick/restrict/delete/pin anywhere in this codebase —
   sending/stopping polls needs neither), so when you promote it, open its
   **Edit Administrator Rights** screen and **uncheck every single right**.
   An admin with zero rights still bypasses a group's "only admins can send
   polls" restriction, which is the only reason it needs to be an admin at
   all — there's no upside to leaving `Ban users`, `Delete messages`, etc.
   switched on, only unnecessary risk if that admin's rights were ever
   misused by something other than this bot.
3. Confirm the real numeric chat IDs. The ones in `config.json` are guesses and
   a group's id changes if it is upgraded to a supergroup (`-100xxxxxxxxxx`).
   To get the real id:
   - the moment you add the bot, it logs a line like
     `my_chat_member: chat_id=-100123... title='Nasiya' ... -> bot is now 'administrator'`
     to `bot.log`, **or**
   - send **`/chatid`** in the group and the bot replies with the id.
   Put the correct ids into `config.json` → `groups` (keep the
   `project_name` / `group_label` / `own_service_question` values).
4. Verify everything is wired up:
   ```bash
   python bot.py --check      # checks bot access + poll permission for every group, then exits
   ```
   This also logs a warning per group listing any admin rights the bot holds
   that it never uses (`can_restrict_members`, `can_delete_messages`, etc.) —
   only a human admin can revoke them (the bot can't edit its own rights), so
   treat that warning as a to-do in Telegram's group settings, not something
   this codebase can fix on its own.

### 2. Google Sheets

1. In [Google Cloud Console](https://console.cloud.google.com/): create a
   project, enable the **Google Sheets API**.
2. Create a **service account**, then create a **JSON key** for it and download
   it (e.g. save as `service_account.json` next to `bot.py`).
3. Open the target spreadsheet and **Share** it with the service account's
   email (`...@...iam.gserviceaccount.com`) as an **Editor**.
4. The spreadsheet needs two tabs named `Partners` and `Projects`, each with a
   header row in row 1. Header names the bot looks for are in `config.json` →
   `sheets`. Adjust `date_header` / `partner_name_header` / `project_name_header`
   / `average_header` there if your sheet uses different text.

### 3. Config

```bash
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_PATH
```

Fill in `config.json` → `voter_map`: map each voter's **numeric Telegram
user_id** (string key) to the **exact column header** used in the sheets:

```json
"voter_map": {
  "123456789": "Bobur",
  "987654321": "Olimovv"
}
```

To find a user_id, easiest first: have each person open a DM with the bot and
send **`/start`** — Telegram only lets a bot message a user privately after
that user has started a chat with it, so this is also how each person
unlocks ever getting a private message from the bot at all. `/start` replies
with a short announcement and, from then on, they can also send **`/myid`**
(in the group or in the DM) to get their `user_id`, name and username. You
can also just have them vote once while the bot runs — an unmapped voter is
logged as a warning with their `user_id`, `@username` and full name. Then add
them to `voter_map` and they can re-vote.

### 4. Install & run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Useful flags:

```bash
python bot.py --check        # verify bot access to every group, then exit
python bot.py --send-now     # send today's polls right away, then keep running
python bot.py --close-now    # close today's polls right away, then keep running
```

### 5. Deploy (keep it running 24/7)

The scheduler only fires while the process is alive, so it needs to run under
a process manager on a server:

```bash
sudo cp quality-poll-bot.service.example /etc/systemd/system/quality-poll-bot.service
sudo nano /etc/systemd/system/quality-poll-bot.service   # set User and the real paths
sudo systemctl daemon-reload
sudo systemctl enable --now quality-poll-bot
journalctl -u quality-poll-bot -f   # watch it come up
```

supervisor, `screen`/`tmux`, or a container work too — anything that restarts
the process if it dies. The `poll_id → metadata` map lives in `polls.db`
(local SQLite, fast) **and** is mirrored to a `PollLog` tab in the spreadsheet
(auto-created on first use) every time a poll is sent or closed. On startup
the bot re-imports everything from `PollLog` into `polls.db` before doing
anything else. This matters specifically on hosts with no persistent disk
(Render's free tier included) — every redeploy wipes `polls.db`, so without
the sheet-backed copy, votes on any poll sent by the *previous* instance
would come back as "unknown poll_id" and be silently dropped. With it, a
redeploy mid-day is safe: the new instance recovers everything on startup.

#### Deploying to Render (free)

Render's **Background Worker** is the "obvious" fit but always requires a
paid instance. To stay on the **free Web Service** tier instead, the bot
also runs a tiny HTTP server (`webserver.py`) alongside its normal Telegram
long-polling — purely so Render has a port to bind to and a `/healthz` route
to check. It only starts when a `PORT` env var is present, which Render sets
automatically for Web Services and nothing else sets, so this is a no-op
everywhere else (VM, local dev, a Background Worker).

The catch: **free Web Services sleep after ~15 minutes with no HTTP
request**, and a sleeping process can't fire its internal scheduler. The fix
is a free external **uptime pinger** hitting `/healthz` every 5–10 minutes,
which counts as traffic and keeps the instance from ever going idle — so the
in-process scheduler keeps firing `send_time`/`close_time` exactly as
configured.

1. Push this repo to GitHub (already done if you're reading this from here).
2. Render dashboard → **New** → **Blueprint** → pick this repo (it reads
   [render.yaml](render.yaml) and creates a free Web Service with
   `healthCheckPath: /healthz` already set). Or do it by hand: **New** →
   **Web Service**, Build Command `pip install -r requirements.txt`, Start
   Command `python bot.py`, plan **Free**.
3. Fill in the env vars it prompts for (or add under **Environment**):
   | Key | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | your bot token |
   | `GOOGLE_SHEET_ID` | your spreadsheet id |
   | `GOOGLE_CREDENTIALS_JSON` | the **entire contents** of `service_account.json`, pasted as one line |
   Render's free tier has no persistent disk, so use `GOOGLE_CREDENTIALS_JSON`
   here rather than `GOOGLE_CREDENTIALS_PATH` — the bot reads the key straight
   out of the env var, no file needed.
4. `config.json` is committed to the repo, so it deploys as-is — edit it in
   git (groups, partners, `voter_map`, schedule) and push to update it; no
   dashboard step needed for that file.
5. Deploy, then check **Logs** for the same lines you see locally
   (`Authorized as @...`, `Connected to spreadsheet '...'`,
   `Scheduler started ...`, `Health-check web server listening on 0.0.0.0:...`).
   A restart loop means the logs will show why (missing env var, bad
   credentials JSON, etc.) — the bot fails fast with a clear message.
6. **Set up the pinger** (do this or the service sleeps and misses its
   schedule): create a free [UptimeRobot](https://uptimerobot.com) monitor
   (or [cron-job.org](https://cron-job.org)) hitting
   `https://<your-app>.onrender.com/healthz` every 5 minutes. `/healthz`
   returns `{"status": "ok", "uptime_seconds": ...}` — a shrinking
   `uptime_seconds` on repeated checks means it's still going to sleep
   between pings and the interval needs to be shorter.
7. Render's clock is UTC regardless of server region; `config.json` →
   `schedule.timezone` (`Asia/Tashkent`) is what the scheduler actually uses,
   so no timezone changes are needed there.

If you'd rather not deal with a pinger at all and don't mind paying, the
**Background Worker** path (no `webserver.py`/`PORT` involved, no sleeping)
is simpler: same build/start commands, plan any paid tier instead of Free,
and skip step 6 entirely.

## Logging

Logs go to both the console and `bot.log` (rotating, 5 MB × 5). They record:
polls sent, votes received (new / changed / retracted), sheet writes, unmapped
voters, permission problems, and Sheets API retries.

## Notes / assumptions

* Telegram regular polls have no native "description" field, so the own-service
  description ("Iltimos tanlagan bahoingizga izoh qoldiring") is appended to the
  question text on its own line.
* Partner sheet names default to `KATM / ERI / MyID / Multicard / PlayMobile`.
  **Verify these against the real `Сервис` column values** and edit
  `config.json` → `partners[].sheet_name` if they differ — a mismatch just
  creates a new row with the wrong name, it does not crash.
* Sheets API calls retry up to 5× with exponential backoff on 429/5xx.
* If the bot can't post in a group, that group is logged and skipped; the other
  group and the scheduler are unaffected.
