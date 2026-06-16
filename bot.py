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

# Активные черновики (chat_id -> данные). Один черновик на чат.
# Структура: {texts:[], files:[(file_id,filename)], sender, panel_msg_id,
#             priority, due, seen_media_groups:set, debounce_task}
drafts: dict[int, dict] = {}


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
#  Сборка названия / описания из накопленных текстов
# ─────────────────────────────────────────────────────────────
def assemble(texts):
    """Возвращает (название, тело_без_ссылок, список_ссылок) из всех сообщений."""
    full = "\n".join(t for t in texts if t and t.strip()).strip()
    links = URL_RE.findall(full)
    if not full:
        return "Задача из Telegram", "", links

    body_text = URL_RE.sub("", full).strip()
    lines = body_text.split("\n")
    title = "Задача из Telegram"
    rest = []
    title_taken = False
    for l in lines:
        if not title_taken and l.strip():
            title = l.strip()[:120]
            title_taken = True
        elif title_taken:
            rest.append(l)
    body = "\n".join(rest).strip()
    # уникализируем ссылки, сохраняя порядок
    seen = set()
    uniq_links = []
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq_links.append(u)
    return title, body, uniq_links


def build_notes(body, links, priority_key, due_label, sender):
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
#  Клавиатуры
# ─────────────────────────────────────────────────────────────
def collect_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Собрать задачу", callback_data="collect")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def priority_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Высокий", callback_data="p|high"),
            InlineKeyboardButton("🟡 Средний", callback_data="p|medium"),
            InlineKeyboardButton("🟢 Низкий", callback_data="p|low"),
        ],
    ])


def due_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Сегодня", callback_data="d|today"),
            InlineKeyboardButton("Завтра", callback_data="d|tomorrow"),
        ],
        [
            InlineKeyboardButton("Через неделю", callback_data="d|week"),
            InlineKeyboardButton("Без срока", callback_data="d|none"),
        ],
    ])


def resolve_due(key):
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
#  Панель-черновик: показать / обновить сводку накопленного
# ─────────────────────────────────────────────────────────────
def draft_summary(draft):
    title, body, links = assemble(draft["texts"])
    n_files = len(draft["files"])
    lines = [f"📝 Черновик: «{title}»"]
    extras = []
    if body:
        extras.append("текст ✚")
    if links:
        extras.append(f"ссылок: {len(links)}")
    if n_files:
        extras.append(f"вложений: {n_files}")
    if extras:
        lines.append("Собрано: " + ", ".join(extras))
    lines.append("\nКидай ещё или жми «Собрать задачу», когда всё.")
    return "\n".join(lines)


async def refresh_panel(context, chat_id):
    """Обновляет (или создаёт) сообщение-панель с кнопками."""
    draft = drafts.get(chat_id)
    if not draft:
        return
    text = draft_summary(draft)
    if draft.get("panel_msg_id"):
        try:
            await context.bot.edit_message_text(
                text, chat_id, draft["panel_msg_id"],
                reply_markup=collect_keyboard(),
            )
            return
        except Exception:
            pass  # сообщение могло устареть — пересоздадим ниже
    msg = await context.bot.send_message(
        chat_id, text, reply_markup=collect_keyboard()
    )
    draft["panel_msg_id"] = msg.message_id


# Дебаунс: при пачке сообщений (особенно альбомов) обновляем панель один раз.
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
#  Финальное создание задачи
# ─────────────────────────────────────────────────────────────
async def finalize_task(context, chat_id):
    draft = drafts.get(chat_id)
    if not draft:
        return
    bot = context.bot
    priority_key = draft.get("priority")
    due_iso, due_label = resolve_due(draft["due"]) if draft.get("due") else (None, None)

    title, body, links = assemble(draft["texts"])
    notes = build_notes(body, links, priority_key, due_label, draft["sender"])
    panel_id = draft["panel_msg_id"]

    async with httpx.AsyncClient() as client:
        try:
            task = await create_asana_task(client, title, notes, priority_key, due_iso)
            task_gid = task["gid"]
            await move_task_to_section(client, task_gid)
        except Exception as e:
            log.exception("Ошибка создания задачи")
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
                os.unlink(tmp_path)
                attached += 1
            except Exception as e:
                log.warning("Не прикрепил файл %s: %s", filename, e)

    permalink = task.get("permalink_url", "")
    out = [f"✅ Задача создана: {title}"]
    if priority_key:
        out.append(f"🚩 {PRIORITY_LABELS[priority_key]}")
    if due_label:
        out.append(f"📅 {due_label}")
    if attached:
        out.append(f"📎 Вложений: {attached}")
    if permalink:
        out.append(permalink)
    await bot.edit_message_text("\n".join(out), chat_id, panel_id)
    drafts.pop(chat_id, None)


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
        "Привет! Кидай материал для задачи — можно несколькими сообщениями подряд "
        "(текст, фото, видео, ссылки, в т.ч. пересланные).\n\n"
        "Бот копит всё в один черновик. Когда всё скинул — жми «✅ Собрать задачу», "
        "выбери приоритет и срок.\n\n"
        "• Первая строка текста — название задачи\n"
        "• Ссылки соберу отдельным блоком сверху\n"
        "• /cancel — сбросить текущий черновик\n\n"
        f"Твой Telegram ID: {uid}"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if drafts.pop(update.effective_chat.id, None):
        await update.message.reply_text("🗑 Черновик сброшен.")
    else:
        await update.message.reply_text("Нет активного черновика.")


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
    chat_id = msg.chat_id
    sender = update.effective_user.full_name if update.effective_user else "—"

    # Создаём черновик, если его ещё нет
    draft = drafts.get(chat_id)
    if not draft:
        draft = {
            "texts": [],
            "files": [],
            "sender": sender,
            "panel_msg_id": None,
            "priority": None,
            "due": None,
            "seen_media_groups": set(),
            "debounce_task": None,
            "stage": "collecting",
        }
        drafts[chat_id] = draft

    # Если черновик уже на этапе выбора приоритета/срока — не примешиваем новые
    if draft.get("stage") != "collecting":
        await msg.reply_text(
            "⏳ Этот черновик уже собирается (выбери приоритет/срок выше). "
            "Для новой задачи заверши текущую или /cancel."
        )
        return

    # Текст / подпись
    text = msg.text or msg.caption
    if text:
        draft["texts"].append(text)

    # Файл
    file_tuple = file_from_message(msg)
    if file_tuple:
        draft["files"].append(file_tuple)

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    await schedule_refresh(context, chat_id)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data
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
            await query.answer("Черновик пуст — кинь хоть что-то", show_alert=True)
            return
        draft["stage"] = "priority"
        title, _, _ = assemble(draft["texts"])
        await query.edit_message_text(
            f"📝 «{title}»\n\nВыбери приоритет:",
            reply_markup=priority_keyboard(),
        )
        return

    if data.startswith("p|"):
        draft["priority"] = data.split("|")[1]
        draft["stage"] = "due"
        title, _, _ = assemble(draft["texts"])
        await query.edit_message_text(
            f"📝 «{title}»\n🚩 Приоритет: {PRIORITY_LABELS[draft['priority']]}\n\nТеперь срок:",
            reply_markup=due_keyboard(),
        )
        return

    if data.startswith("d|"):
        draft["due"] = data.split("|")[1]
        title, _, _ = assemble(draft["texts"])
        await query.edit_message_text(f"📝 «{title}»\n⏳ Создаю задачу…")
        await finalize_task(context, chat_id)
        return


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
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
