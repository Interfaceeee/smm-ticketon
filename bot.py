import os
import re
import logging
import asyncio
import tempfile
import functools
from datetime import date, timedelta, time
from zoneinfo import ZoneInfo

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────────────────────
#  Настройки (переменные окружения на Railway)
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ASANA_TOKEN = os.environ["ASANA_TOKEN"]

ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "1215525237226401")
ASANA_ASSIGNEE_GID = os.environ.get("ASANA_ASSIGNEE_GID", "1213398188813384")
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "1208507351529750")

# Секция "События для сторис" (GID уже зашит, можно переопределить переменной)
ASANA_EVENTS_SECTION_GID = os.environ.get("ASANA_EVENTS_SECTION_GID", "1215779880131861")

# Кастомное поле "Приоритет" и GID его опций
PRIORITY_FIELD_GID = "1215525237226419"
PRIORITY_OPTIONS = {
    "high": "1215525237226420",    # Высокий
    "medium": "1215525237226421",  # Средний
    "low": "1215525237226422",     # Низкий
}
PRIORITY_LABELS = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}

# Единый белый список: кто может пользоваться ботом (id через запятую).
# Применяется ко ВСЕМ командам, сообщениям и кнопкам. Чужие игнорируются.
# Если список пуст и переменная не задана — бот предупреждает в логах и
# работает в открытом режиме (для первичной настройки). Как только впишешь
# хотя бы один id — доступ только у перечисленных.
_RAW_ALLOWED = os.environ.get("ALLOWED_USERS", "").strip()
ALLOWED_USERS = {int(x) for x in _RAW_ALLOWED.split(",") if x.strip()}
# Chat id СММщицы для дайджеста (узнаётся по /start, впиши в Railway для надёжности)
SMM_CHAT_ID = os.environ.get("SMM_CHAT_ID", "").strip()
# Админ — кому бот шлёт уведомления о запросах доступа от чужих (обычно твой id).
# Если не задан, берётся первый из ALLOWED_USERS.
_RAW_ADMIN = os.environ.get("ADMIN_ID", "").strip()
ADMIN_ID = int(_RAW_ADMIN) if _RAW_ADMIN else (
    min(ALLOWED_USERS) if ALLOWED_USERS else None
)
# Чтобы не спамить тебя — не уведомляем повторно об одном и том же чужом id
_notified_strangers: set[int] = set()

TZ = ZoneInfo("Asia/Bishkek")
ASANA_API = "https://app.asana.com/api/1.0"
URL_RE = re.compile(r"https?://[^\s]+")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("smm-bot")

# Активные черновики (chat_id -> данные)
drafts: dict[int, dict] = {}
# Запомненный chat_id СММщицы в рантайме (если не задан через переменную)
runtime_smm_chat_id: int | None = None


# ─────────────────────────────────────────────────────────────
#  Хелперы Asana
# ─────────────────────────────────────────────────────────────
def asana_headers():
    return {"Authorization": f"Bearer {ASANA_TOKEN}"}


async def create_event_task(client, name, notes, priority_key, due_on):
    data = {
        "name": name,
        "notes": notes,
        "projects": [ASANA_PROJECT_GID],
        "assignee": ASANA_ASSIGNEE_GID,
        "workspace": ASANA_WORKSPACE_GID,
    }
    if due_on:
        data["due_on"] = due_on
    if priority_key and priority_key in PRIORITY_OPTIONS:
        data["custom_fields"] = {PRIORITY_FIELD_GID: PRIORITY_OPTIONS[priority_key]}
    r = await client.post(
        f"{ASANA_API}/tasks", json={"data": data}, headers=asana_headers(), timeout=30
    )
    r.raise_for_status()
    return r.json()["data"]


async def move_task_to_section(client, task_gid, section_gid):
    if not section_gid:
        return
    try:
        await client.post(
            f"{ASANA_API}/sections/{section_gid}/addTask",
            json={"data": {"task": task_gid}},
            headers=asana_headers(),
            timeout=30,
        )
    except Exception as e:
        log.warning("Не удалось переместить в секцию: %s", e)


async def attach_file(client, task_gid, filepath, filename):
    with open(filepath, "rb") as f:
        files = {"file": (filename, f)}
        r = await client.post(
            f"{ASANA_API}/tasks/{task_gid}/attachments",
            data={"parent": task_gid},
            files=files,
            headers=asana_headers(),
            timeout=120,
        )
    r.raise_for_status()


