import os
import re
import logging
import asyncio
import tempfile
from datetime import date, timedelta

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
#  Настройки (берутся из переменных окружения на Railway)
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ASANA_TOKEN = os.environ["ASANA_TOKEN"]

ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "1215525237226401")
ASANA_SECTION_GID = os.environ.get("ASANA_SECTION_GID", "1215525237226403")
ASANA_ASSIGNEE_GID = os.environ.get("ASANA_ASSIGNEE_GID", "1213398188813384")
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "1208507351529750")

# Кастомное поле "Приоритет" и GID его опций
PRIORITY_FIELD_GID = "1215525237226419"
PRIORITY_OPTIONS = {
    "high": "1215525237226420",    # Высокий
    "medium": "1215525237226421",  # Средний
    "low": "1215525237226422",     # Низкий
}
PRIORITY_LABELS = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}

# Кто имеет право слать задачи (Telegram user id через запятую). Пусто = все.
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
}

ASANA_API = "https://app.asana.com/api/1.0"
URL_RE = re.compile(r"https?://[^\s]+")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("smm-bot")

# Черновики задач, ожидающие выбора приоритета/срока (draft_id -> данные)
drafts: dict[str, dict] = {}
# Сборка альбомов (media_group_id -> данные)
media_groups: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────
#  Хелперы Asana
# ─────────────────────────────────────────────────────────────
def asana_headers():
    return {"Authorization": f"Bearer {ASANA_TOKEN}"}


async def create_asana_task(client, name, notes, priority_key, due_on):
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


async def move_task_to_section(client, task_gid):
    try:
        await client.post(
            f"{ASANA_API}/sections/{ASANA_SECTION_GID}/addTask",
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


# ─────────────────────────────────────────────────────────────
#  Разбор сообщения и сборка описания
# ─────────────────────────────────────────────────────────────
def parse_message(text):
    """(название, тело_без_ссылок, список_ссылок)."""
    text = (text or "").strip()
    links = URL_RE.findall(text)
    if not text:
        return "Задача из Telegram", "", links

    # Убираем ссылки из тела, чтобы не дублировались (они уйдут в блок сверху)
    body_text = URL_RE.sub("", text).strip()
    lines = body_text.split("\n")
    title = "Задача из Telegram"
    rest_lines = []
    title_taken = False
    for l in lines:
        if not title_taken and l.strip():
            title = l.strip()[:120]
            title_taken = True
        elif title_taken:
            rest_lines.append(l)
    body = "\n".join(rest_lines).strip()
    return title, body, links


def build_notes(body, links, priority_key, due_label, sender):
    """Описание с блоком-шапкой сверху."""
    head = []
    if links:
        head.append("🔗 Ссылка: " + "  ".join(links))
    if priority_key:
        head.append("🚩 Приоритет: " + PRIORITY_LABELS[priority_key])
    if due_label:
        head.append("📅 Срок: " + due_label)

    parts = []
    if head:
        parts.append("\n".join(head))
        parts.append("──────────")
    if body:
        parts.append(body)
    parts.append(f"\n— Создано через бота, от {sender}")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
#  Кнопки выбора приоритета / срока
# ─────────────────────────────────────────────────────────────
def priority_keyboard(draft_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Высокий", callback_data=f"p|{draft_id}|high"),
            InlineKeyboardButton("🟡 Средний", callback_data=f"p|{draft_id}|medium"),
            InlineKeyboardButton("🟢 Низкий", callback_data=f"p|{draft_id}|low"),
        ],
    ])


def due_keyboard(draft_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Сегодня", callback_data=f"d|{draft_id}|today"),
            InlineKeyboardButton("Завтра", callback_data=f"d|{draft_id}|tomorrow"),
        ],
        [
            InlineKeyboardButton("Через неделю", callback_data=f"d|{draft_id}|week"),
            InlineKeyboardButton("Без срока", callback_data=f"d|{draft_id}|none"),
        ],
    ])


def resolve_due(key):
    """-> (date_iso | None, человекочитаемый_лейбл | None)."""
    today = date.today()
    if key == "today":
        d = today
    elif key == "tomorrow":
        d = today + timedelta(days=1)
    elif key == "week":
        d = today + timedelta(days=7)
    else:
        return None, None
    return d.isoformat(), d.strftime("%d.%m.%Y")


