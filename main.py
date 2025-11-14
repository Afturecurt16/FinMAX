import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Literal
from pydantic import BaseModel
from maxapi import Bot, Dispatcher, F
from maxapi.types import BotStarted, Command, MessageCreated, MessageCallback

from schedule import open_schedule_menu
from groups_schedule import (
    open_groups_menu,
    try_handle_group_message,
    reset_groups_flow_for,
)
from teachers_schedule import (
    open_teachers_menu,
    try_handle_teacher_message,
    reset_teachers_flow_for,
)
from homework import (
    open_homework_menu,
    open_watch_menu,
    register_homework_handlers,
    homework_is_waiting_group,
    _start_add_flow,
    homework_is_adding,       
    handle_add_message,       
)


STATE = {}

def _st(event):
    return STATE.setdefault((event.chat_id, event.user_id), {})

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s"
)
log = logging.getLogger("finashka-max-bot")

WELCOME_TEXT = (
    "Привет! 👋\n"
    "Я — помощник студентов твоего университета. "
    "Могу напоминать о парах и дз, хранить расписание и показывать дз других групп.\n\n"
    "Выбери одну из опций ниже:"
)

BOOT_TS = time.time()
OLD_EVENT_SLOP = 1.5

def _to_epoch_seconds(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 10**12 else float(value)
    if isinstance(value, str):
        try:
            s = value.rstrip("Z")
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return None
    if isinstance(value, datetime):
        return value.timestamp()
    return None

def _extract_event_ts(event):
    for key in ("timestamp", "created_at", "date", "ts", "created_ts", "sent_at", "time", "created"):
        ts = _to_epoch_seconds(getattr(event, key, None))
        if ts is not None:
            return ts
    msg = getattr(event, "message", None)
    if msg:
        for key in ("timestamp", "created_at", "date", "ts", "created_ts", "sent_at", "time", "created"):
            ts = _to_epoch_seconds(getattr(msg, key, None))
            if ts is not None:
                return ts
        body = getattr(msg, "body", None)
        if body:
            for key in ("timestamp", "created_at", "date", "ts", "created_ts", "sent_at", "time", "created"):
                ts = _to_epoch_seconds(getattr(body, key, None))
                if ts is not None:
                    return ts
    return None

def _is_old_event(event):
    ts = _extract_event_ts(event)
    return ts is not None and ts < (BOOT_TS - OLD_EVENT_SLOP)

def _is_from_bot(message):
    author = getattr(message, "author", None) or getattr(message, "from_", None) or getattr(message, "sender", None)
    for attr in ("is_bot", "bot", "isBot"):
        if author and getattr(author, attr, None) is True:
            return True
    try:
        bot_id = getattr(message.bot, "id", None)
        author_id = getattr(author, "id", None) if author else None
        if bot_id is not None and author_id is not None and bot_id == author_id:
            return True
    except Exception:
        pass
    return False

TOKEN = os.getenv("MAX_TOKEN")
if not TOKEN:
    with open("token.txt", "r", encoding="utf-8") as f:
        TOKEN = f.readline().strip()

bot = Bot(TOKEN)
dp = Dispatcher()

class InlineKeyboardAttachment(BaseModel):
    type: Literal["inline_keyboard"]
    payload: dict

def build_main_menu_attachment() -> InlineKeyboardAttachment:
    return InlineKeyboardAttachment(
        type="inline_keyboard",
        payload={
            "buttons": [[
                {"type": "message", "text": "Расписание"},   
                {"type": "message", "text": "Домашняя работа"},
            ]]
        },
    )

def main_menu_kwargs(text: str):
    return {"text": text, "attachments": [build_main_menu_attachment()]}


@dp.bot_started()
async def on_bot_started(event: BotStarted):
    if _is_old_event(event):
        return
    await event.bot.send_message(
        chat_id=event.chat_id,
        **main_menu_kwargs(WELCOME_TEXT),
    )

@dp.message_created(Command("start"))
async def on_start(event: MessageCreated):
    if _is_old_event(event) or _is_from_bot(event.message):
        return
    await event.message.answer(**main_menu_kwargs(WELCOME_TEXT))

@dp.message_created(F.message.body.text == "Расписание")
async def on_schedule_menu(event: MessageCreated):
    if _is_old_event(event) or _is_from_bot(event.message):
        return

    reset_groups_flow_for(event)
    reset_teachers_flow_for(event)

    await open_schedule_menu(event)

@dp.message_created(F.message.body.text == "Домашняя работа")
async def on_homework_menu(event: MessageCreated):
    if _is_old_event(event) or _is_from_bot(event.message):
        return
    await open_homework_menu(event)


@dp.message_created(F.message.body.text == "Почта")
async def on_mail_menu(event: MessageCreated):
    if _is_old_event(event) or _is_from_bot(event.message):
        return
    await event.message.answer("Здесь будет модуль проверки почты ✉️")

@dp.message_callback(F.callback.payload == "hw:watch")
async def on_watch_menu(event: MessageCallback):
    if _is_old_event(event):
        return
    await open_watch_menu(event)

@dp.message_callback(F.callback.payload == "hw:add")
async def on_add_menu(event: MessageCallback):
    if _is_old_event(event):
        return
    await _start_add_flow(event)

@dp.message_created(F.message.body.text == "⬅️ В меню")
async def on_back_to_main(event: MessageCreated):
    if _is_old_event(event) or _is_from_bot(event.message):
        return
    await event.message.answer(**main_menu_kwargs(WELCOME_TEXT))

@dp.message_created()
async def multiplex(event: MessageCreated):
    if _is_old_event(event) or _is_from_bot(event.message):
        return

    body = getattr(event.message, "body", None) or event.message
    text = (getattr(body, "text", None) or "").strip()
    payload = getattr(body, "payload", None)

    if homework_is_adding(event):
        await handle_add_message(event)
        return

    if text and homework_is_waiting_group(event):
        await open_watch_menu(event)
        return

    if payload == "sched:groups" or text == "Группы":
        reset_teachers_flow_for(event)
        await open_groups_menu(event)
        return

    if payload == "sched:teachers" or text == "Преподаватели":
        reset_groups_flow_for(event)
        await open_teachers_menu(event)
        return

    if await try_handle_group_message(event):
        return

    if await try_handle_teacher_message(event):
        return

register_homework_handlers(dp, bot)

def _get_payload_text(event):
    body = getattr(event.message, "body", None) or event.message
    p = getattr(body, "payload", None)
    if p is None:
        return None
    if isinstance(p, str):
        return p
    if isinstance(p, dict):
        for k in ("payload", "cmd", "command", "action", "type", "event", "data"):
            v = p.get(k)
            if isinstance(v, str):
                return v
        for v in p.values():
            if isinstance(v, str):
                return v
    return None


logging.getLogger("groups_schedule").setLevel(logging.INFO)
logging.getLogger("maxapi").setLevel(logging.INFO)

async def main():
    try:
        await bot.delete_webhook()
    except Exception:
        log.warning("Не удалось удалить webhook, продолжаю...")

    log.warning("✅ Бот запущен в MAX (polling)…")
    await dp.start_polling(bot)

@dp.message_created()
async def multiplex(event: MessageCreated):
    if _is_old_event(event) or _is_from_bot(event.message):
        return

    body = getattr(event.message, "body", None) or event.message
    text = (getattr(body, "text", None) or "").strip()
    payload_text = _get_payload_text(event)

    if homework_is_adding(event):
        await handle_add_message(event)
        return

    if text and homework_is_waiting_group(event):
        await open_watch_menu(event)
        return

    if text == "⬅️ В расписание" or payload_text == "sched:root":
        reset_groups_flow_for(event)
        reset_teachers_flow_for(event)
        await open_schedule_menu(event)
        return

    if payload_text == "sched:groups" or text == "Группы":
        reset_teachers_flow_for(event)
        reset_groups_flow_for(event)
        await open_groups_menu(event)
        return

    if payload_text == "sched:teachers" or text == "Преподаватели":
        reset_groups_flow_for(event)
        reset_teachers_flow_for(event)
        await open_teachers_menu(event)
        return

    if await try_handle_group_message(event):
        return

    if await try_handle_teacher_message(event):
        return


if __name__ == "__main__":
    asyncio.run(main())
