"""Poll creation, sending, closing, and the poll_answer -> sheet router.

The module keeps a few globals wired up by :func:`configure` (bot, sheets
client, config, timezone) so both the scheduler jobs and the aiogram handler
can reach them.
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InputPollOption, PollAnswer

import storage
from sheets import SheetColumnMissing, extract_score

logger = logging.getLogger("qpoll.polls")

router = Router()

_BOT = None
_SHEETS = None
_CFG = None
_TZ = None


def configure(bot, sheets_client, config):
    global _BOT, _SHEETS, _CFG, _TZ
    _BOT = bot
    _SHEETS = sheets_client
    _CFG = config
    _TZ = ZoneInfo(config["schedule"]["timezone"])


def _today_str():
    return datetime.now(_TZ).strftime("%d-%b-%Y")


def _partner_question(partner):
    return (
        "Bugungi kun holatiga %s xizmatining ishini qanday baholaysiz?"
        % partner["question_name"]
    )


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

async def _send_one_poll(chat_id, question, meta_extra):
    """Send a single poll and persist its metadata. Never raises for the
    common 'bot can't post here' cases – logs and returns instead so one bad
    group does not abort the whole daily run.
    """
    options = [InputPollOption(text=o) for o in _CFG["poll_options"]]
    try:
        msg = await _BOT.send_poll(
            chat_id=chat_id,
            question=question,
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
            is_closed=False,
        )
    except TelegramRetryAfter as e:
        logger.warning("Flood control for chat %s, sleeping %ss", chat_id,
                       e.retry_after)
        await asyncio.sleep(e.retry_after)
        return await _send_one_poll(chat_id, question, meta_extra)
    except TelegramForbiddenError as e:
        logger.error(
            "Cannot post polls in chat %s (%s) – bot blocked/kicked/not a "
            "member: %s", chat_id, meta_extra.get("group_label"), e,
        )
        return None
    except TelegramBadRequest as e:
        logger.error(
            "Cannot post poll in chat %s (%s): %s – check the bot has "
            "'Send Polls' permission there", chat_id,
            meta_extra.get("group_label"), e,
        )
        return None

    meta = {
        "poll_id": msg.poll.id,
        "date": _today_str(),
        "chat_id": chat_id,
        "message_id": msg.message_id,
        "question": question,
    }
    meta.update(meta_extra)
    storage.save_poll(meta)
    logger.info(
        "Sent %s poll to %s (%s): poll_id=%s message_id=%s",
        meta["poll_type"], chat_id, meta.get("group_label"), meta["poll_id"],
        msg.message_id,
    )
    return meta


async def send_daily_polls():
    """Send the 6 polls (1 own-service + 5 partner) to every configured group."""
    day = _today_str()
    logger.info("=== Daily poll run for %s ===", day)
    sent = 0
    for chat_id_str, group in _CFG["groups"].items():
        chat_id = int(chat_id_str)

        own_q = "%s\n\n%s" % (
            group["own_service_question"],
            _CFG["own_service_description"],
        )
        if await _send_one_poll(chat_id, own_q, {
            "poll_type": "own_service",
            "group_label": group["group_label"],
            "project_name": group["project_name"],
        }):
            sent += 1

        for partner in _CFG["partners"]:
            if await _send_one_poll(chat_id, _partner_question(partner), {
                "poll_type": "partner",
                "group_label": group["group_label"],
                "partner_key": partner["key"],
                "partner_sheet_name": partner["sheet_name"],
            }):
                sent += 1

    logger.info("Daily poll run finished: %d/%d polls sent",
                sent, len(_CFG["groups"]) * (1 + len(_CFG["partners"])))


async def close_daily_polls():
    """stopPoll every still-open poll for today. Votes already recorded stay;
    real-time processing of votes is unaffected because it is driven by
    poll_answer updates, not by the poll being open.
    """
    day = _today_str()
    polls = [p for p in storage.polls_for_date(day) if not p["closed"]]
    logger.info("Closing %d open poll(s) for %s", len(polls), day)
    for p in polls:
        try:
            await _BOT.stop_poll(p["chat_id"], p["message_id"])
            storage.mark_closed(p["poll_id"])
            logger.info("Closed poll_id=%s (%s)", p["poll_id"],
                        p.get("partner_key") or p.get("project_name"))
        except TelegramBadRequest as e:
            # Already closed, message deleted, etc. – not fatal.
            logger.warning("Could not stop poll_id=%s: %s", p["poll_id"], e)
            storage.mark_closed(p["poll_id"])
        except TelegramForbiddenError as e:
            logger.warning("Not allowed to stop poll_id=%s: %s", p["poll_id"], e)


# --------------------------------------------------------------------------
# Receiving votes
# --------------------------------------------------------------------------

@router.poll_answer()
async def on_poll_answer(poll_answer: PollAnswer):
    poll_id = poll_answer.poll_id
    user = poll_answer.user
    if user is None:
        # Anonymous poll answer – shouldn't happen (our polls are non-anonymous).
        logger.warning("poll_answer without a user on poll_id=%s – ignoring",
                       poll_id)
        return
    user_id = user.id
    username = user.username

    meta = storage.get_poll(poll_id)
    if meta is None:
        logger.warning(
            "poll_answer for unknown poll_id=%s from user %s (@%s) – "
            "no metadata, ignoring", poll_id, user_id, username,
        )
        return

    column_name = _CFG["voter_map"].get(str(user_id))
    if not column_name:
        logger.warning(
            "Unmapped voter: user_id=%s (@%s) name=%r voted on poll_id=%s "
            "(%s) – add them to config.json voter_map; skipping",
            user_id, username, user.full_name, poll_id,
            meta.get("partner_key") or meta.get("project_name"),
        )
        return

    option_ids = list(poll_answer.option_ids or [])
    previous = storage.get_previous_vote(poll_id, user_id)

    if not option_ids:
        value = None
        logger.info(
            "Vote RETRACTED: %s (user %s) on poll_id=%s (%s); was %s",
            column_name, user_id, poll_id,
            meta.get("partner_key") or meta.get("project_name"), previous,
        )
    else:
        idx = option_ids[0]
        try:
            option_text = _CFG["poll_options"][idx]
        except IndexError:
            logger.error("poll_answer option_id %s out of range on poll_id=%s",
                         idx, poll_id)
            return
        value = extract_score(option_text)
        if value is None:
            logger.error("Could not parse score from option %r", option_text)
            return
        verb = "CHANGED" if previous is not None and previous != value else "new"
        logger.info(
            "Vote %s: %s (user %s) on poll_id=%s (%s) -> %s%s",
            verb, column_name, user_id, poll_id,
            meta.get("partner_key") or meta.get("project_name"), value,
            "" if previous is None else " (was %s)" % previous,
        )

    if meta["poll_type"] == "own_service":
        tab = _CFG["sheets"]["projects_tab"]
        name_header = _CFG["sheets"]["project_name_header"]
        entity_name = meta["project_name"]
    else:
        tab = _CFG["sheets"]["partners_tab"]
        name_header = _CFG["sheets"]["partner_name_header"]
        entity_name = meta["partner_sheet_name"]

    try:
        await asyncio.to_thread(
            _SHEETS.record_score,
            tab, name_header, entity_name, meta["date"], column_name, value,
        )
    except SheetColumnMissing as e:
        logger.warning(
            "Vote from %s (user %s) not written – %s", column_name, user_id, e
        )
        return
    except Exception:
        logger.exception(
            "Failed to write vote to sheet for %s (user %s) poll_id=%s",
            column_name, user_id, poll_id,
        )
        return

    storage.record_vote(poll_id, user_id, value)
    logger.info(
        "Recorded %s=%s in tab %r for [%s / %s]",
        column_name, "cleared" if value is None else value, tab,
        meta["date"], entity_name,
    )
