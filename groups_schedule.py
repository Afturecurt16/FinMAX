import asyncio
import logging
from datetime import datetime, timedelta, date
from typing import Dict, List

from pydantic import BaseModel
from maxapi.types import MessageCreated

from fa_api import FaAPI

from homework import _reply_homework_for_date as _hw_reply_dz

log = logging.getLogger("groups_schedule")

GENERIC_TEACHER_WORDS = {
    "преподаватель", "преподователь",
    "teacher", "lecturer",
    "доцент", "ассистент", "старший преподаватель", "профессор",
}

def _pick_first(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def _normalize_label(s: str) -> str:
    s = (s or "").strip()
    if "/" in s:
        parts = [p.strip() for p in s.split("/") if p.strip()]
        if parts:
            parts.sort(key=len, reverse=True)
            s = parts[0]
    return s

def _teacher_fio_any(t: dict) -> str:
    fio = _pick_first(
        t.get("full_name"),
        t.get("fio_full"),
        t.get("display_name"),
        t.get("lecturer_title"),
        t.get("fio"),
        t.get("fullname"),
    )
    fio = _normalize_label(fio)
    if fio and fio.lower() not in GENERIC_TEACHER_WORDS:
        return fio

    last = _pick_first(
        t.get("surname"), t.get("last_name"),
        t.get("lastname"), t.get("lastName"), t.get("family")
    )
    first = _pick_first(
        t.get("first_name"), t.get("firstname"),
        t.get("firstName"), t.get("given"), t.get("name_first")
    )
    middle = _pick_first(
        t.get("middle_name"), t.get("middlename"),
        t.get("middleName"), t.get("patronymic"), t.get("secondName")
    )
    parts = [p for p in (last, first, middle) if p]
    if parts:
        return " ".join(parts)

    for key in ("lecturer", "teacher", "name", "title"):
        v = _normalize_label(_pick_first(t.get(key)))
        if v and v.lower() not in GENERIC_TEACHER_WORDS:
            return v

    return ""

def _teacher_names_from_record(rec: dict) -> list[str]:
    names: list[str] = []
    seen = set()

    def add_name(val: str):
        val = _normalize_label(val)
        if not val:
            return
        low = val.lower()
        if low in GENERIC_TEACHER_WORDS:
            return
        if low in seen:
            return
        seen.add(low)
        names.append(val)

    multi = False
    for key in ("listOfLecturers", "teachers", "lecturers", "employees"):
        arr = rec.get(key)
        if isinstance(arr, list) and arr:
            multi = True
            for t in arr:
                if isinstance(t, dict):
                    fio = _teacher_fio_any(t)
                    if fio:
                        add_name(fio)

    if not multi:
        fio = _teacher_fio_any(rec)
        if fio:
            add_name(fio)

    return names

RING_STARTS = ["08:30","10:15","12:00","13:50","15:35","17:20","19:05"]  

def _hhmm_to_min(s: str):
    try:
        h, m = s.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None

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

def reset_groups_flow_for(event: MessageCreated):
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
                    {"type": "message", "text": "Сменить группу"},
                ],
                [
                    {
                        "type": "message",
                        "text": "⬅️ В меню",
                        "payload": "menu:home",
                    }
                ],
            ]
        }
    )

fa = FaAPI()

async def _search_group(query: str):
    return await asyncio.to_thread(fa.search_group, query)

async def _timetable_group(group_id: str, start: datetime, end: datetime):
    s = start.strftime("%Y.%m.%d")
    e = end.strftime("%Y.%m.%d")
    return await asyncio.to_thread(fa.timetable_group, group_id, s, e)

_RU_WEEKDAY_ACC = {
    0: "понедельник",
    1: "вторник",
    2: "среду",
    3: "четверг",
    4: "пятницу",
    5: "субботу",
    6: "воскресенье",
}

