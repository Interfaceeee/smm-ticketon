import os
import re
import logging
import asyncio
import tempfile
import functools
import mimetypes
from datetime import date, timedelta, time, datetime
from zoneinfo import ZoneInfo

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ─────────────────────────────────────────────────────────────
#  Настройки
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ASANA_TOKEN = os.environ["ASANA_TOKEN"]

ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "1215525237226401")
ASANA_ASSIGNEE_GID = os.environ.get("ASANA_ASSIGNEE_GID", "1213398188813384")
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "1208507351529750")

# Секции-статусы
SEC_INBOX = os.environ.get("SEC_INBOX", "1215525237226403")
SEC_IN_PROGRESS = os.environ.get("SEC_IN_PROGRESS", "1215525237226410")
SEC_REVIEW = os.environ.get("SEC_REVIEW", "1215525237226411")
SEC_DONE = os.environ.get("SEC_DONE", "1215525237226412")

# Поле "Приоритет"
PRIORITY_FIELD_GID = "1215525237226419"
PRIORITY_OPTIONS = {"high": "1215525237226420", "medium": "1215525237226421", "low": "1215525237226422"}
PRIORITY_LABELS = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}

# Доступ
_RAW_ALLOWED = os.environ.get("ALLOWED_USERS", "").strip()
ALLOWED_USERS = {int(x) for x in _RAW_ALLOWED.split(",") if x.strip()}
_RAW_ADMIN = os.environ.get("ADMIN_ID", "").strip()
ADMIN_ID = int(_RAW_ADMIN) if _RAW_ADMIN else (min(ALLOWED_USERS) if ALLOWED_USERS else None)
_RAW_SMM = os.environ.get("SMM_CHAT_ID", "").strip()
SMM_CHAT_ID = int(_RAW_SMM) if _RAW_SMM else None
runtime_smm_chat_id: int | None = None
_notified_strangers: set[int] = set()

TZ = ZoneInfo("Asia/Bishkek")
ASANA_API = "https://app.asana.com/api/1.0"
URL_RE = re.compile(r"https?://[^\s]+")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger("smm-bot")

# Черновики создания (chat_id -> данные)
drafts: dict[int, dict] = {}
# Ожидания воркфлоу (chat_id -> {"mode": ..., "task_gid": ..., ...})
# mode: "await_post_link" (от Sona) | "await_edit" (от тебя)
waiting: dict[int, dict] = {}


# ─────────────────────────────────────────────────────────────
#  Asana helpers
# ─────────────────────────────────────────────────────────────
def asana_headers():
    return {"Authorization": f"Bearer {ASANA_TOKEN}"}


