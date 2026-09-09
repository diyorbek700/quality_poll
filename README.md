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

* **Partner polls** → tab **`Partners`**. One row per partner per day. Votes for
  the same partner from *either* group merge into the **same row**.
  `EGOV` in the poll text is written as **`ERI`** in the sheet (same partner).
* **Own-service poll** → tab **`Projects`**. The Nasiya group's poll writes the
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
2. **Disable group privacy** is *not* required for poll answers, but the bot
   **must be a member of both groups** with permission to **Send Polls**
   (in a group with restricted permissions, make it an admin).
3. Confirm the real numeric chat IDs. Supergroup IDs usually look like
   `-100xxxxxxxxxx`. Add the bot to each group and check the logs, or forward a
   message to [@RawDataBot](https://t.me/RawDataBot). Update `config.json` →
   `groups` if they differ from the placeholders.

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

To find a user_id: have them vote once while the bot runs — an unmapped voter is
logged as a warning with their `user_id`, `@username` and full name. Then add
them and they can re-vote.

### 4. Install & run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Useful flags:

```bash
python bot.py --send-now     # send today's polls right away, then keep running
python bot.py --close-now    # close today's polls right away, then keep running
```

Run it under a process manager (systemd, supervisor, `screen`/`tmux`, …) so it
stays up. The `poll_id → metadata` map is in `polls.db`, so a restart does not
lose the ability to route votes for polls already sent today.

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
