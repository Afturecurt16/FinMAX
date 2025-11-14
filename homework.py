import asyncio
import logging
import time
import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Dict, Optional, List

from maxapi.types import MessageCreated, MessageCallback
from maxapi import F

from maxapi.types import ButtonsPayload, CallbackButton, MessageButton

log = logging.getLogger("homework")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "homework.db"
DATA_DIR = BASE_DIR / "homework_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BOOT_TS = time.time()
OLD_EVENT_SLOP = 1.5


def _to_epoch_seconds(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 10**12 else float(v)
    if isinstance(v, str):
        try:
            s = v.rstrip("Z")
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return None
    if isinstance(v, datetime):
        return v.timestamp()
    return None


def _extract_event_ts(event) -> Optional[float]:
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


def _is_old_event(event) -> bool:
    ts = _extract_event_ts(event)
    return ts is not None and ts < (BOOT_TS - OLD_EVENT_SLOP)


def _is_from_bot(message) -> bool:
    try:
        author = getattr(message, "author", None) or {}
        is_bot = (
            getattr(author, "is_bot", None)
            or getattr(author, "bot", None)
            or getattr(author, "isBot", None)
        )
        bot_id = getattr(getattr(message, "bot", None), "id", None)
        return bool(is_bot) or (author and bot_id and getattr(author, "id", None) == bot_id)
    except Exception:
        return False


def _dialog_key(event) -> str:
    for name in ("chat_id", "peer_id", "dialog_id", "conversation_id"):
        v = getattr(event, name, None)
        if v is not None:
            return f"chat:{v}"

    msg = getattr(event, "message", None)
    if msg is not None:
        chat = getattr(msg, "chat", None)
        if chat is not None:
            for name in ("id", "chat_id"):
                v = getattr(chat, name, None)
                if v is not None:
                    return f"chat:{v}"
        for name in ("chat_id", "peer_id", "conversation_id"):
            v = getattr(msg, name, None)
            if v is not None:
                return f"chat:{v}"
    return "chat:global"


STATE: Dict[str, dict] = {}


def _st(key: str) -> dict:
    return STATE.setdefault(key, {})


def _reset(key: str):
    STATE[key] = {}


def homework_is_waiting_group(event) -> bool:
    key = _dialog_key(event)
    st = _st(key)
    return st.get("mode") == "ASK_GROUP"


def _msg_text(event) -> str:
    msg = getattr(event, "message", None)
    if msg is None:
        return ""
    body = getattr(msg, "body", None)

    if isinstance(body, dict):
        t = (body.get("text") or "").strip()
        if not t and isinstance(body.get("payload"), dict):
            pt = body["payload"].get("text")
            if isinstance(pt, str):
                t = pt.strip()
        return t

    if body is not None:
        t = (getattr(body, "text", None) or "").strip()
        if not t:
            payload = getattr(body, "payload", None)
            if isinstance(payload, dict):
                pt = payload.get("text")
                if isinstance(pt, str):
                    t = pt.strip()
        return t

    return (getattr(msg, "text", None) or "").strip()

def homework_is_adding(event) -> bool:
    key = _dialog_key(event)
    st = _st(key)
    mode = st.get("mode") or ""
    return mode.startswith("ADD_")

async def handle_add_message(event: MessageCreated):
    if _is_old_event(event) or _is_from_bot(event.message):
        return
    text = _msg_text(event).strip()
    try:
        await _try_handle_add_flow(event, text)
    except Exception as e:
        log.exception("handle_add_message failed: %s", e)
        await event.message.answer("Не удалось обработать ввод для добавления ДЗ. Попробуйте ещё раз.")

def _range_kb() -> dict:
    buttons = [
        [CallbackButton(text="Сегодня",          payload="hw:today"),
         CallbackButton(text="Завтра",           payload="hw:tomorrow")],
        [CallbackButton(text="Эта неделя",       payload="hw:thisweek"),
         CallbackButton(text="След неделя",      payload="hw:nextweek")],  
        [CallbackButton(text="Выбрать дату",     payload="hw:pickdate"),
         CallbackButton(text="Сменить группу",   payload="hw:change_group")],
         [MessageButton(text="⬅️ В меню", payload="menu:home")]
    ]
    return ButtonsPayload(buttons=buttons).pack()

def homework_root_kb() -> dict:
    buttons = [
        [CallbackButton(text="Посмотреть", payload="hw:watch"),
         CallbackButton(text="Добавить",   payload="hw:add")],
        [MessageButton(text="⬅️ В меню", payload="menu:home")],
    ]
    return ButtonsPayload(buttons=buttons).pack()


def _after_add_kb() -> dict:
    buttons = [
        [CallbackButton(text="Добавить", payload="hw:add_more")],
        [MessageButton(text="⬅️ В меню", payload="menu:home")],
    ]
    return ButtonsPayload(buttons=buttons).pack()


def _homework_root_kb() -> dict:
    return homework_root_kb()

def _no_files_kb() -> dict:
    buttons = [[CallbackButton(text="Нет файлов (сохранить)", payload="hw:nofile")]]
    return ButtonsPayload(buttons=buttons).pack()


def _human_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")

def _iso_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")

def _parse_user_date(s: str) -> Optional[date]:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _ensure_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None

def _resolve_table_name(conn: sqlite3.Connection, group: str) -> Optional[str]:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (group,)
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND LOWER(name)=LOWER(?)",
        (group,)
    )
    row = cur.fetchone()
    return row[0] if row else None