async def create_task(client, name, notes, priority_key, due_on):
    data = {"name": name, "notes": notes, "projects": [ASANA_PROJECT_GID],
            "assignee": ASANA_ASSIGNEE_GID, "workspace": ASANA_WORKSPACE_GID}
    if due_on:
        data["due_on"] = due_on
    if priority_key in PRIORITY_OPTIONS:
        data["custom_fields"] = {PRIORITY_FIELD_GID: PRIORITY_OPTIONS[priority_key]}
    r = await client.post(f"{ASANA_API}/tasks", json={"data": data}, headers=asana_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["data"]


async def move_to_section(client, task_gid, section_gid):
    if not section_gid:
        return
    try:
        await client.post(f"{ASANA_API}/sections/{section_gid}/addTask",
                          json={"data": {"task": task_gid}}, headers=asana_headers(), timeout=30)
    except Exception as e:
        log.warning("Не переместил в секцию: %s", e)


async def set_completed(client, task_gid, value=True):
    await client.put(f"{ASANA_API}/tasks/{task_gid}", json={"data": {"completed": value}},
                     headers=asana_headers(), timeout=30)


async def get_task(client, task_gid, fields="name,due_on,completed,notes,permalink_url"):
    r = await client.get(f"{ASANA_API}/tasks/{task_gid}", params={"opt_fields": fields},
                         headers=asana_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["data"]


async def update_notes(client, task_gid, notes):
    await client.put(f"{ASANA_API}/tasks/{task_gid}", json={"data": {"notes": notes}},
                     headers=asana_headers(), timeout=30)


async def add_comment(client, task_gid, text):
    await client.post(f"{ASANA_API}/tasks/{task_gid}/stories",
                     json={"data": {"text": text}}, headers=asana_headers(), timeout=30)


async def list_section_tasks(client, section_gid):
    params = {"opt_fields": "name,due_on,completed,notes,permalink_url",
              "completed_since": "now", "limit": 100}
    r = await client.get(f"{ASANA_API}/sections/{section_gid}/tasks",
                        params=params, headers=asana_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["data"]


async def list_attachments(client, task_gid):
    params = {"opt_fields": "name,download_url,resource_subtype"}
    r = await client.get(f"{ASANA_API}/tasks/{task_gid}/attachments",
                        params=params, headers=asana_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def guess_mime(filename):
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


async def attach_file(client, task_gid, filepath, filename):
    """Прикрепляет файл к задаче. Явно указываем MIME-type — иначе Asana
    отвечает 400 на загрузку фото без content-type."""
    mime = guess_mime(filename)
    with open(filepath, "rb") as f:
        files = {"file": (filename, f, mime)}
        r = await client.post(f"{ASANA_API}/tasks/{task_gid}/attachments",
                             data={"parent": task_gid}, files=files,
                             headers=asana_headers(), timeout=120)
    r.raise_for_status()


# ─────────────────────────────────────────────────────────────
#  Текст: разбор и сборка
# ─────────────────────────────────────────────────────────────
def classify_links(links):
    site, post, other = None, None, []
    for u in links:
        low = u.lower()
        if ("ticketon.kg" in low or "ticketon.kz" in low) and site is None:
            site = u
        elif ("instagram.com" in low or "instagr.am" in low) and post is None:
            post = u
        else:
            other.append(u)
    return site, post, other


def assemble(texts):
    full = "\n".join(t for t in texts if t and t.strip()).strip()
    links = URL_RE.findall(full)
    seen, uniq = set(), []
    for u in links:
        if u not in seen:
            seen.add(u); uniq.append(u)
    if not full:
        return "Задача на пост", "", uniq
    body_text = URL_RE.sub("", full).strip()
    lines = body_text.split("\n")
    title, rest, taken = "Задача на пост", [], False
    for l in lines:
        if not taken and l.strip():
            title = l.strip()[:120]; taken = True
        elif taken:
            rest.append(l)
    return title, "\n".join(rest).strip(), uniq


def build_notes(body, links, priority_key, due_label, sender, post_link=None):
    site, post, other = classify_links(links)
    if post_link and not post:
        post = post_link
    head = []
    if site:
        head.append("🌐 Сайт: " + site)
    if post:
        head.append("📸 Пост: " + post)
    for u in other:
        head.append("🔗 Ссылка: " + u)
    if due_label:
        head.append("📅 Дата: " + due_label)
    if priority_key:
        head.append("🚩 Приоритет: " + PRIORITY_LABELS[priority_key])
    parts = []
    if head:
        parts.append("\n".join(head)); parts.append("──────────")
    if body:
        parts.append(body)
    parts.append(f"\n— Создано через бота, от {sender}")
    return "\n".join(parts)


def set_post_in_notes(notes, post_url):
    """Вставляет/заменяет строку 📸 Пост в существующих notes."""
    lines = (notes or "").split("\n")
    out, inserted = [], False
    for l in lines:
        if l.startswith("📸 Пост:"):
            out.append(f"📸 Пост: {post_url}"); inserted = True
        else:
            out.append(l)
    if not inserted:
        # вставим после строки сайта или в начало
        new = []
        placed = False
        for l in out:
            new.append(l)
            if l.startswith("🌐 Сайт:") and not placed:
                new.append(f"📸 Пост: {post_url}"); placed = True
        if not placed:
            new = [f"📸 Пост: {post_url}"] + out
        out = new
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────
#  Получатели уведомлений
# ─────────────────────────────────────────────────────────────
def smm_chat_id():
    return SMM_CHAT_ID or runtime_smm_chat_id


# ─────────────────────────────────────────────────────────────
#  Клавиатуры
# ─────────────────────────────────────────────────────────────
def collect_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Создать задачу", callback_data="collect")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def priority_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔴 Высокий", callback_data="p|high"),
        InlineKeyboardButton("🟡 Средний", callback_data="p|medium"),
        InlineKeyboardButton("🟢 Низкий", callback_data="p|low"),
    ]])


def date_keyboard():
    today = date.today()
    rows, row = [], []
    labels = []
    for i in range(0, 7):
        d = today + timedelta(days=i)
        name = {0: "Сегодня", 1: "Завтра"}.get(i, d.strftime("%d.%m"))
        labels.append((name, d.isoformat()))
    labels.append(("+2 недели", (today + timedelta(days=14)).isoformat()))
    labels.append(("Без даты", "none"))
    for name, iso in labels:
        row.append(InlineKeyboardButton(name, callback_data=f"d|{iso}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✍️ Дата вручную", callback_data="d|manual")])
    return InlineKeyboardMarkup(rows)


def take_keyboard(task_gid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🤝 Взять в работу", callback_data=f"take|{task_gid}")
    ]])


def review_keyboard(task_gid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Внести правки", callback_data=f"edit|{task_gid}"),
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve|{task_gid}"),
    ]])


def edit_send_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📨 Отправить правку", callback_data="edit_send"),
        InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"),
    ]])


def rework_send_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📨 Отправить на проверку", callback_data="rework_send"),
        InlineKeyboardButton("❌ Отмена", callback_data="rework_cancel"),
    ]])


def resolve_due(key):
    if key == "none":
        return None, None
    d = date.fromisoformat(key)
    return d.isoformat(), d.strftime("%d.%m.%Y")


# ─────────────────────────────────────────────────────────────
#  Панель создания
# ─────────────────────────────────────────────────────────────
def draft_summary(draft):
    title, body, links = assemble(draft["texts"])
    site, post, other = classify_links(links)
    n_files = len(draft["files"])
    lines = [f"📝 Новая задача: «{title}»"]
    extras = []
    if body: extras.append("текст ✚")
    if site: extras.append("сайт ✓")
    if other: extras.append(f"ссылок: {len(other)}")
    if n_files: extras.append(f"вложений: {n_files}")
    if extras:
        lines.append("Собрано: " + ", ".join(extras))
    lines.append("\nКидай ещё или жми «Создать задачу».")
    return "\n".join(lines)