async def list_section_tasks(client, section_gid):
    """Незавершённые задачи секции с нужными полями."""
    params = {
        "opt_fields": "name,due_on,completed,notes,permalink_url",
        "completed_since": "now",  # только незавершённые
        "limit": 100,
    }
    r = await client.get(
        f"{ASANA_API}/sections/{section_gid}/tasks",
        params=params, headers=asana_headers(), timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]


async def complete_task(client, task_gid):
    await client.put(
        f"{ASANA_API}/tasks/{task_gid}",
        json={"data": {"completed": True}},
        headers=asana_headers(), timeout=30,
    )


async def list_attachments(client, task_gid):
    """Список вложений задачи с временными download_url."""
    params = {"opt_fields": "name,download_url,resource_subtype,host"}
    r = await client.get(
        f"{ASANA_API}/tasks/{task_gid}/attachments",
        params=params, headers=asana_headers(), timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]


# ─────────────────────────────────────────────────────────────
#  Разбор ссылок: сайт vs пост
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
    # уникализируем
    seen, uniq = set(), []
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    if not full:
        return "Событие", "", uniq
    body_text = URL_RE.sub("", full).strip()
    lines = body_text.split("\n")
    title, rest, taken = "Событие", [], False
    for l in lines:
        if not taken and l.strip():
            title = l.strip()[:120]
            taken = True
        elif taken:
            rest.append(l)
    return title, "\n".join(rest).strip(), uniq


def build_notes(body, links, priority_key, due_label, sender):
    site, post, other = classify_links(links)
    head = []
    if site:
        head.append("🌐 Сайт: " + site)
    if post:
        head.append("📸 Пост: " + post)
    for u in other:
        head.append("🔗 Ссылка: " + u)
    if due_label:
        head.append("📅 Дата события: " + due_label)
    if priority_key:
        head.append("🚩 Приоритет: " + PRIORITY_LABELS[priority_key])

    parts = []
    if head:
        parts.append("\n".join(head))
        parts.append("──────────")
    if body:
        parts.append(body)
    parts.append(f"\n— Создано через бота, от {sender}")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
#  Клавиатуры
# ─────────────────────────────────────────────────────────────
def collect_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Собрать событие", callback_data="collect")],
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
    # ближайшие 7 дней + неделя/2 недели
    labels = []
    for i in range(0, 7):
        d = today + timedelta(days=i)
        name = {0: "Сегодня", 1: "Завтра"}.get(i, d.strftime("%d.%m"))
        labels.append((name, d.isoformat()))
    labels.append(("+2 недели", (today + timedelta(days=14)).isoformat()))
    labels.append(("+месяц", (today + timedelta(days=30)).isoformat()))
    for name, iso in labels:
        row.append(InlineKeyboardButton(name, callback_data=f"d|{iso}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✍️ Ввести дату вручную", callback_data="d|manual")])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────
#  Панель-черновик
# ─────────────────────────────────────────────────────────────
def draft_summary(draft):
    title, body, links = assemble(draft["texts"])
    site, post, other = classify_links(links)
    n_files = len(draft["files"])
    lines = [f"📝 Черновик события: «{title}»"]
    extras = []
    if body:
        extras.append("текст ✚")
    if site:
        extras.append("сайт ✓")
    if post:
        extras.append("пост ✓")
    if other:
        extras.append(f"ещё ссылок: {len(other)}")
    if n_files:
        extras.append(f"вложений: {n_files}")
    if extras:
        lines.append("Собрано: " + ", ".join(extras))
    lines.append("\nКидай ещё или жми «Собрать событие».")
    return "\n".join(lines)


async def refresh_panel(context, chat_id):
    draft = drafts.get(chat_id)
    if not draft:
        return
    text = draft_summary(draft)
    if draft.get("panel_msg_id"):
        try:
            await context.bot.edit_message_text(
                text, chat_id, draft["panel_msg_id"], reply_markup=collect_keyboard()
            )
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
#  Финальное создание события
# ─────────────────────────────────────────────────────────────
async def finalize_event(context, chat_id):
    draft = drafts.get(chat_id)
    if not draft:
        return
    bot = context.bot
    priority_key = draft.get("priority")
    due_iso = draft.get("due_iso")
    due_label = None
    if due_iso:
        due_label = date.fromisoformat(due_iso).strftime("%d.%m.%Y")

    title, body, links = assemble(draft["texts"])
    notes = build_notes(body, links, priority_key, due_label, draft["sender"])
    panel_id = draft["panel_msg_id"]

    async with httpx.AsyncClient() as client:
        try:
            task = await create_event_task(client, title, notes, priority_key, due_iso)
            task_gid = task["gid"]
            await move_task_to_section(client, task_gid, ASANA_EVENTS_SECTION_GID)
        except Exception as e:
            log.exception("Ошибка создания события")
            await bot.edit_message_text(f"❌ Не смог создать событие:\n{e}", chat_id, panel_id)
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
                os.unlink(tmp_path)
                attached += 1
            except Exception as e:
                log.warning("Не прикрепил файл %s: %s", filename, e)

    permalink = task.get("permalink_url", "")
    out = [f"✅ Событие создано: {title}"]
    if due_label:
        out.append(f"📅 {due_label}")
    if priority_key:
        out.append(f"🚩 {PRIORITY_LABELS[priority_key]}")
    if attached:
        out.append(f"📎 Вложений: {attached}")
    if permalink:
        out.append(permalink)
    await bot.edit_message_text("\n".join(out), chat_id, panel_id)
    drafts.pop(chat_id, None)