# ─────────────────────────────────────────────────────────────
#  Финальное создание задачи (после выбора кнопок)
# ─────────────────────────────────────────────────────────────
async def finalize_task(context, chat_id, status_msg_id, draft):
    bot = context.bot
    priority_key = draft.get("priority")
    due_key = draft.get("due")
    due_iso, due_label = resolve_due(due_key) if due_key else (None, None)

    title, body, links = parse_message(draft["caption"])
    notes = build_notes(body, links, priority_key, due_label, draft["sender"])

    async with httpx.AsyncClient() as client:
        try:
            task = await create_asana_task(client, title, notes, priority_key, due_iso)
            task_gid = task["gid"]
            await move_task_to_section(client, task_gid)
        except Exception as e:
            log.exception("Ошибка создания задачи")
            await bot.edit_message_text(
                f"❌ Не смог создать задачу:\n{e}", chat_id, status_msg_id
            )
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
    lines = [f"✅ Задача создана: {title}"]
    if priority_key:
        lines.append(f"🚩 {PRIORITY_LABELS[priority_key]}")
    if due_label:
        lines.append(f"📅 {due_label}")
    if attached:
        lines.append(f"📎 Вложений: {attached}")
    if permalink:
        lines.append(permalink)
    await bot.edit_message_text("\n".join(lines), chat_id, status_msg_id)


# ─────────────────────────────────────────────────────────────
#  Создание черновика и показ кнопок приоритета
# ─────────────────────────────────────────────────────────────
async def start_draft(context, chat_id, sender, caption, files):
    draft_id = f"{chat_id}_{int(asyncio.get_event_loop().time()*1000)}"
    drafts[draft_id] = {
        "chat_id": chat_id,
        "sender": sender,
        "caption": caption,
        "files": files,
        "priority": None,
        "due": None,
    }
    title, _, _ = parse_message(caption)
    msg = await context.bot.send_message(
        chat_id,
        f"📝 «{title}»\n\nВыбери приоритет:",
        reply_markup=priority_keyboard(draft_id),
    )
    drafts[draft_id]["status_msg_id"] = msg.message_id


# ─────────────────────────────────────────────────────────────
#  Сборка альбома (media group) с задержкой
# ─────────────────────────────────────────────────────────────
async def flush_media_group(context, group_id):
    await asyncio.sleep(2)
    data = media_groups.pop(group_id, None)
    if not data:
        return
    await start_draft(
        context, data["chat_id"], data["sender"], data["caption"], data["files"]
    )


# ─────────────────────────────────────────────────────────────
#  Хендлеры
# ─────────────────────────────────────────────────────────────
def authorized(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return update.effective_user and update.effective_user.id in ALLOWED_USERS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else "?"
    await update.message.reply_text(
        "Привет! Кидай сюда задачу для СММ-щика.\n\n"
        "• Первая строка — название задачи\n"
        "• Остальное — описание\n"
        "• Ссылку добавлю отдельным блоком сверху\n"
        "• Можно приложить фото/видео (в т.ч. альбомом)\n\n"
        "После отправки выберешь приоритет и срок кнопками.\n\n"
        f"Твой Telegram ID: {uid}"
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    msg = update.message
    sender = update.effective_user.full_name if update.effective_user else "—"
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    file_tuple = file_from_message(msg)

    if msg.media_group_id:
        gid = msg.media_group_id
        grp = media_groups.get(gid)
        if not grp:
            grp = {"chat_id": msg.chat_id, "sender": sender,
                   "caption": msg.caption or "", "files": []}
            media_groups[gid] = grp
            context.application.create_task(flush_media_group(context, gid))
        if msg.caption:
            grp["caption"] = msg.caption
        if file_tuple:
            grp["files"].append(file_tuple)
        return

    caption = msg.text or msg.caption or ""
    files = [file_tuple] if file_tuple else []
    await start_draft(context, msg.chat_id, sender, caption, files)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    kind, draft_id, value = parts[0], parts[1], parts[2]

    draft = drafts.get(draft_id)
    if not draft:
        await query.edit_message_text("⌛ Черновик устарел, отправь задачу заново.")
        return

    title, _, _ = parse_message(draft["caption"])

    if kind == "p":
        draft["priority"] = value
        await query.edit_message_text(
            f"📝 «{title}»\n🚩 Приоритет: {PRIORITY_LABELS[value]}\n\nТеперь выбери срок:",
            reply_markup=due_keyboard(draft_id),
        )
    elif kind == "d":
        draft["due"] = value
        await query.edit_message_text(f"📝 «{title}»\n⏳ Создаю задачу…")
        await finalize_task(context, draft["chat_id"], draft["status_msg_id"], draft)
        drafts.pop(draft_id, None)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
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