async def refresh_panel(context, chat_id):
    draft = drafts.get(chat_id)
    if not draft:
        return
    text = draft_summary(draft)
    if draft.get("panel_msg_id"):
        try:
            await context.bot.edit_message_text(text, chat_id, draft["panel_msg_id"],
                                                reply_markup=collect_keyboard())
            return
        except Exception:
            pass
    msg = await context.bot.send_message(chat_id, text, reply_markup=collect_keyboard())
    draft["panel_msg_id"] = msg.message_id


async def schedule_refresh(context, chat_id):
    draft = drafts.get(chat_id)
    if not draft:
        return
    old = draft.get("debounce_task")
    if old and not old.done():
        old.cancel()

    async def _later():
        try:
            await asyncio.sleep(1.0)
            await refresh_panel(context, chat_id)
        except asyncio.CancelledError:
            pass
    draft["debounce_task"] = context.application.create_task(_later())


# ─────────────────────────────────────────────────────────────
#  Создание задачи → уведомление Sona «Взять в работу»
# ─────────────────────────────────────────────────────────────
async def finalize_task(context, chat_id):
    draft = drafts.get(chat_id)
    if not draft:
        return
    bot = context.bot
    priority_key = draft.get("priority")
    due_iso = draft.get("due_iso")
    due_label = date.fromisoformat(due_iso).strftime("%d.%m.%Y") if due_iso else None
    title, body, links = assemble(draft["texts"])
    notes = build_notes(body, links, priority_key, due_label, draft["sender"])
    panel_id = draft["panel_msg_id"]

    async with httpx.AsyncClient() as client:
        try:
            task = await create_task(client, title, notes, priority_key, due_iso)
            task_gid = task["gid"]
            await move_to_section(client, task_gid, SEC_INBOX)
        except Exception as e:
            log.exception("Создание задачи")
            await bot.edit_message_text(f"❌ Не смог создать задачу:\n{e}", chat_id, panel_id)
            drafts.pop(chat_id, None)
            return

        attached = 0
        for file_id, filename in draft["files"]:
            try:
                tg_file = await bot.get_file(file_id)
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp_path = tmp.name
                await tg_file.download_to_drive(tmp_path)
                await attach_file(client, task_gid, tmp_path, filename)
                os.unlink(tmp_path); attached += 1
            except Exception as e:
                log.warning("Вложение %s: %s", filename, e)

    permalink = task.get("permalink_url", "")
    # ответ тебе
    out = [f"✅ Задача создана и отправлена СММ: {title}"]
    if due_label: out.append(f"📅 {due_label}")
    if attached: out.append(f"📎 Вложений: {attached}")
    if permalink: out.append(permalink)
    await bot.edit_message_text("\n".join(out), chat_id, panel_id)
    drafts.pop(chat_id, None)

    # уведомление Sona
    smm = smm_chat_id()
    if not smm:
        await bot.send_message(chat_id, "⚠️ Не задан получатель СММ (SMM_CHAT_ID / /setsmm) — некому отправить.")
        return
    nlines = [f"🆕 Новая задача на пост:\n", f"📌 {title}"]
    if due_label: nlines.append(f"📅 {due_label}")
    if priority_key: nlines.append(f"🚩 {PRIORITY_LABELS[priority_key]}")
    site, _, _ = classify_links(links)
    if site: nlines.append(f"🌐 {site}")
    if permalink: nlines.append(f"🗂 Асана: {permalink}")
    try:
        await bot.send_message(smm, "\n".join(nlines), reply_markup=take_keyboard(task_gid),
                              disable_web_page_preview=True)
    except Exception as e:
        await bot.send_message(chat_id, f"⚠️ Не смог уведомить СММ: {e}")


# ─────────────────────────────────────────────────────────────
#  Доступ
# ─────────────────────────────────────────────────────────────
def is_allowed(user_id):
    if not ALLOWED_USERS:
        return True
    return user_id is not None and user_id in ALLOWED_USERS


def is_admin(user_id):
    return ADMIN_ID is not None and user_id == ADMIN_ID


async def notify_admin_request(context, user, chat_id, text_preview):
    if ADMIN_ID is None or user is None or user.id in _notified_strangers:
        return
    _notified_strangers.add(user.id)
    uname = f"@{user.username}" if user.username else "—"
    preview = (text_preview or "").strip()
    if len(preview) > 200:
        preview = preview[:200] + "…"
    msg = ("🔔 Запрос доступа к боту\n\n"
           f"👤 Имя: {user.full_name or '—'}\n🔗 Username: {uname}\n"
           f"🆔 ID: `{user.id}`\n💬 chat_id: `{chat_id}`\n")
    if preview:
        msg += f"\n📝 Написал(а): {preview}\n"
    msg += "\nДоступ — добавь ID в Railway → ALLOWED_USERS и сохрани."
    try:
        await context.bot.send_message(ADMIN_ID, msg, parse_mode="Markdown")
    except Exception as e:
        log.warning("Уведомление админа: %s", e)