# ─────────────────────────────────────────────────────────────
#  Ежедневный дайджест + автоархив
# ─────────────────────────────────────────────────────────────
def smm_chat_id():
    if SMM_CHAT_ID:
        return int(SMM_CHAT_ID)
    return runtime_smm_chat_id


async def build_and_send_digest(context: ContextTypes.DEFAULT_TYPE, target_chat=None):
    chat = target_chat or smm_chat_id()
    if not chat:
        log.warning("Дайджест: не задан chat_id СММщицы (SMM_CHAT_ID или /start).")
        return "no_recipient"
    if not ASANA_EVENTS_SECTION_GID:
        log.warning("Дайджест: не задан ASANA_EVENTS_SECTION_GID.")
        if target_chat:
            await context.bot.send_message(chat, "⚠️ Не настроена секция событий (ASANA_EVENTS_SECTION_GID).")
        return "no_section"

    today = date.today()
    async with httpx.AsyncClient() as client:
        try:
            tasks = await list_section_tasks(client, ASANA_EVENTS_SECTION_GID)
        except Exception as e:
            log.exception("Дайджест: ошибка чтения секции")
            if target_chat:
                await context.bot.send_message(chat, f"⚠️ Ошибка чтения Асаны: {e}")
            return "error"

        active, archived = [], 0
        for t in tasks:
            due = t.get("due_on")
            if due:
                d = date.fromisoformat(due)
                if d < today:
                    # прошло — архивируем (помечаем выполненным)
                    try:
                        await complete_task(client, t["gid"])
                        archived += 1
                    except Exception as e:
                        log.warning("Не заархивировал %s: %s", t["gid"], e)
                    continue
            active.append(t)

    # сортируем по дате (без даты — в конец)
    active.sort(key=lambda t: t.get("due_on") or "9999-12-31")

    if not active:
        text = "📭 На сегодня активных событий для сторис нет."
        await context.bot.send_message(chat, text)
        return "empty"

    # Заголовок отдельным сообщением
    header = f"📅 События для сторис на {today.strftime('%d.%m.%Y')} — {len(active)} шт."
    await context.bot.send_message(chat, header)

    # Каждое событие — отдельным сообщением со ссылкой на Асану и кнопкой файлов
    for t in active:
        due = t.get("due_on")
        when = date.fromisoformat(due).strftime("%d.%m.%Y") if due else "дата не указана"
        block = [f"📌 {t['name']}", f"📅 {when}"]
        site, post, _ = classify_links(URL_RE.findall(t.get("notes") or ""))
        if site:
            block.append(f"🌐 Сайт: {site}")
        if post:
            block.append(f"📸 Пост: {post}")
        permalink = t.get("permalink_url")
        if permalink:
            block.append(f"🗂 Асана: {permalink}")

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📎 Прислать файлы", callback_data=f"files|{t['gid']}")
        ]])
        await context.bot.send_message(
            chat, "\n".join(block), reply_markup=kb,
            disable_web_page_preview=True,
        )

    if archived:
        await context.bot.send_message(chat, f"🗂 Заархивировано прошедших: {archived}")
    return "ok"


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE):
    await build_and_send_digest(context)


# Telegram Bot API лимиты: фото ~10 МБ, документ/видео ~50 МБ
TG_PHOTO_LIMIT = 10 * 1024 * 1024
TG_FILE_LIMIT = 50 * 1024 * 1024
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
VID_EXT = (".mp4", ".mov", ".m4v")


