from typing import Literal
from pydantic import BaseModel
from maxapi.types import MessageCreated


class InlineKeyboardAttachment(BaseModel):
    type: Literal["inline_keyboard"]
    payload: dict


def schedule_root_kb() -> InlineKeyboardAttachment:
    return InlineKeyboardAttachment(
        type="inline_keyboard",
        payload={
            "buttons": [
                [
                    {"type": "message", "text": "Группы", "payload": "sched:groups"},
                    {
                        "type": "message",
                        "text": "Преподаватели",
                        "payload": "sched:teachers",
                    },
                ],
                [
                    {"type": "message", "text": "⬅️ В меню"},
                ],
            ]
        },
    )


async def open_schedule_menu(event: MessageCreated):
    await event.message.answer(
        "Раздел «Расписание». Что показать?",
        attachments=[schedule_root_kb()],
    )