def restricted(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        uid = user.id if user else None
        if not is_allowed(uid):
            log.info("Отклонён доступ: user_id=%s username=%s", uid, getattr(user, "username", None))
            chat_id = update.effective_chat.id if update.effective_chat else None
            preview = update.message.text if (update.message and update.message.text) else ""
            await notify_admin_request(context, user, chat_id, preview)
            if update.callback_query:
                try:
                    await update.callback_query.answer("Нет доступа. Запрос отправлен админу.", show_alert=True)
                except Exception:
                    pass
            elif update.message:
                try:
                    await update.message.reply_text(
                        f"🔒 Доступ ограничен.\n\nТвой ID: `{uid}`\nПередай администратору.",
                        parse_mode="Markdown")
                except Exception:
                    pass
            return
        return await handler(update, context)
    return wrapper


# ─────────────────────────────────────────────────────────────
#  Команды
# ─────────────────────────────────────────────────────────────
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id

    if is_admin(uid):
        # Текст для тебя (админа)
        await update.message.reply_text(
            "Привет! Кидай материал задачи на пост — можно несколькими сообщениями "
            "(текст, афиша, ссылки, файлы). Жми «✅ Создать задачу», выбери приоритет и дату.\n\n"
            "Дальше задача идёт по воркфлоу: СММ берёт в работу → публикует → "
            "присылает ссылку → ты проверяешь (правки/одобрить) → Done.\n\n"
            "• /digest — список активных задач\n"
            "• /setsmm — назначить этот чат получателем уведомлений СММ\n"
            "• /cancel — сбросить черновик\n\n"
            f"Твой Telegram ID / chat_id: {uid} / {chat_id}"
        )
    else:
        # Текст для СММ (разрешённый пользователь, но не админ)
        await update.message.reply_text(
            "Привет! Я бот для задач на пост. 🎬\n\n"
            "Как работаем:\n"
            "1. Тебе приходит новая задача с кнопкой «🤝 Взять в работу» — жми, когда берёшься.\n"
            "2. Публикуешь пост и присылаешь мне сюда ссылку на Instagram — я передам на проверку.\n"
            "3. Если будут правки — пришлю их сюда, смотри в Асане. Если всё ок — задача закроется.\n\n"
            "• /digest — показать твои активные задачи\n"
            "• Под каждой задачей есть «📎 Прислать файлы» — выгружу афишу/видео из Асаны сюда.\n\n"
            "Чтобы получать ежедневный список задач — напиши /setsmm один раз."
        )


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    dropped = drafts.pop(cid, None) or waiting.pop(cid, None)
    await update.message.reply_text("🗑 Сброшено." if dropped else "Нет активного черновика.")


@restricted
async def cmd_setsmm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global runtime_smm_chat_id
    runtime_smm_chat_id = update.effective_chat.id
    uid = update.effective_user.id if update.effective_user else None
    if is_admin(uid):
        # тебе — техническая инструкция
        await update.message.reply_text(
            f"✅ Этот чат назначен получателем уведомлений СММ. chat_id: {runtime_smm_chat_id}\n"
            "Впиши его в Railway → SMM_CHAT_ID, чтобы сохранилось после рестарта."
        )
    else:
        # СММ — понятный текст без технических деталей
        await update.message.reply_text(
            "✅ Готово! Теперь ты будешь получать сюда список задач на день "
            "и уведомления о новых постах. Можешь приступать к работе 🙌"
        )


@restricted
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await build_and_send_digest(context, target_chat=update.effective_chat.id)


# ─────────────────────────────────────────────────────────────
#  Дайджест (активные задачи: Inbox + In progress + Under review)
# ─────────────────────────────────────────────────────────────
TG_PHOTO_LIMIT = 10 * 1024 * 1024
TG_FILE_LIMIT = 50 * 1024 * 1024
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
VID_EXT = (".mp4", ".mov", ".m4v")


async def build_and_send_digest(context, target_chat=None):
    chat = target_chat or smm_chat_id()
    if not chat:
        log.warning("Дайджест: нет получателя")
        return
    async with httpx.AsyncClient() as client:
        try:
            tagged = []  # (task, section_code)
            for sec, code in ((SEC_INBOX, "inbox"), (SEC_IN_PROGRESS, "progress"), (SEC_REVIEW, "review")):
                for t in await list_section_tasks(client, sec):
                    tagged.append((t, code))
        except Exception as e:
            log.exception("Дайджест чтение")
            if target_chat:
                await context.bot.send_message(chat, f"⚠️ Ошибка Асаны: {e}")
            return

    # уникализируем по gid (первая встреченная секция приоритетна: inbox→progress→review)
    seen, uniq = set(), []
    for t, code in tagged:
        if t["gid"] not in seen:
            seen.add(t["gid"]); uniq.append((t, code))
    uniq.sort(key=lambda x: x[0].get("due_on") or "9999-12-31")

    if not uniq:
        await context.bot.send_message(chat, "📭 Активных задач нет.")
        return

    await context.bot.send_message(chat, f"📋 Активные задачи — {len(uniq)} шт.")
    status_label = {"inbox": "🆕 Новая", "progress": "🔄 В работе", "review": "⏳ На проверке"}
    for t, code in uniq:
        due = t.get("due_on")
        when = date.fromisoformat(due).strftime("%d.%m.%Y") if due else "без даты"
        block = [f"📌 {t['name']}", f"{status_label.get(code, '')}  📅 {when}"]
        site, post, _ = classify_links(URL_RE.findall(t.get("notes") or ""))
        if site: block.append(f"🌐 Сайт: {site}")
        if post: block.append(f"📸 Пост: {post}")
        if t.get("permalink_url"): block.append(f"🗂 Асана: {t['permalink_url']}")

        gid = t["gid"]
        files_btn = InlineKeyboardButton("📎 Файлы", callback_data=f"files|{gid}")
        if code == "inbox":
            rows = [[InlineKeyboardButton("🤝 Взять в работу", callback_data=f"take|{gid}")],
                    [files_btn]]
        elif code == "progress":
            rows = [[InlineKeyboardButton("✅ Отправить ссылку на пост", callback_data=f"link|{gid}")],
                    [files_btn]]
        else:  # review
            rows = [[files_btn]]
        await context.bot.send_message(chat, "\n".join(block),
                                      reply_markup=InlineKeyboardMarkup(rows),
                                      disable_web_page_preview=True)


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE):
    await build_and_send_digest(context)


