import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict
import re
from pydantic import BaseModel
from maxapi.types import MessageCreated

from fa_api import FaAPI

log = logging.getLogger("teachers_schedule")

RING_STARTS = ["08:30","10:15","12:00","13:50","15:35","17:20","19:05"] 

def _hhmm_to_min(s: str):
    try:
        h, m = s.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

def _extract_email_from_value(v: object) -> str:
    if isinstance(v, str):
        m = EMAIL_RE.search(v)
        if m:
            return m.group(0)
    return ""

def _find_teacher_email_in_record(rec: dict) -> str:
    for key in ("lecturerEmail", "email", "teacherEmail", "lecturer_email"):
        e = _extract_email_from_value(rec.get(key))
        if e:
            return e

    for key in ("listOfLecturers", "teachers", "lecturers"):
        arr = rec.get(key)
        if isinstance(arr, list):
            for t in arr:
                if isinstance(t, dict):
                    for k in ("lecturerEmail", "email", "mail", "e_mail"):
                        e = _extract_email_from_value(t.get(k))
                        if e:
                            return e

    for key in ("comment", "note", "notes", "desc", "description", "info", "title", "subject", "details"):
        e = _extract_email_from_value(rec.get(key))
        if e:
            return e

    return ""

def _find_teacher_email(records: list[dict]) -> str:
    for rec in records:
        e = _find_teacher_email_in_record(rec or {})
        if e:
            return e
    return ""

