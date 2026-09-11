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
| `config.json` | groups, partners, poll options, schedule, sheet header names, `voter_map` |
| `.env` / `.env.example` | secrets (token, sheet id, credentials path) |
| `requirements.txt` | dependencies |

## Setup

### 1. Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token.
2. Add the bot to **both groups** and make it an **admin** (the simplest way to
   guarantee it can send polls; a plain member works only if the group grants
   "Send Polls" to everyone).
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

To find a user_id, easiest first: have each person send **`/myid`** to the bot
(in the group, or in a DM) — it replies with their `user_id`, name and
username. You can also just have them vote once while the bot runs — an
unmapped voter is logged as a warning with their `user_id`, `@username` and
full name. Then add
them and they can re-vote.

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
the process if it dies. The `poll_id → metadata` map lives in `polls.db`, so a
restart mid-day does not lose the ability to route votes for polls already
sent that day.

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