async def send_event_files(context, chat_id, task_gid):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            atts = await list_attachments(client, task_gid)
        except Exception as e:
            await context.bot.send_message(chat_id, f"⚠️ Не получил вложения: {e}")
            return
        if not atts:
            await context.bot.send_message(chat_id, "📭 У задачи нет файлов.")
            return
        await context.bot.send_message(chat_id, f"📎 Отправляю файлы ({len(atts)})…")
        sent, skipped = 0, []
        for a in atts:
            name = a.get("name") or "file"; url = a.get("download_url")
            if not url:
                skipped.append(name); continue
            try:
                resp = await client.get(url, timeout=120); resp.raise_for_status()
                content = resp.content; size = len(content); low = name.lower()
                if low.endswith(IMG_EXT) and size <= TG_PHOTO_LIMIT:
                    await context.bot.send_photo(chat_id, photo=content, caption=name)
                elif low.endswith(VID_EXT) and size <= TG_FILE_LIMIT:
                    await context.bot.send_video(chat_id, video=content, caption=name)
                elif size <= TG_FILE_LIMIT:
                    await context.bot.send_document(chat_id, document=content, filename=name)
                else:
                    skipped.append(f"{name} (большой)"); continue
                sent += 1
            except Exception as e:
                log.warning("Файл %s: %s", name, e); skipped.append(name)
        if sent:
            await context.bot.send_message(chat_id, f"✅ Отправлено: {sent}")
        if skipped:
            await context.bot.send_message(chat_id, "⚠️ Не отправлено (открой в Асане):\n" +
                                          "\n".join(f"• {s}" for s in skipped))


# ─────────────────────────────────────────────────────────────
#  Сообщения
# ─────────────────────────────────────────────────────────────
def file_from_message(msg):
    if msg.photo:
        return msg.photo[-1].file_id, f"photo_{msg.photo[-1].file_unique_id}.jpg"
    if msg.video:
        return msg.video.file_id, msg.video.file_name or f"video_{msg.video.file_unique_id}.mp4"
    if msg.document:
        return msg.document.file_id, msg.document.file_name or f"doc_{msg.document.file_unique_id}"
    if msg.animation:
        return msg.animation.file_id, f"anim_{msg.animation.file_unique_id}.mp4"
    return None