def _create_group_table_if_needed(conn: sqlite3.Connection, table: str):
    if not _table_exists(conn, table):
        conn.execute(
            f"""
            CREATE TABLE "{table}" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                deadline TEXT NOT NULL,
                task TEXT NOT NULL,
                files TEXT DEFAULT '[]',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

def _select_for_dates(conn: sqlite3.Connection, table: str, date_strs: List[str]) -> List[dict]:
    q_marks = ",".join("?" for _ in date_strs)
    sql = f'SELECT subject, deadline, task, files FROM "{table}" WHERE deadline IN ({q_marks}) ORDER BY id ASC'
    cur = conn.execute(sql, tuple(date_strs))
    rows = cur.fetchall()
    out = []
    for subject, deadline, task, files in rows:
        try:
            files_list = json.loads(files) if isinstance(files, str) else (files or [])
        except Exception:
            files_list = []
        out.append({"subject": subject, "deadline": deadline, "task": task, "files": files_list})
    return out

def _insert_homework(conn: sqlite3.Connection, table: str, subject: str, deadline: date, task: str, files: List[str]):
    _create_group_table_if_needed(conn, table)
    conn.execute(
        f'INSERT INTO "{table}"(subject, deadline, task, files) VALUES (?,?,?,?)',
        (subject, _human_date(deadline), task, json.dumps(files, ensure_ascii=False)),
    )
    conn.commit()

async def open_homework_menu(event: MessageCreated):
    if _is_old_event(event) or _is_from_bot(event.message):
        return
    await event.message.answer(
        text="📅 Вы хотите посмотреть или добавить домашнюю работу?",
        attachments=[homework_root_kb()],
    )


async def open_watch_menu(event: MessageCreated | MessageCallback):
    if _is_old_event(event):
        return
    if isinstance(event, MessageCreated) and _is_from_bot(event.message):
        return

    key = _dialog_key(event)
    st = _st(key)

    text = _msg_text(event).strip()
    if st.get("mode") != "ASK_GROUP":
        _reset(key)
        st = _st(key)
        st["mode"] = "ASK_GROUP"
        await event.message.answer("Введите номер группы (например: БИ25-6):")
        return

    if text:
        group = text
        st["mode"] = "IN_GROUP"
        st["group_id"] = group
        st["group_name"] = group

        await event.message.answer(f"Вы ввели номер группы: {group}")
        await event.message.answer(
            text="Выберите период:",
            attachments=[_range_kb()],
        )
        return

    await event.message.answer("Введите номер группы (например: БИ25-6):")

async def _reply_homework_for_date(event: MessageCreated, group: str, day: date):
    _ensure_db()
    d_human = _human_date(day)
    d_iso = _iso_date(day)

    with sqlite3.connect(DB_PATH) as conn:
        table = _resolve_table_name(conn, group)
        if not table:
            await event.message.answer("Для этой группы ДЗ пока не добавляли.")
            return

        items = _select_for_dates(conn, table, [d_human, d_iso])
        if not items:
            await event.message.answer(f"На {d_human} ничего не найдено.")
            return

        for it in items:
            subject = (it.get("subject") or "Предмет").strip()
            deadline_str = (it.get("deadline") or d_human).strip()
            task = (it.get("task") or "").strip()
            files = it.get("files") or []

            lines = [
                f"Домашняя работа на {d_human}",
                f"Предмет: {subject}",
                f"Дедлайн: {deadline_str}",
            ]
            if task:
                lines.append(f"Задание: {task}")
            if files:
                for fn in files:
                    lines.append(f"Файл: {fn}")

            await event.message.answer("\n".join(lines))

            for fn in files:
                p = DATA_DIR / group / fn
                if p.exists():
                    await event.message.answer(f"📎 Файл: {p}")


async def _reply_homework_for_week(event: MessageCreated, group: str, start: date):
    monday = start - timedelta(days=start.weekday())
    for i in range(6):  
        day = monday + timedelta(days=i)
        await _reply_homework_for_date(event, group, day)


async def _start_add_flow(event: MessageCreated | MessageCallback):
    if _is_old_event(event):
        return
    if isinstance(event, MessageCreated) and _is_from_bot(event.message):
        return

    key = _dialog_key(event)
    _reset(key)
    st = _st(key)

    st["mode"] = "ADD_ASK_GROUP"
    st["add"] = {}

    await event.message.answer("Введите номер группы, для которой добавляете ДЗ (например: БИ25-6):")


async def _try_handle_add_flow(event: MessageCreated, text: str) -> bool:
    key = _dialog_key(event)
    st = _st(key)
    mode = st.get("mode")
    add = st.setdefault("add", {})

    if mode == "ADD_ASK_GROUP":
        group_name = text.strip()
        add["group"] = group_name
        st["mode"] = "ADD_ASK_SUBJECT"
        await event.message.answer(f"Группа: {group_name}\nВведите название предмета:")
        return True

    if mode == "ADD_ASK_SUBJECT":
        add["subject"] = text.strip()
        st["mode"] = "ADD_ASK_DEADLINE"
        await event.message.answer("Введите дедлайн (YYYY-MM-DD или DD.MM.YYYY):")
        return True

    if mode == "ADD_ASK_DEADLINE":
        d = _parse_user_date(text)
        if not d:
            await event.message.answer("Не понял дату. Пример: 2025-12-12 или 12.12.2025. Попробуйте ещё раз:")
            return True
        add["deadline"] = d
        st["mode"] = "ADD_ASK_TASK"
        await event.message.answer("Опишите задание (текст одним сообщением):")
        return True

    if mode == "ADD_ASK_TASK":
        add["task"] = text.strip()
        add.setdefault("files", [])
        st["mode"] = "ADD_WAIT_FILES"
        await event.message.answer(
            "Прикрепите сюда файлы при их наличии.\nЕсли файлов нет — нажмите кнопку ниже:\nВажно! В MAX пока нет механики добавления файлов. Как только она появится, файлы будут загружаться корректно.",
            attachments=[_no_files_kb()],
        )
        return True

    if mode == "ADD_WAIT_FILES":
        msg = getattr(event, "message", None)
        atts = getattr(msg, "attachments", None)
        if atts and isinstance(atts, list):
            saved = 0
            grp = add.get("group")
            d = add.get("deadline")
            for a in atts:
                name = None
                if isinstance(a, dict):
                    name = a.get("file_name") or a.get("name") or a.get("title")
                else:
                    name = getattr(a, "file_name", None) or getattr(a, "name", None) or getattr(a, "title", None)
                if not name:
                    continue
                stem, dot, ext = name.partition(".")
                new_name = f"{stem}_{_human_date(d)}{('.' + ext) if dot else ''}"
                (DATA_DIR / grp).mkdir(parents=True, exist_ok=True)
                add["files"].append(new_name)
                saved += 1
            if saved:
                await event.message.answer(f"Принято файлов: {saved}. Нажмите «Нет файлов (сохранить)», чтобы записать ДЗ.")
                return True
        await event.message.answer("Прикрепите файлы (если есть) или нажмите «Нет файлов (сохранить)».")
        return True

    if mode == "ADD_CONFIRM":
        return True

    return False


async def _finalize_add(event: MessageCreated | MessageCallback):
    key = _dialog_key(event)
    st = _st(key)
    add = st.get("add") or {}
    grp = add.get("group")
    subj = (add.get("subject") or "").strip()
    dl: date = add.get("deadline")
    task = (add.get("task") or "").strip()
    files: List[str] = add.get("files") or []

    if not (grp and subj and dl and task):
        await event.message.answer("Похоже, не вся информация собрана. Попробуйте ещё раз / начните заново.")
        return

    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        _insert_homework(conn, grp, subj, dl, task, files)

    st["mode"] = "IN_GROUP"
    st["group_id"] = grp
    st["group_name"] = grp
    st.pop("add", None)

    lines = [
        "✅ Домашняя работа добавлена.",
        f"Группа: {grp}",
        f"Предмет: {subj}",
        f"Дедлайн: {_human_date(dl)}",
        f"Задание: {task}",
    ]
    if files:
        lines.append("Файлы:")
        lines.extend(files)

    await event.message.answer("\n".join(lines))
    await event.message.answer(
        "Выберите следующее действие:",
        attachments=[_after_add_kb()],
    )


def register_homework_handlers(dp, bot):
    @dp.message_created(F.message.body.text == "Посмотреть")
    async def _go_watch_by_text(event: MessageCreated):
        if _is_old_event(event) or _is_from_bot(event.message):
            return
        try:
            await open_watch_menu(event)
        except Exception as e:
            log.exception("open_watch_menu failed: %s", e)
            await event.message.answer("Раздел «Посмотреть» временно недоступен.")

    @dp.message_created(F.message.body.payload == "hw:watch")
    async def _go_watch_by_payload_body(event: MessageCreated):
        if _is_old_event(event) or _is_from_bot(event.message):
            return
        try:
            await open_watch_menu(event)
        except Exception as e:
            log.exception("open_watch_menu (payload body) failed: %s", e)
            await event.message.answer("Раздел «Посмотреть» временно недоступен.")

    @dp.message_created(F.message.payload == "hw:watch")
    async def _go_watch_by_payload_msg(event: MessageCreated):
        if _is_old_event(event) or _is_from_bot(event.message):
            return
        try:
            await open_watch_menu(event)
        except Exception as e:
            log.exception("open_watch_menu (payload msg) failed: %s", e)
            await event.message.answer("Раздел «Посмотреть» временно недоступен.")

    @dp.message_callback(F.callback.payload == "hw:watch")
    async def _go_watch_by_callback(event: MessageCallback):
        if _is_old_event(event):
            return
        try:
            await open_watch_menu(event)
        except Exception as e:
            log.exception("open_watch_menu (callback) failed: %s", e)
            await event.message.answer("Раздел «Посмотреть» временно недоступен.")



    @dp.message_created(F.message.body.text == "Добавить")
    async def _go_add_by_text(event: MessageCreated):
        if _is_old_event(event) or _is_from_bot(event.message):
            return
        try:
            await _start_add_flow(event)
        except Exception as e:
            log.exception("start_add_flow failed: %s", e)
            await event.message.answer("Раздел «Добавить» временно недоступен.")

    @dp.message_created(F.message.body.payload == "hw:add")
    async def _go_add_by_payload_body(event: MessageCreated):
        if _is_old_event(event) or _is_from_bot(event.message):
            return
        try:
            await _start_add_flow(event)
        except Exception as e:
            log.exception("start_add_flow (body) failed: %s", e)
            await event.message.answer("Раздел «Добавить» временно недоступен.")

    @dp.message_created(F.message.payload == "hw:add")
    async def _go_add_by_payload_msg(event: MessageCreated):
        if _is_old_event(event) or _is_from_bot(event.message):
            return
        try:
            await _start_add_flow(event)
        except Exception as e:
            log.exception("start_add_flow (msg) failed: %s", e)
            await event.message.answer("Раздел «Добавить» временно недоступен.")

    @dp.message_callback(F.callback.payload == "hw:add")
    async def _go_add_by_callback(event: MessageCallback):
        if _is_old_event(event):
            return
        try:
            await _start_add_flow(event)
        except Exception as e:
            log.exception("start_add_flow (callback) failed: %s", e)
            await event.message.answer("Раздел «Добавить» временно недоступен.")


    @dp.message_callback(F.callback.payload == "hw:today")
    async def _hw_today(event: MessageCallback):
        key = _dialog_key(event); st = _st(key)
        group = st.get("group_name") or st.get("group_id")
        if not group:
            st["mode"] = "ASK_GROUP"
            await event.message.answer("Группа не выбрана. Введите номер группы:")
            return
        today = datetime.now().date()
        await _reply_homework_for_date(event, group, today)
        await event.message.answer("Выберите период:", attachments=[_range_kb()])

    @dp.message_callback(F.callback.payload == "hw:tomorrow")
    async def _hw_tomorrow(event: MessageCallback):
        key = _dialog_key(event); st = _st(key)
        group = st.get("group_name") or st.get("group_id")
        if not group:
            st["mode"] = "ASK_GROUP"
            await event.message.answer("Группа не выбрана. Введите номер группы:")
            return
        tomorrow = datetime.now().date() + timedelta(days=1)
        await _reply_homework_for_date(event, group, tomorrow)
        await event.message.answer("Выберите период:", attachments=[_range_kb()])

    @dp.message_callback(F.callback.payload == "hw:thisweek")
    async def _hw_thisweek(event: MessageCallback):
        key = _dialog_key(event); st = _st(key)
        group = st.get("group_name") or st.get("group_id")
        if not group:
            st["mode"] = "ASK_GROUP"
            await event.message.answer("Группа не выбрана. Введите номер группы:")
            return
        start = datetime.now().date()
        await _reply_homework_for_week(event, group, start)
        await event.message.answer("Выберите период:", attachments=[_range_kb()])

    @dp.message_callback(F.callback.payload == "hw:nextweek")
    async def _hw_nextweek(event: MessageCallback):
        key = _dialog_key(event); st = _st(key)
        group = st.get("group_name") or st.get("group_id")
        if not group:
            st["mode"] = "ASK_GROUP"
            await event.message.answer("Группа не выбрана. Введите номер группы:")
            return
        start = datetime.now().date() + timedelta(days=7)
        await _reply_homework_for_week(event, group, start)
        await event.message.answer("Выберите период:", attachments=[_range_kb()])

    @dp.message_callback(F.callback.payload == "hw:pickdate")
    async def _hw_pickdate(event: MessageCallback):
        key = _dialog_key(event); st = _st(key)
        st["mode"] = "ASK_DATE"
        await event.message.answer("Введите дату (YYYY-MM-DD или DD.MM.YYYY):")

    @dp.message_callback(F.callback.payload == "hw:change_group")
    async def _hw_change_group(event: MessageCallback):
        key = _dialog_key(event)
        _reset(key); st = _st(key)
        st["mode"] = "ASK_GROUP"
        await event.message.answer("Введите номер группы (например: БИ25-6):")


    @dp.message_callback(F.callback.payload == "hw:add_more")
    async def _go_add_more_for_same_group(event: MessageCallback):
        if _is_old_event(event):
            return
        key = _dialog_key(event)
        st = _st(key)
        group = st.get("group_name") or st.get("group_id")
        if not group:
            st.clear()
            st["mode"] = "ADD_ASK_GROUP"
            st["add"] = {}
            await event.message.answer("Введите номер группы, для которой добавляете ДЗ:")
            return

        st["mode"] = "ADD_ASK_SUBJECT"
        st["add"] = {"group": group, "files": []}
        await event.message.answer(f"Группа: {group}\nВведите название предмета:")


    @dp.message_callback(F.callback.payload == "hw:nofile")
    async def _add_no_files(event: MessageCallback):
        if _is_old_event(event):
            return
        try:
            await _finalize_add(event)
        except Exception as e:
            log.exception("finalize_add (callback) failed: %s", e)
            await event.message.answer("Не удалось сохранить ДЗ.")

    @dp.message_created()
    async def homework_text_states(event: MessageCreated):
        try:
            if _is_old_event(event) or _is_from_bot(event.message):
                return

            text = _msg_text(event).strip()
            key = _dialog_key(event)
            st = _st(key)
            mode = st.get("mode")

            if not text and mode not in ("ADD_WAIT_FILES",):
                return

            if text == "Посмотреть":
                await open_watch_menu(event); return
            if text == "Добавить":
                await _start_add_flow(event); return

            if mode == "ASK_GROUP":
                await open_watch_menu(event); return

            if mode == "IN_GROUP":
                group = st.get("group_name") or st.get("group_id")
                if not group:
                    st["mode"] = "ASK_GROUP"
                    await event.message.answer("Группа не выбрана. Введите номер группы:")
                    return

                low = text.lower()
                today = datetime.now().date()

                if low in {"сегодня"}:
                    await _reply_homework_for_date(event, group, today)
                    await event.message.answer("Выберите период:", attachments=[_range_kb()])
                    return

                if low in {"завтра"}:
                    await _reply_homework_for_date(event, group, today + timedelta(days=1))
                    await event.message.answer("Выберите период:", attachments=[_range_kb()])
                    return

                if low in {"эта неделя", "текущая неделя"}:
                    await _reply_homework_for_week(event, group, today)
                    await event.message.answer("Выберите период:", attachments=[_range_kb()])
                    return

                if low in {"след неделя", "следующая неделя"}:
                    next_week = today + timedelta(days=7)
                    await _reply_homework_for_week(event, group, next_week)
                    await event.message.answer("Выберите период:", attachments=[_range_kb()])
                    return

                if low in {"выбрать дату"}:
                    st["mode"] = "ASK_DATE"
                    await event.message.answer("Введите дату (YYYY-MM-DD или DD.MM.YYYY):")
                    return

                if low in {"сменить группу"}:
                    _reset(key); st = _st(key)
                    st["mode"] = "ASK_GROUP"
                    await event.message.answer("Введите номер группы (например: БИ25-6):")
                    return
                

            if mode == "ASK_DATE":
                group = st.get("group_name") or st.get("group_id")
                d = _parse_user_date(text)
                if not d:
                    await event.message.answer("Не понял дату. Пример: 2025-12-12 или 12.12.2025. Попробуйте ещё раз:")
                    return
                await _reply_homework_for_date(event, group, d)
                st["mode"] = "IN_GROUP"
                await event.message.answer("Выберите период:", attachments=[_range_kb()])
                return

            if await _try_handle_add_flow(event, text):
                return

        except Exception as e:
            log.exception("homework_text_states crash: %s", e)