async def send_event_files(context, chat_id, task_gid, query=None):
    """Качает вложения задачи из Асаны и шлёт их в чат."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            atts = await list_attachments(client, task_gid)
        except Exception as e:
            log.exception("Файлы: ошибка чтения вложений")
            await context.bot.send_message(chat_id, f"⚠️ Не смог получить вложения: {e}")
            return

        if not atts:
            await context.bot.send_message(chat_id, "📭 У этого события нет прикреплённых файлов.")
            return

        await context.bot.send_message(chat_id, f"📎 Отправляю файлы ({len(atts)} шт.)…")
        sent, skipped = 0, []
        for a in atts:
            name = a.get("name") or "file"
            url = a.get("download_url")
            if not url:
                # Вложение-ссылка (например, на внешний сервис) — шлём ссылкой
                skipped.append(name)
                continue
            try:
                resp = await client.get(url, timeout=120)
                resp.raise_for_status()
                content = resp.content
                size = len(content)
                low = name.lower()

                if low.endswith(IMG_EXT) and size <= TG_PHOTO_LIMIT:
                    await context.bot.send_photo(chat_id, photo=content, caption=name)
                elif low.endswith(VID_EXT) and size <= TG_FILE_LIMIT:
                    await context.bot.send_video(chat_id, video=content, caption=name)
                elif size <= TG_FILE_LIMIT:
                    await context.bot.send_document(chat_id, document=content, filename=name)
                else:
                    skipped.append(f"{name} (слишком большой)")
                    continue
                sent += 1
            except Exception as e:
                log.warning("Не отправил вложение %s: %s", name, e)
                skipped.append(name)

        if sent:
            await context.bot.send_message(chat_id, f"✅ Отправлено: {sent}")
        if skipped:
            await context.bot.send_message(
                chat_id,
                "⚠️ Не удалось отправить (открой в Асане):\n" + "\n".join(f"• {s}" for s in skipped),
            )


# ─────────────────────────────────────────────────────────────
#  Хендлеры
# ─────────────────────────────────────────────────────────────
def is_allowed(user_id: int | None) -> bool:
    """True, если пользователю можно пользоваться ботом."""
    if not ALLOWED_USERS:
        # Список не настроен — открытый режим (только для первичной настройки).
        return True
    return user_id is not None and user_id in ALLOWED_USERS


async def notify_admin_request(context, user, chat_id, text_preview):
    """Шлёт админу карточку с данными чужого пользователя (один раз на id)."""
    if ADMIN_ID is None or user is None:
        return
    if user.id in _notified_strangers:
        return
    _notified_strangers.add(user.id)

    uname = f"@{user.username}" if user.username else "—"
    name = user.full_name or "—"
    preview = (text_preview or "").strip()
    if len(preview) > 200:
        preview = preview[:200] + "…"
    msg = (
        "🔔 Запрос доступа к боту\n\n"
        f"👤 Имя: {name}\n"
        f"🔗 Username: {uname}\n"
        f"🆔 ID: `{user.id}`\n"
        f"💬 chat_id: `{chat_id}`\n"
    )
    if preview:
        msg += f"\n📝 Написал(а): {preview}\n"
    msg += (
        "\nЧтобы дать доступ — добавь этот ID в Railway → ALLOWED_USERS "
        "(через запятую к остальным) и сохрани."
    )
    try:
        await context.bot.send_message(ADMIN_ID, msg, parse_mode="Markdown")
    except Exception as e:
        log.warning("Не смог уведомить админа: %s", e)


def restricted(handler):
    """Декоратор: пропускает только пользователей из ALLOWED_USERS.
    Чужому отвечает его ID и уведомляет админа для одобрения."""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        uid = user.id if user else None
        if not is_allowed(uid):
            log.info("Отклонён доступ: user_id=%s username=%s",
                     uid, getattr(user, "username", None))
            chat_id = update.effective_chat.id if update.effective_chat else None
            text_preview = update.message.text if (update.message and update.message.text) else ""

            # Уведомляем админа (один раз на id)
            await notify_admin_request(context, user, chat_id, text_preview)

            # Отвечаем самому пользователю его ID
            reply = (
                "🔒 Доступ к этому боту ограничен.\n\n"
                f"Твой ID: `{uid}`\n"
                "Передай его администратору, чтобы он выдал доступ."
            )
            if update.callback_query:
                try:
                    await update.callback_query.answer(
                        "Нет доступа. Запрос отправлен администратору.", show_alert=True
                    )
                except Exception:
                    pass
            elif update.message:
                try:
                    await update.message.reply_text(reply, parse_mode="Markdown")
                except Exception:
                    pass
            return
        return await handler(update, context)
    return wrapper


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else "?"
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "Привет! Кидай материал для события — можно несколькими сообщениями подряд "
        "(текст, афиша, ссылки на сайт и пост, в т.ч. пересланные).\n\n"
        "Бот копит всё в один черновик. Когда всё скинул — жми «✅ Собрать событие», "
        "выбери приоритет и дату.\n\n"
        "• Первая строка текста — название события\n"
        "• Ссылку ticketon → сайт, instagram → пост (бот сам разложит)\n"
        "• /digest — прислать дайджест прямо сейчас\n"
        "• /setsmm — назначить этот чат получателем ежедневного дайджеста\n"
        "• /cancel — сбросить черновик\n\n"
        f"Твой Telegram ID / chat_id: {uid} / {chat_id}"
    )


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if drafts.pop(update.effective_chat.id, None):
        await update.message.reply_text("🗑 Черновик сброшен.")
    else:
        await update.message.reply_text("Нет активного черновика.")


@restricted
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await build_and_send_digest(context, target_chat=update.effective_chat.id)


@restricted
async def cmd_setsmm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назначить текущий чат получателем дайджеста."""
    global runtime_smm_chat_id
    runtime_smm_chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"✅ Этот чат назначен получателем дайджеста. chat_id: {runtime_smm_chat_id}\n"
        "Впиши его в Railway → SMM_CHAT_ID, чтобы сохранилось после рестарта."
    )


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


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    sender = update.effective_user.full_name if update.effective_user else "—"

    draft = drafts.get(chat_id)
    if not draft:
        draft = {
            "texts": [], "files": [], "sender": sender,
            "panel_msg_id": None, "priority": None,
            "due_iso": None, "debounce_task": None,
            "stage": "collecting",
        }
        drafts[chat_id] = draft

    # Ручной ввод даты ожидается отдельно
    if draft.get("stage") == "await_manual_date":
        text = (msg.text or "").strip()
        iso = parse_manual_date(text)
        if not iso:
            await msg.reply_text("Не понял дату. Формат: ДД.ММ.ГГГГ (напр. 25.08.2026).")
            return
        draft["due_iso"] = iso
        await msg.reply_text("⏳ Создаю событие…")
        await finalize_event(context, chat_id)
        return

    if draft.get("stage") != "collecting":
        await msg.reply_text(
            "⏳ Это событие уже собирается (выбери приоритет/дату выше). "
            "Для нового заверши текущее или /cancel."
        )
        return

    text = msg.text or msg.caption
    if text:
        draft["texts"].append(text)
    file_tuple = file_from_message(msg)
    if file_tuple:
        draft["files"].append(file_tuple)

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    await schedule_refresh(context, chat_id)