def parse_manual_date(text):
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    sender = update.effective_user.full_name if update.effective_user else "—"

    # 1) Ждём ссылку на пост от Sona — принимаем ТОЛЬКО instagram-ссылку
    w = waiting.get(chat_id)
    if w and w["mode"] == "await_post_link":
        text = (msg.text or msg.caption or "").strip()
        urls = URL_RE.findall(text)
        post_url = None
        for u in urls:
            if "instagr" in u.lower():
                post_url = u
                break
        if not post_url:
            await msg.reply_text(
                "❗️Нужна ссылка именно на пост в Instagram "
                "(например https://www.instagram.com/p/...).\n"
                "Пришли её, пожалуйста, ещё раз."
            )
            return
        await handle_post_link(context, chat_id, w["task_gid"], post_url, sender)
        waiting.pop(chat_id, None)
        return

    # 2) Ждём правку от тебя (копим текст+файлы, отправляем по кнопке)
    if w and w["mode"] == "await_edit":
        text = msg.text or msg.caption
        if text:
            w.setdefault("texts", []).append(text)
        ft = file_from_message(msg)
        if ft:
            w.setdefault("files", []).append(ft)
        # обновим панель правки
        n = len(w.get("files", []))
        has_text = bool(w.get("texts"))
        summary = "✏️ Правка: " + ", ".join(
            ([f"текст ✚"] if has_text else []) + ([f"файлов: {n}"] if n else [])
        ) + "\nКидай ещё или жми «Отправить правку»."
        try:
            if w.get("panel_msg_id"):
                await context.bot.edit_message_text(summary, chat_id, w["panel_msg_id"],
                                                   reply_markup=edit_send_keyboard())
            else:
                pm = await context.bot.send_message(chat_id, summary, reply_markup=edit_send_keyboard())
                w["panel_msg_id"] = pm.message_id
        except Exception:
            pass
        return

    # 2b) СММ дорабатывает после правки (копит что изменила, опционально)
    if w and w["mode"] == "await_rework":
        text = msg.text or msg.caption
        if text:
            w.setdefault("texts", []).append(text)
        ft = file_from_message(msg)
        if ft:
            w.setdefault("files", []).append(ft)
        n = len(w.get("files", []))
        has_text = bool(w.get("texts"))
        summary = "🔧 Доработка: " + ", ".join(
            ([f"текст ✚"] if has_text else []) + ([f"файлов: {n}"] if n else [])
        ) + "\nПриложи что изменила (необязательно) и жми «Отправить на проверку»."
        try:
            if w.get("panel_msg_id"):
                await context.bot.edit_message_text(summary, chat_id, w["panel_msg_id"],
                                                   reply_markup=rework_send_keyboard())
            else:
                pm = await context.bot.send_message(chat_id, summary, reply_markup=rework_send_keyboard())
                w["panel_msg_id"] = pm.message_id
        except Exception:
            pass
        return

    # 3) Обычный сбор новой задачи
    draft = drafts.get(chat_id)
    if not draft:
        draft = {"texts": [], "files": [], "sender": sender, "panel_msg_id": None,
                 "priority": None, "due_iso": None, "debounce_task": None, "stage": "collecting"}
        drafts[chat_id] = draft

    if draft.get("stage") == "await_manual_date":
        iso = parse_manual_date(msg.text or "")
        if not iso:
            await msg.reply_text("Формат: ДД.ММ.ГГГГ (напр. 25.08.2026).")
            return
        draft["due_iso"] = iso
        await msg.reply_text("⏳ Создаю задачу…")
        await finalize_task(context, chat_id)
        return

    # Если задача уже на стадии выбора приоритета/даты, но ещё НЕ создана —
    # всё равно принимаем доп. текст/файлы (вдруг фото пришло чуть позже).
    if draft.get("stage") in ("priority", "date"):
        text = msg.text or msg.caption
        added = []
        if text:
            draft["texts"].append(text); added.append("текст")
        ft = file_from_message(msg)
        if ft:
            draft["files"].append(ft); added.append("файл")
        if added:
            await msg.reply_text(
                f"➕ Добавил ({', '.join(added)}) к задаче. "
                f"Всего вложений: {len(draft['files'])}. Продолжай выбор выше 👆"
            )
        return

    if draft.get("stage") != "collecting":
        await msg.reply_text("⏳ Задача уже создаётся. Подожди или /cancel.")
        return

    text = msg.text or msg.caption
    if text:
        draft["texts"].append(text)
    ft = file_from_message(msg)
    if ft:
        draft["files"].append(ft)
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    await schedule_refresh(context, chat_id)


# ─────────────────────────────────────────────────────────────
#  Воркфлоу-переходы
# ─────────────────────────────────────────────────────────────
async def handle_post_link(context, smm_chat, task_gid, post_url, sender):
    """Sona прислала ссылку на пост → вписываем, двигаем в Under review, шлём тебе."""
    bot = context.bot
    async with httpx.AsyncClient() as client:
        try:
            t = await get_task(client, task_gid)
            new_notes = set_post_in_notes(t.get("notes") or "", post_url)
            await update_notes(client, task_gid, new_notes)
            await move_to_section(client, task_gid, SEC_REVIEW)
            t = await get_task(client, task_gid)
        except Exception as e:
            await bot.send_message(smm_chat, f"⚠️ Ошибка обновления задачи: {e}")
            return
    await bot.send_message(smm_chat, "✅ Ссылка добавлена, задача отправлена на проверку.")
    # тебе на проверку
    if ADMIN_ID:
        block = [f"👀 На проверку: {t['name']}", f"📸 Пост: {post_url}"]
        if t.get("permalink_url"):
            block.append(f"🗂 Асана: {t['permalink_url']}")
        await bot.send_message(ADMIN_ID, "\n".join(block),
                              reply_markup=review_keyboard(task_gid),
                              disable_web_page_preview=True)


async def finalize_edit(context, admin_chat, w):
    """Отправляет накопленную правку комментарием в задачу + уведомляет Sona."""
    bot = context.bot
    task_gid = w["task_gid"]
    texts = w.get("texts", [])
    files = w.get("files", [])
    comment = "✏️ Правка от заказчика:\n" + ("\n".join(texts) if texts else "(см. вложения)")
    failed_files = []
    async with httpx.AsyncClient() as client:
        try:
            await add_comment(client, task_gid, comment)
        except Exception as e:
            await bot.send_message(admin_chat, f"⚠️ Не смог отправить правку: {e}")
            return
        # вложения — каждое в своём try, чтобы одно битое не валило всё
        for file_id, filename in files:
            tmp_path = None
            try:
                tg_file = await bot.get_file(file_id)
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp_path = tmp.name
                await tg_file.download_to_drive(tmp_path)
                await attach_file(client, task_gid, tmp_path, filename)
            except Exception as e:
                log.warning("Вложение правки %s: %s", filename, e)
                failed_files.append(filename)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        try:
            await move_to_section(client, task_gid, SEC_IN_PROGRESS)
            t = await get_task(client, task_gid)
        except Exception as e:
            await bot.send_message(admin_chat, f"⚠️ Правка добавлена, но статус не обновился: {e}")
            return

    msg = "📨 Правка отправлена СММ."
    if failed_files:
        msg += f"\n⚠️ Не загрузились файлы: {len(failed_files)} (текст правки ушёл)."
    await bot.send_message(admin_chat, msg)
    smm = smm_chat_id()
    if smm:
        block = [f"✏️ Есть правка по задаче: {t['name']}"]
        if t.get("permalink_url"):
            block.append(f"🗂 Смотри в Асане: {t['permalink_url']}")
        block.append("\nКогда доработаешь — жми кнопку ниже 👇")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Готово, на проверку", callback_data=f"ready|{task_gid}")
        ]])
        await bot.send_message(smm, "\n".join(block), reply_markup=kb,
                              disable_web_page_preview=True)