def _num_emoji(n: int) -> str:
    m = {0:"0️⃣",1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",6:"6️⃣",7:"7️⃣",8:"8️⃣",9:"9️⃣",10:"🔟"}
    if n in m:
        return m[n]
    out = []
    for ch in str(n):
        out.append(m.get(int(ch), ch))
    return "".join(out)

def _pair_no_by_begin(begin_hhmm: str, tolerance_min: int = 25):
    bmin = _hhmm_to_min(begin_hhmm or "")
    if bmin is None:
        return None
    best_idx, best_diff = None, 10**9
    for i, hhmm in enumerate(RING_STARTS):
        rmin = _hhmm_to_min(hhmm)
        if rmin is None:
            continue
        diff = abs(bmin - rmin)
        if diff < best_diff:
            best_diff, best_idx = diff, i
    if best_idx is not None and best_diff <= tolerance_min:
        return best_idx + 1  
    return None

STATE: Dict[str, dict] = {}

def _conv_key(event: MessageCreated) -> str:
    parts = []
    for name in ("chat_id", "user_id", "peer_id", "dialog_id", "conversation_id"):
        v = getattr(event, name, None)
        if v is not None:
            parts.append(f"{name}={v}")
    return "|".join(parts) if parts else "global"

def _st(event: MessageCreated) -> dict:
    key = _conv_key(event)
    return STATE.setdefault(key, {})

def reset_teachers_flow_for(event: MessageCreated):
    key = _conv_key(event)
    if key in STATE:
        del STATE[key]

class InlineKeyboardAttachment(BaseModel):
    type: str = "inline_keyboard"
    payload: dict

def _range_kb() -> InlineKeyboardAttachment:
    return InlineKeyboardAttachment(
        payload={
            "buttons": [
                [
                    {"type": "message", "text": "Сегодня"},
                    {"type": "message", "text": "Завтра"},
                ],
                [
                    {"type": "message", "text": "Эта неделя"},
                    {"type": "message", "text": "Следующая неделя"},
                ],
                [
                    {"type": "message", "text": "Выбрать дату"},
                    {"type": "message", "text": "Сменить преподавателя"},
                ],
                [
                    {
                        "type": "message",
                        "text": "⬅️ В расписание",
                        "payload": "sched:root",
                    }
                ],
            ]
        }
    )

fa = FaAPI()

async def _search_teacher(query: str):
    return await asyncio.to_thread(fa.search_teacher, query)

async def _timetable_teacher(teacher_id: str, start: datetime, end: datetime):
    s = start.strftime("%Y.%m.%d")
    e = end.strftime("%Y.%m.%d")
    return await asyncio.to_thread(fa.timetable_teacher, teacher_id, s, e)

def _fmt_day(records, teacher_name: str) -> str:
    if not records:
        return f"Расписание для {teacher_name} на этот день пустое."

    def _v(x):
        return (x or "").strip()

    def _begin_min(rec):
        return _hhmm_to_min(_v(rec.get("beginLesson"))) or 10**9

    records_sorted = sorted(records, key=_begin_min)

    date_str = records_sorted[0].get("date") or ""

    lines = [f"Расписание для {teacher_name} на {date_str}:", ""]

    last_idx = len(records_sorted) - 1

    for idx, rec in enumerate(records_sorted):
        begin = _v(rec.get("beginLesson"))
        end   = _v(rec.get("endLesson"))
        group = _v(rec.get("group"))
        subj  = _v(rec.get("discipline"))
        aud   = _v(rec.get("auditorium"))

        time_part = f"{begin}-{end}" if (begin or end) else ""

        pno = _pair_no_by_begin(begin)
        if pno is None:
            pno = idx + 1
        prefix = _num_emoji(pno)

        right = ", ".join([p for p in (group, aud) if p])

        line = f"{prefix} "
        if time_part:
            line += f"{time_part} "
        if subj:
            line += subj
        if right:
            line += f" ({right})"

        lines.append(line)

        if idx != last_idx:
            lines.append("")

    email = _find_teacher_email(records_sorted)
    if email:
        lines += ["", f"Email: {email}"]

    return "\n".join(lines)

def _week_bounds(dt: datetime):
    monday = dt - timedelta(days=dt.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


async def open_teachers_menu(event: MessageCreated):
    st = _st(event)
    st.clear()
    st["mode"] = "ASK_SURNAME"

    await event.message.answer(
        "Введите фамилию преподавателя (например: Неизвестный):"
    )

async def try_handle_teacher_message(event: MessageCreated) -> bool:
    body = getattr(event.message, "body", None) or event.message
    text = (getattr(body, "text", None) or "").strip()

    if not text:
        return False

    st = _st(event)
    mode = st.get("mode")

    if mode is None:
        return False

    if mode == "ASK_SURNAME":
        query = text
        await event.message.answer("Ищу преподавателя…")
        try:
            teachers = await _search_teacher(query)
        except Exception as e:
            await event.message.answer(f"Ошибка при запросе преподавателя: {e}")
            return True

        if not teachers:
            await event.message.answer(
                "Мы не нашли такого преподавателя. Попробуйте ввести фамилию ещё раз:"
            )
            return True

        t = teachers[0]
        tid = str(t.get("id"))
        name = (
            t.get("lecturer_title")
            or t.get("name")
            or t.get("full_name")
            or "Преподаватель"
        )

        st["mode"] = "IN_TEACHER"
        st["teacher_id"] = tid
        st["teacher_name"] = name

        await event.message.answer(
            text=f"Выберите период:",
            attachments=[_range_kb()],
        )
        return True

    if mode == "IN_TEACHER":
        tid = st.get("teacher_id")
        name = st.get("teacher_name") or "Преподаватель"
        if not tid:
            st["mode"] = "ASK_SURNAME"
            await event.message.answer(
                "Не выбран преподаватель. Введите фамилию преподавателя:"
            )
            return True

        today = datetime.now().date()

        if text == "Сегодня":
            start = end = datetime.combine(today, datetime.min.time())
        elif text == "Завтра":
            d = today + timedelta(days=1)
            start = end = datetime.combine(d, datetime.min.time())
        elif text == "Эта неделя":
            start, end = _week_bounds(datetime.combine(today, datetime.min.time()))
        elif text == "Следующая неделя":
            cur_mon, cur_sun = _week_bounds(datetime.combine(today, datetime.min.time()))
            start = cur_mon + timedelta(days=7)
            end = cur_sun + timedelta(days=7)
        elif text == "Выбрать дату":
            st["mode"] = "ASK_DATE"
            await event.message.answer(
                "Введите дату в формате YYYY-MM-DD или DD.MM.YYYY:"
            )
            return True
        elif text == "Сменить преподавателя":
            st.clear()
            st["mode"] = "ASK_SURNAME"
            await event.message.answer(
                "Введите фамилию преподавателя (например: Неизвестный):"
            )
            return True
        else:
            return False

        try:
            raw = await _timetable_teacher(tid, start, end)
        except Exception as e:
            await event.message.answer(f"Ошибка при запросе расписания: {e}")
            return True

        if not raw:
            if start == end:
                ds = start.strftime("%Y-%m-%d")
                await event.message.answer(f"Занятий не найдено на {ds}.")
            else:
                ds = f"{start.strftime('%Y-%m-%d')} — {end.strftime('%Y-%m-%d')}"
                await event.message.answer(f"Занятий не найдено в диапазоне {ds}.")
        else:
            if start != end:
                by_date = {}
                for r in raw:
                    d = r.get("date")
                    if not d:
                        continue
                    by_date.setdefault(d, []).append(r)
                for d, items in sorted(by_date.items()):
                    txt = _fmt_day(items, teacher_name=name)
                    await event.message.answer(txt)
            else:
                day_iso = start.strftime("%Y-%m-%d")
                items = [r for r in raw if r.get("date") == day_iso] or raw
                txt = _fmt_day(items, teacher_name=name)
                await event.message.answer(txt)

        await event.message.answer(
            text="Выберите период:",
            attachments=[_range_kb()],
        )
        return True

    if mode == "ASK_DATE":
        tid = st.get("teacher_id")
        name = st.get("teacher_name") or "Преподаватель"
        if not tid:
            st["mode"] = "ASK_SURNAME"
            await event.message.answer(
                "Не выбран преподаватель. Введите фамилию преподавателя:"
            )
            return True

        s = text
        dt = None
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue

        if dt is None:
            await event.message.answer(
                "Не понял дату. Пример: 2025-11-07 или 07.11.2025. Попробуйте ещё раз:"
            )
            return True

        start = end = dt
        try:
            raw = await _timetable_teacher(tid, start, end)
        except Exception as e:
            await event.message.answer(f"Ошибка при запросе расписания: {e}")
            return True

        if not raw:
            ds = start.strftime("%Y-%m-%d")
            await event.message.answer(f"Занятий не найдено на {ds}.")
        else:
            day_iso = start.strftime("%Y-%m-%d")
            items = [r for r in raw if r.get("date") == day_iso] or raw
            txt = _fmt_day(items, teacher_name=name)
            await event.message.answer(txt)

        st["mode"] = "IN_TEACHER"
        await event.message.answer(
            text="Выберите период:",
            attachments=[_range_kb()],
        )
        return True

    return False