def _fmt_day(records: List[dict], group_name: str) -> str:
    if not records:
        return f"Расписание для {group_name} на этот день пустое."

    def _v(x):
        return (x or "").strip()

    def _begin_min(rec):
        return _hhmm_to_min(_v(rec.get("beginLesson"))) or 10**9

    recs_sorted = sorted(records, key=_begin_min)

    date_str = recs_sorted[0].get("date") or ""
    try:
        d = datetime.fromisoformat(date_str).date()
        wd = _RU_WEEKDAY_ACC.get(d.weekday(), "")
        header = f"Расписание {group_name} на {wd} ({date_str}):"
    except Exception:
        header = f"Расписание {group_name} на {date_str}:"

    out_lines: list[str] = [header, ""]

    prev_begin = None

    for idx, rec in enumerate(recs_sorted):
        begin = _v(rec.get("beginLesson"))
        end   = _v(rec.get("endLesson"))
        subj  = _v(rec.get("discipline"))
        aud   = _v(rec.get("auditorium"))
        kind  = _v(rec.get("kindOfWork"))

        if prev_begin is not None and begin != prev_begin:
            out_lines.append("")

        pno = _pair_no_by_begin(begin)
        if pno is None:
            pno = idx + 1
        prefix = _num_emoji(pno)

        if begin and end:
            time_part = f"{begin}-{end}"
        else:
            time_part = begin or end or ""

        teachers = _teacher_names_from_record(rec)
        teachers_str = " / ".join(teachers) if teachers else ""

        if teachers_str and aud:
            right = f"{teachers_str} — {aud}"
        elif teachers_str:
            right = teachers_str
        elif aud:
            right = aud
        else:
            right = ""

        line1 = f"{prefix} {time_part}."
        if right:
            line1 += f" {right}."

        kind_hint = ""
        kl = kind.lower()
        if "семинар" in kl:
            kind_hint = "семинар"
        elif "лекц" in kl:
            kind_hint = "лекция"

        if subj:
            if kind_hint:
                line2 = f"    {subj} ({kind_hint})."
            elif kind:
                line2 = f"    {subj} ({kind})."
            else:
                line2 = f"    {subj}."
        else:
            line2 = ""

        out_lines.append(line1)
        if line2:
            out_lines.append(line2)

        if idx + 1 < len(recs_sorted):
            next_begin = _v(recs_sorted[idx + 1].get("beginLesson"))
            if end and next_begin:
                e_min = _hhmm_to_min(end)
                nb_min = _hhmm_to_min(next_begin)
                if e_min is not None and nb_min is not None:
                    gap = nb_min - e_min
                    if gap > 0:
                        out_lines.append(f"    Перерыв {gap} минут.")

        prev_begin = begin

    return "\n".join(out_lines)

    return header + "\n\n".join(blocks)
def _week_bounds(dt: datetime):
    monday = dt - timedelta(days=dt.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday

async def _show_schedule_and_homework_for_day(
    event: MessageCreated,
    group_name: str,
    day_iso: str,
    records_for_day: List[dict],
):
    txt = _fmt_day(records_for_day, group_name=group_name)
    await event.message.answer(txt)

    try:
        import sqlite3
        from datetime import datetime as _dt
        from homework import DB_PATH as _HW_DB_PATH

        d = _dt.strptime(day_iso, "%Y-%m-%d").date()
        d_human = d.strftime("%d.%m.%Y")

        has_hw = False
        with sqlite3.connect(_HW_DB_PATH) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND (name=? OR LOWER(name)=LOWER(?)) LIMIT 1",
                (group_name, group_name),
            )
            row = cur.fetchone()
            if row:
                table = row[0]
                cur2 = conn.execute(
                    f'SELECT 1 FROM "{table}" WHERE deadline IN (?, ?) LIMIT 1',
                    (d_human, day_iso),
                )
                has_hw = cur2.fetchone() is not None

        if has_hw:
            await _hw_reply_dz(event, group_name, d)

    except Exception as e:
        log.debug("HW check failed for %s %s: %s", group_name, day_iso, e)

        