async def finalize_rework(context, smm_chat, w):
    """СММ доработала: опц. комментарий о доработке + файлы → Under review → тебе на проверку."""
    bot = context.bot
    task_gid = w["task_gid"]
    texts = w.get("texts", [])
    files = w.get("files", [])
    failed_files = []
    async with httpx.AsyncClient() as client:
        # комментарий о доработке (если есть что сказать)
        if texts:
            try:
                await add_comment(client, task_gid, "🔧 Доработка от СММ:\n" + "\n".join(texts))
            except Exception as e:
                log.warning("Комментарий доработки: %s", e)
        for file_id, filename in files:
            tmp_path = None
            try:
                tg_file = await bot.get_file(file_id)
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp_path = tmp.name
                await tg_file.download_to_drive(tmp_path)
                await attach_file(client, task_gid, tmp_path, filename)
            except Exception as e:
                log.warning("Файл доработки %s: %s", filename, e)
                failed_files.append(filename)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        try:
            await move_to_section(client, task_gid, SEC_REVIEW)
            t = await get_task(client, task_gid)
        except Exception as e:
            await bot.send_message(smm_chat, f"⚠️ Не смог отправить на проверку: {e}")
            return
    msg = "✅ Отправлено заказчику на проверку."
    if failed_files:
        msg += f"\n⚠️ Не загрузились файлы: {len(failed_files)}."
    await bot.send_message(smm_chat, msg)
    # тебе на проверку
    if ADMIN_ID:
        site, post, _ = classify_links(URL_RE.findall(t.get("notes") or ""))
        block = [f"👀 Доработано, на проверку: {t['name']}"]
        if post:
            block.append(f"📸 Пост: {post}")
        if t.get("permalink_url"):
            block.append(f"🗂 Асана: {t['permalink_url']}")
        await bot.send_message(ADMIN_ID, "\n".join(block),
                              reply_markup=review_keyboard(task_gid),
                              disable_web_page_preview=True)


