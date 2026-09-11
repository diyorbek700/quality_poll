"""Entry point: loads config, wires up the Sheets client, the aiogram
dispatcher and the APScheduler jobs, then starts long-polling.

Usage:
    python bot.py                # run the bot + scheduler
    python bot.py --send-now     # send today's polls immediately, then run normally
    python bot.py --close-now    # close today's polls immediately, then run normally
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

import polls
import storage
from sheets import SheetsClient

BASE = Path(__file__).parent
logger = logging.getLogger("qpoll")


def setup_logging():
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        BASE / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def load_config():
    with open(BASE / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    # minimal validation
    for key in ("schedule", "groups", "partners", "poll_options", "sheets"):
        if key not in cfg:
            raise SystemExit("config.json is missing the %r section" % key)
    if len(cfg["poll_options"]) != 10:
        logger.warning("Expected 10 poll options, found %d",
                       len(cfg["poll_options"]))
    return cfg


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            "Environment variable %s is not set – copy .env.example to .env "
            "and fill it in" % name
        )
    return value


async def check_groups(bot, cfg):
    """Verify the bot can reach every configured group and may post polls."""
    me = await bot.get_me()
    all_ok = True
    for cid, g in cfg["groups"].items():
        label = g.get("group_label", "?")
        try:
            chat = await bot.get_chat(int(cid))
        except Exception as e:
            all_ok = False
            logger.error(
                "[%s] chat_id=%s UNREACHABLE: %s – add @%s to that group as an "
                "admin; the bot logs the real chat_id on join (my_chat_member) "
                "and also answers /chatid inside the group. Put that id in "
                "config.json -> groups.",
                label, cid, e, me.username)
            continue
        try:
            member = await bot.get_chat_member(int(cid), me.id)
            status = member.status
        except Exception as e:
            status = "unknown (%s)" % e
        can_polls = getattr(member, "can_send_polls", None)
        ok = status in ("administrator", "creator") or can_polls is True
        all_ok = all_ok and ok
        logger.info(
            "[%s] chat_id=%s title=%r type=%s bot=%s can_send_polls=%s -> %s",
            label, cid, chat.title, chat.type, status, can_polls,
            "OK" if ok else "CANNOT POST POLLS",
        )
    logger.info("Group check %s", "PASSED" if all_ok else "FAILED")
    return all_ok


def resolve_google_credentials():
    """Either GOOGLE_CREDENTIALS_JSON (the service-account key, pasted whole
    into an env var — the easy option on PaaS hosts like Render/Railway) or
    GOOGLE_CREDENTIALS_PATH (a file on disk) must be set. Returns kwargs
    ready to pass to SheetsClient(...).
    """
    raw_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw_json:
        try:
            return {"credentials_info": json.loads(raw_json)}
        except json.JSONDecodeError as e:
            raise SystemExit("GOOGLE_CREDENTIALS_JSON is not valid JSON: %s" % e)

    creds_path = require_env("GOOGLE_CREDENTIALS_PATH")
    if not Path(creds_path).is_absolute():
        creds_path = str((BASE / creds_path).resolve())
    if not Path(creds_path).exists():
        raise SystemExit("Google credentials file not found: %s" % creds_path)
    return {"credentials_path": creds_path}


async def run(send_now=False, close_now=False, check_only=False):
    load_dotenv(BASE / ".env")
    token = require_env("TELEGRAM_BOT_TOKEN")
    cfg = load_config()

    if check_only:
        bot = Bot(token)
        try:
            await check_groups(bot, cfg)
        finally:
            await bot.session.close()
        return

    sheet_id = require_env("GOOGLE_SHEET_ID")
    creds_kwargs = resolve_google_credentials()

    storage.init_db()

    sheets = SheetsClient(sheet_id, cfg, **creds_kwargs)

    bot = Bot(token)
    me = await bot.get_me()
    logger.info("Authorized as @%s (id=%s)", me.username, me.id)

    dp = Dispatcher()
    dp.include_router(polls.router)
    polls.configure(bot, sheets, cfg)

    tz = ZoneInfo(cfg["schedule"]["timezone"])
    scheduler = AsyncIOScheduler(timezone=tz)

    send_h, send_m = (int(x) for x in cfg["schedule"]["send_time"].split(":"))
    scheduler.add_job(
        polls.send_daily_polls,
        CronTrigger(hour=send_h, minute=send_m),
        id="send_daily_polls",
        misfire_grace_time=3600,
    )
    if cfg["schedule"].get("close_polls_enabled"):
        close_h, close_m = (
            int(x) for x in cfg["schedule"]["close_time"].split(":")
        )
        scheduler.add_job(
            polls.close_daily_polls,
            CronTrigger(hour=close_h, minute=close_m),
            id="close_daily_polls",
            misfire_grace_time=3600,
        )
    scheduler.start()
    logger.info(
        "Scheduler started (tz=%s): send at %s, close at %s (%s)",
        tz, cfg["schedule"]["send_time"], cfg["schedule"]["close_time"],
        "enabled" if cfg["schedule"].get("close_polls_enabled") else "disabled",
    )

    if send_now:
        logger.info("--send-now: sending today's polls immediately")
        await polls.send_daily_polls()
    if close_now:
        logger.info("--close-now: closing today's polls immediately")
        await polls.close_daily_polls()

    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "poll", "poll_answer", "my_chat_member"],
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main():
    parser = argparse.ArgumentParser(description="Quality-assessment poll bot")
    parser.add_argument("--send-now", action="store_true",
                        help="send today's polls immediately on startup")
    parser.add_argument("--close-now", action="store_true",
                        help="close today's polls immediately on startup")
    parser.add_argument("--check", action="store_true",
                        help="check bot access to every configured group, then exit")
    args = parser.parse_args()

    setup_logging()
    try:
        asyncio.run(run(send_now=args.send_now, close_now=args.close_now,
                        check_only=args.check))
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code not in (0, None):
            raise
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