def parse_manual_date(text):
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


@restricted
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    # Кнопка дайджеста «Прислать файлы» — не связана с черновиком
    if data.startswith("files|"):
        task_gid = data.split("|", 1)[1]
        await send_event_files(context, chat_id, task_gid, query)
        return

    draft = drafts.get(chat_id)

    if not draft:
        await query.edit_message_text("⌛ Черновик устарел. Кидай материал заново.")
        return

    if data == "cancel":
        drafts.pop(chat_id, None)
        await query.edit_message_text("🗑 Черновик отменён.")
        return

    if data == "collect":
        if not draft["texts"] and not draft["files"]:
            await query.answer("Черновик пуст", show_alert=True)
            return
        draft["stage"] = "priority"
        title, _, _ = assemble(draft["texts"])
        await query.edit_message_text(
            f"📝 «{title}»\n\nВыбери приоритет:", reply_markup=priority_keyboard()
        )
        return

    if data.startswith("p|"):
        draft["priority"] = data.split("|")[1]
        draft["stage"] = "date"
        title, _, _ = assemble(draft["texts"])
        await query.edit_message_text(
            f"📝 «{title}»\n🚩 {PRIORITY_LABELS[draft['priority']]}\n\nДата события:",
            reply_markup=date_keyboard(),
        )
        return

    if data.startswith("d|"):
        val = data.split("|", 1)[1]
        if val == "manual":
            draft["stage"] = "await_manual_date"
            await query.edit_message_text(
                "✍️ Напиши дату события сообщением в формате ДД.ММ.ГГГГ (напр. 25.08.2026)."
            )
            return
        draft["due_iso"] = val
        title, _, _ = assemble(draft["texts"])
        await query.edit_message_text(f"📝 «{title}»\n⏳ Создаю событие…")
        await finalize_event(context, chat_id)
        return


async def on_startup(app: Application):
    # Ежедневный дайджест в 10:00 по Бишкеку
    app.job_queue.run_daily(
        daily_digest_job,
        time=time(hour=10, minute=0, tzinfo=TZ),
        name="daily_digest",
    )
    log.info("Дайджест запланирован на 10:00 Asia/Bishkek")


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("setsmm", cmd_setsmm))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL
             | filters.ANIMATION) & ~filters.COMMAND,
            handle_message,
        )
    )
    log.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