async def open_groups_menu(event: MessageCreated):
    st = _st(event)
    st.clear()
    st["mode"] = "ASK_GROUP"

    await event.message.answer(
        "Введите название группы (например: БИ25-6):"
    )

async def try_handle_group_message(event: MessageCreated) -> bool:
    body = getattr(event.message, "body", None) or event.message
    text = (getattr(body, "text", None) or "").strip()

    if not text:
        return False

    st = _st(event)
    mode = st.get("mode")

    if mode is None:
        return False

    if mode == "ASK_GROUP":
        query = text
        await event.message.answer("Ищу группу…")
        try:
            groups = await _search_group(query)
        except Exception as e:
            await event.message.answer(f"Ошибка при запросе группы: {e}")
            return True

        if not groups:
            await event.message.answer(
                "Мы не нашли такую группу. Попробуйте ввести название ещё раз:"
            )
            return True

        g = groups[0]
        gid = str(g.get("id"))
        name = (
            g.get("group")
            or g.get("name")
            or g.get("title")
            or g.get("label")
            or g.get("full_name")
            or g.get("fullname")
            or query
        )

        st["mode"] = "IN_GROUP"
        st["group_id"] = gid
        st["group_name"] = name

        await event.message.answer(
            text=f"Группа: {name}\nВыберите период:",
            attachments=[_range_kb()],
        )
        return True

    if mode == "IN_GROUP":
        gid = st.get("group_id")
        name = st.get("group_name") or "Группа"
        if not gid:
            st["mode"] = "ASK_GROUP"
            await event.message.answer(
                "Группа не выбрана. Введите название группы:"
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
        elif text == "Сменить группу":
            st.clear()
            st["mode"] = "ASK_GROUP"
            await event.message.answer(
                "Введите название группы (например: БИ25-6):"
            )
            return True
        else:
            return False

        try:
            raw = await _timetable_group(gid, start, end)
        except Exception as e:
            await event.message.answer(f"Ошибка при запросе расписания: {e}")
            return True

        if not raw:
            if start == end:
                ds = start.strftime("%Y-%m-%d")
                await _show_schedule_and_homework_for_day(event, name, ds, [])
            else:
                monday = start - timedelta(days=start.weekday())
                for i in range(6):  
                    day_dt = monday + timedelta(days=i)
                    ds = day_dt.strftime("%Y-%m-%d")
                    await _show_schedule_and_homework_for_day(event, name, ds, [])
        else:
            if start != end:
                monday = start - timedelta(days=start.weekday())
                for i in range(6):
                    day_dt = monday + timedelta(days=i)
                    ds = day_dt.strftime("%Y-%m-%d")
                    items = [r for r in raw if (r.get("date") or "") == ds]
                    await _show_schedule_and_homework_for_day(event, name, ds, items)
            else:
                day_iso = start.strftime("%Y-%m-%d")
                items = [r for r in raw if (r.get("date") or "") == day_iso] or raw
                await _show_schedule_and_homework_for_day(event, name, day_iso, items)

        await event.message.answer(
            text="Выберите дальнейшее действие:",
            attachments=[_range_kb()],
        )
        return True

    if mode == "ASK_DATE":
        gid = st.get("group_id")
        name = st.get("group_name") or "Группа"
        if not gid:
            st["mode"] = "ASK_GROUP"
            await event.message.answer(
                "Группа не выбрана. Введите название группы:"
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
            raw = await _timetable_group(gid, start, end)
        except Exception as e:
            await event.message.answer(f"Ошибка при запросе расписания: {e}")
            return True

        day_iso = start.strftime("%Y-%m-%d")
        items = [r for r in (raw or []) if (r.get("date") or "") == day_iso] or (raw or [])
        await _show_schedule_and_homework_for_day(event, name, day_iso, items)

        st["mode"] = "IN_GROUP"
        await event.message.answer(
            text="Выберите дальнейшее действие:",
            attachments=[_range_kb()],
        )
        return True
    return False