# ─────────────────────────────────────────────────────────────
#  Кнопки
# ─────────────────────────────────────────────────────────────
@restricted
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    # ── воркфлоу-кнопки ──
    if data.startswith("files|"):
        await send_event_files(context, chat_id, data.split("|", 1)[1])
        return

    if data.startswith("take|"):
        task_gid = data.split("|", 1)[1]
        async with httpx.AsyncClient() as client:
            try:
                await move_to_section(client, task_gid, SEC_IN_PROGRESS)
                t = await get_task(client, task_gid)
            except Exception as e:
                await query.edit_message_text(f"⚠️ Ошибка: {e}")
                return
        lines = [f"🤝 Взято в работу: {t['name']}"]
        if t.get("permalink_url"):
            lines.append(f"🗂 Асана: {t['permalink_url']}")
        lines.append("\nКогда опубликуешь — пришли сюда ссылку на пост (Instagram).")
        await query.edit_message_text("\n".join(lines), disable_web_page_preview=True)
        waiting[chat_id] = {"mode": "await_post_link", "task_gid": task_gid}
        if ADMIN_ID:
            admin_line = f"🤝 СММ взял(а) в работу: {t['name']}"
            await context.bot.send_message(ADMIN_ID, admin_line)
        return

    if data.startswith("link|"):
        task_gid = data.split("|", 1)[1]
        async with httpx.AsyncClient() as client:
            try:
                t = await get_task(client, task_gid)
            except Exception as e:
                await query.answer(f"Ошибка: {e}", show_alert=True)
                return
        waiting[chat_id] = {"mode": "await_post_link", "task_gid": task_gid}
        lines = [f"🔗 Пришли ссылку на пост (Instagram) для задачи:", f"📌 {t['name']}"]
        if t.get("permalink_url"):
            lines.append(f"🗂 Асана: {t['permalink_url']}")
        await context.bot.send_message(chat_id, "\n".join(lines), disable_web_page_preview=True)
        return

    if data.startswith("edit|"):
        task_gid = data.split("|", 1)[1]
        waiting[chat_id] = {"mode": "await_edit", "task_gid": task_gid, "texts": [], "files": [], "panel_msg_id": None}
        await query.edit_message_text(
            "✏️ Опиши правку: текст и/или фото (можно файлами и несколько). "
            "Когда всё — жми «Отправить правку»."
        )
        pm = await context.bot.send_message(chat_id, "✏️ Правка: пусто.\nКидай текст/файлы.",
                                           reply_markup=edit_send_keyboard())
        waiting[chat_id]["panel_msg_id"] = pm.message_id
        return

    if data == "edit_send":
        w = waiting.get(chat_id)
        if not w or w.get("mode") != "await_edit":
            await query.edit_message_text("⌛ Нет активной правки.")
            return
        if not w.get("texts") and not w.get("files"):
            await query.answer("Правка пустая", show_alert=True)
            return
        await query.edit_message_text("⏳ Отправляю правку…")
        await finalize_edit(context, chat_id, w)
        waiting.pop(chat_id, None)
        return

    if data == "edit_cancel":
        waiting.pop(chat_id, None)
        await query.edit_message_text("❌ Правка отменена.")
        return

    if data.startswith("ready|"):
        # СММ нажала «Готово, на проверку» → собираем доработку
        task_gid = data.split("|", 1)[1]
        waiting[chat_id] = {"mode": "await_rework", "task_gid": task_gid,
                            "texts": [], "files": [], "panel_msg_id": None}
        await query.edit_message_text(
            "🔧 Что доработала? Можешь приложить новый текст/фото (необязательно). "
            "Когда готово — жми «Отправить на проверку»."
        )
        pm = await context.bot.send_message(
            chat_id, "🔧 Доработка: пусто.\nПриложи изменения или сразу жми «Отправить на проверку».",
            reply_markup=rework_send_keyboard())
        waiting[chat_id]["panel_msg_id"] = pm.message_id
        return

    if data == "rework_send":
        w = waiting.get(chat_id)
        if not w or w.get("mode") != "await_rework":
            await query.edit_message_text("⌛ Нет активной доработки.")
            return
        await query.edit_message_text("⏳ Отправляю на проверку…")
        await finalize_rework(context, chat_id, w)
        waiting.pop(chat_id, None)
        return

    if data == "rework_cancel":
        waiting.pop(chat_id, None)
        await query.edit_message_text("❌ Отменено. Нажми «Готово, на проверку» снова, когда будешь готова.")
        return

    if data.startswith("approve|"):
        task_gid = data.split("|", 1)[1]
        async with httpx.AsyncClient() as client:
            try:
                await move_to_section(client, task_gid, SEC_DONE)
                await set_completed(client, task_gid, True)
                t = await get_task(client, task_gid)
            except Exception as e:
                await query.edit_message_text(f"⚠️ Ошибка: {e}")
                return
        await query.edit_message_text(f"✅ Одобрено и закрыто: {t['name']}")
        smm = smm_chat_id()
        if smm:
            await context.bot.send_message(smm, f"✅ Задача одобрена и закрыта: {t['name']}")
        return

    # ── кнопки создания задачи ──
    draft = drafts.get(chat_id)
    if data == "cancel":
        drafts.pop(chat_id, None)
        await query.edit_message_text("🗑 Черновик отменён.")
        return
    if not draft:
        await query.edit_message_text("⌛ Черновик устарел. Кидай материал заново.")
        return
    if data == "collect":
        if not draft["texts"] and not draft["files"]:
            await query.answer("Пусто", show_alert=True)
            return
        draft["stage"] = "priority"
        title, _, _ = assemble(draft["texts"])
        await query.edit_message_text(f"📝 «{title}»\n\nПриоритет:", reply_markup=priority_keyboard())
        return
    if data.startswith("p|"):
        draft["priority"] = data.split("|")[1]
        draft["stage"] = "date"
        title, _, _ = assemble(draft["texts"])
        await query.edit_message_text(
            f"📝 «{title}»\n🚩 {PRIORITY_LABELS[draft['priority']]}\n\nДата:",
            reply_markup=date_keyboard())
        return
    if data.startswith("d|"):
        val = data.split("|", 1)[1]
        if val == "manual":
            draft["stage"] = "await_manual_date"
            await query.edit_message_text("✍️ Напиши дату: ДД.ММ.ГГГГ")
            return
        draft["due_iso"] = None if val == "none" else val
        title, _, _ = assemble(draft["texts"])
        await query.edit_message_text(f"📝 «{title}»\n⏳ Создаю задачу…")
        await finalize_task(context, chat_id)
        return


async def on_startup(app: Application):
    # JobQueue требует extra [job-queue]. Если он не установлен (app.job_queue is None),
    # не роняем бота — просто пропускаем планирование. Ручной /digest продолжит работать.
    if app.job_queue is None:
        log.warning("JobQueue недоступен — ежедневный дайджест по расписанию выключен. "
                    "Установи зависимость python-telegram-bot[job-queue]. /digest работает.")
        return
    try:
        app.job_queue.run_daily(daily_digest_job, time=time(hour=10, minute=0, tzinfo=TZ),
                                name="daily_digest")
        log.info("Дайджест на 10:00 Asia/Bishkek")
    except Exception as e:
        log.warning("Не удалось запланировать дайджест: %s", e)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("setsmm", cmd_setsmm))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION)
        & ~filters.COMMAND, handle_message))
    log.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
