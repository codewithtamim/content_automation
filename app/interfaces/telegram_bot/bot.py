"""Telegram bot interface with admin-only access control."""

import logging
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.application.use_cases.create_job import create_job, parse_urls
from app.infrastructure.database.repository import (
    GeminiKeyRepository,
    InstagramAccountRepository,
    SubAdminRepository,
    VideoJobRepository,
)
from app.infrastructure.database.session import get_db_session

logger = logging.getLogger(__name__)

# Conversation states
UPLOAD_URLS = 0
SCHEDULE_URLS, SCHEDULE_TIME = 1, 2
UPLOAD_PICK_ACCOUNT, SCHEDULE_PICK_ACCOUNT = 3, 4
ADD_ADMIN_USERNAME, REMOVE_ADMIN_USERNAME = 10, 11
ADD_GEMINI_KEY = 20
ADD_INSTA_USERNAME, ADD_INSTA_PASSWORD = 21, 22

# Callback data
CB_UPLOAD = "upload"
CB_SCHEDULE = "schedule"
CB_VIEW = "view"
CB_MANAGE_ADMINS = "manage_admins"
CB_ADD_ADMIN = "add_admin"
CB_REMOVE_ADMIN = "remove_admin"
CB_LIST_ADMINS = "list_admins"
CB_MANAGE_CREDS = "manage_creds"
CB_ADD_GEMINI = "add_gemini"
CB_ADD_INSTA = "add_insta"
CB_LIST_GEMINI = "list_gemini"
CB_LIST_INSTA = "list_insta"
CB_ACCOUNT_PREFIX = "acc_"
CB_REMOVE_GEMINI_PREFIX = "rm_gem_"
CB_REMOVE_INSTA_PREFIX = "rm_inst_"


def _get_sub_admin_usernames(context: ContextTypes.DEFAULT_TYPE) -> set[str]:
    """Load sub-admin usernames from DB."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = SubAdminRepository(session)
        return set(repo.list_all())


def is_main_admin(update: Update, admin_chat_id: str, admin_username: str) -> bool:
    """Check if the user is the main admin (from env)."""
    if not update.effective_user or not update.effective_chat:
        return False
    chat_ok = str(update.effective_chat.id) == admin_chat_id
    username_ok = (update.effective_user.username or "").lower() == admin_username.lower().lstrip("@")
    return chat_ok and username_ok


def is_admin(update: Update, admin_chat_id: str, admin_username: str, sub_admin_usernames: set[str]) -> bool:
    """Check if the user is main admin or a sub-admin."""
    if not update.effective_user:
        return False
    if is_main_admin(update, admin_chat_id, admin_username):
        return True
    user_username = (update.effective_user.username or "").lower()
    return user_username in sub_admin_usernames


def build_main_menu_keyboard(is_main_admin_flag: bool) -> InlineKeyboardMarkup:
    """Build the main menu keyboard. Main admin sees extra Manage admins button."""
    keyboard = [
        [InlineKeyboardButton("Upload videos", callback_data=CB_UPLOAD)],
        [InlineKeyboardButton("Schedule uploads", callback_data=CB_SCHEDULE)],
        [InlineKeyboardButton("View scheduled tasks", callback_data=CB_VIEW)],
    ]
    if is_main_admin_flag:
        keyboard.append([InlineKeyboardButton("Manage admins", callback_data=CB_MANAGE_ADMINS)])
        keyboard.append([InlineKeyboardButton("Manage credentials", callback_data=CB_MANAGE_CREDS)])
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start - show main menu."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return
    main_admin = is_main_admin(update, admin_chat_id, admin_username)
    await update.message.reply_text(
        "Welcome! Choose an action:",
        reply_markup=build_main_menu_keyboard(main_admin),
    )


def _build_manage_admins_keyboard() -> InlineKeyboardMarkup:
    """Build the manage admins sub-menu."""
    keyboard = [
        [InlineKeyboardButton("Add sub-admin", callback_data=CB_ADD_ADMIN)],
        [InlineKeyboardButton("Remove sub-admin", callback_data=CB_REMOVE_ADMIN)],
        [InlineKeyboardButton("List sub-admins", callback_data=CB_LIST_ADMINS)],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_manage_creds_keyboard() -> InlineKeyboardMarkup:
    """Build the manage credentials sub-menu."""
    keyboard = [
        [InlineKeyboardButton("Add Gemini key", callback_data=CB_ADD_GEMINI)],
        [InlineKeyboardButton("Add Instagram account", callback_data=CB_ADD_INSTA)],
        [InlineKeyboardButton("List Gemini keys", callback_data=CB_LIST_GEMINI)],
        [InlineKeyboardButton("List Instagram accounts", callback_data=CB_LIST_INSTA)],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_account_picker_keyboard(accounts: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Build inline keyboard for picking Instagram account."""
    keyboard = [
        [InlineKeyboardButton(f"@{username}", callback_data=f"{CB_ACCOUNT_PREFIX}{acc_id}")]
        for acc_id, username in accounts
    ]
    return InlineKeyboardMarkup(keyboard)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Handle inline keyboard callbacks."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END

    query = update.callback_query
    await query.answer()

    main_admin = is_main_admin(update, admin_chat_id, admin_username)
    data = query.data

    if data == CB_MANAGE_CREDS:
        if not main_admin:
            return ConversationHandler.END
        await query.edit_message_text(
            "Manage credentials:", reply_markup=_build_manage_creds_keyboard()
        )
        return ConversationHandler.END
    elif data == CB_ADD_GEMINI:
        if not main_admin:
            return ConversationHandler.END
        await query.edit_message_text("Send your Gemini API key:")
        return ADD_GEMINI_KEY
    elif data == CB_ADD_INSTA:
        if not main_admin:
            return ConversationHandler.END
        await query.edit_message_text("Send Instagram username:")
        return ADD_INSTA_USERNAME
    elif data == CB_LIST_GEMINI:
        if not main_admin:
            return ConversationHandler.END
        await _show_gemini_keys(query, context)
        return ConversationHandler.END
    elif data == CB_LIST_INSTA:
        if not main_admin:
            return ConversationHandler.END
        await _show_instagram_accounts(query, context)
        return ConversationHandler.END
    elif data and data.startswith(CB_REMOVE_GEMINI_PREFIX):
        if not main_admin:
            return ConversationHandler.END
        try:
            key_id = int(data[len(CB_REMOVE_GEMINI_PREFIX) :])
            SessionLocal = context.bot_data["SessionLocal"]
            with get_db_session(SessionLocal) as session:
                repo = GeminiKeyRepository(session)
                if repo.remove(key_id):
                    await query.edit_message_text(f"Removed Gemini key {key_id}.")
                else:
                    await query.edit_message_text("Key not found.")
        except ValueError:
            await query.edit_message_text("Invalid key ID.")
        return ConversationHandler.END
    elif data and data.startswith(CB_REMOVE_INSTA_PREFIX):
        if not main_admin:
            return ConversationHandler.END
        try:
            acc_id = int(data[len(CB_REMOVE_INSTA_PREFIX) :])
            SessionLocal = context.bot_data["SessionLocal"]
            with get_db_session(SessionLocal) as session:
                repo = InstagramAccountRepository(session)
                if repo.remove(acc_id):
                    await query.edit_message_text(f"Removed Instagram account {acc_id}.")
                else:
                    await query.edit_message_text("Account not found.")
        except ValueError:
            await query.edit_message_text("Invalid account ID.")
        return ConversationHandler.END

    if data == CB_MANAGE_ADMINS:
        if not main_admin:
            return ConversationHandler.END
        await query.edit_message_text("Manage admins:", reply_markup=_build_manage_admins_keyboard())
        return ConversationHandler.END
    elif data == CB_ADD_ADMIN:
        if not main_admin:
            return ConversationHandler.END
        await query.edit_message_text("Send the username to add (without @):")
        return ADD_ADMIN_USERNAME
    elif data == CB_REMOVE_ADMIN:
        if not main_admin:
            return ConversationHandler.END
        await query.edit_message_text("Send the username to remove:")
        return REMOVE_ADMIN_USERNAME
    elif data == CB_LIST_ADMINS:
        if not main_admin:
            return ConversationHandler.END
        await _show_sub_admins(query, context)
        return ConversationHandler.END

    if data == CB_UPLOAD:
        await query.edit_message_text(
            "Send video URLs (comma or newline separated):"
        )
        context.user_data["action"] = "upload"
        return UPLOAD_URLS
    elif data == CB_SCHEDULE:
        await query.edit_message_text(
            "Send video URLs (comma or newline separated):"
        )
        context.user_data["action"] = "schedule"
        return SCHEDULE_URLS
    elif data == CB_VIEW:
        await _show_scheduled_tasks(query, context)
        return ConversationHandler.END

    return ConversationHandler.END


async def _show_sub_admins(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of sub-admins."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = SubAdminRepository(session)
        usernames = repo.list_all()
    if not usernames:
        await query.edit_message_text("No sub-admins.")
        return
    text = "Sub-admins:\n\n" + "\n".join(f"• @{u}" for u in usernames)
    await query.edit_message_text(text)


async def _show_gemini_keys(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of Gemini keys with remove buttons."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = GeminiKeyRepository(session)
        keys = repo.list_all_ordered()
    if not keys:
        await query.edit_message_text("No Gemini keys. Add one to get started.")
        return
    keyboard = [
        [InlineKeyboardButton(f"Key #{kid} - Remove", callback_data=f"{CB_REMOVE_GEMINI_PREFIX}{kid}")]
        for kid, _ in keys
    ]
    text = "Gemini keys (tried in order for failover):\n\n" + "\n".join(f"• Key #{kid}" for kid, _ in keys)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_instagram_accounts(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of Instagram accounts with remove buttons."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = InstagramAccountRepository(session)
        accounts = repo.list_all()
    if not accounts:
        await query.edit_message_text("No Instagram accounts. Add one to get started.")
        return
    keyboard = [
        [InlineKeyboardButton(f"@{username} - Remove", callback_data=f"{CB_REMOVE_INSTA_PREFIX}{acc_id}")]
        for acc_id, username in accounts
    ]
    text = "Instagram accounts:\n\n" + "\n".join(f"• @{username} (ID: {acc_id})" for acc_id, username in accounts)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_scheduled_tasks(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show pending/scheduled jobs to the user."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = VideoJobRepository(session)
        jobs = repo.get_all_pending_and_scheduled()

    if not jobs:
        await query.edit_message_text("No pending or scheduled tasks.")
        return

    lines = []
    for j in jobs[:20]:
        schedule_str = j.schedule_time.strftime("%Y-%m-%d %H:%M") if j.schedule_time else "ASAP"
        lines.append(f"• [{j.id}] {j.original_url[:50]}... @ {schedule_str}")
    text = "Scheduled tasks:\n\n" + "\n".join(lines)
    if len(jobs) > 20:
        text += f"\n\n... and {len(jobs) - 20} more"
    await query.edit_message_text(text)


async def add_gemini_key_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Gemini API key input."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END
    key = (update.message.text or "").strip()
    if not key:
        await update.message.reply_text("Key cannot be empty. Send your Gemini API key:")
        return ADD_GEMINI_KEY
    SessionLocal = context.bot_data["SessionLocal"]
    try:
        with get_db_session(SessionLocal) as session:
            repo = GeminiKeyRepository(session)
            keys = repo.list_all_ordered()
            priority = len(keys)
            repo.add(key, priority=priority)
        await update.message.reply_text("Added Gemini API key. It will be tried in failover order.")
    except Exception:
        await update.message.reply_text("Failed to add key.")
    return ConversationHandler.END


async def add_insta_username_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Instagram username - then ask for password."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END
    username = (update.message.text or "").strip()
    if not username:
        await update.message.reply_text("Username cannot be empty. Send Instagram username:")
        return ADD_INSTA_USERNAME
    context.user_data["insta_username"] = username
    await update.message.reply_text("Send Instagram password:")
    return ADD_INSTA_PASSWORD


async def add_insta_password_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Instagram password - store in DB."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END
    password = update.message.text or ""
    username = context.user_data.get("insta_username", "")
    if not username:
        await update.message.reply_text("Session expired. Start over from Manage credentials.")
        return ConversationHandler.END
    SessionLocal = context.bot_data["SessionLocal"]
    try:
        with get_db_session(SessionLocal) as session:
            repo = InstagramAccountRepository(session)
            repo.add(username, password)
        await update.message.reply_text(f"Added Instagram account @{username}.")
    except Exception:
        await update.message.reply_text("Failed to add (username may already exist).")
    context.user_data.pop("insta_username", None)
    return ConversationHandler.END


async def add_admin_username_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle username for adding sub-admin."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END
    username = (update.message.text or "").strip()
    if not username:
        await update.message.reply_text("Username cannot be empty. Send the username to add (without @):")
        return ADD_ADMIN_USERNAME
    SessionLocal = context.bot_data["SessionLocal"]
    try:
        with get_db_session(SessionLocal) as session:
            repo = SubAdminRepository(session)
            repo.add(username)
        await update.message.reply_text(f"Added @{username.lower().lstrip('@')} as sub-admin.")
    except ValueError as e:
        await update.message.reply_text(str(e))
    except Exception:
        await update.message.reply_text("Failed to add (username may already exist).")
    return ConversationHandler.END


async def remove_admin_username_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle username for removing sub-admin."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END
    username = (update.message.text or "").strip()
    if not username:
        await update.message.reply_text("Username cannot be empty. Send the username to remove:")
        return REMOVE_ADMIN_USERNAME
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = SubAdminRepository(session)
        removed = repo.remove(username)
    if removed:
        await update.message.reply_text(f"Removed @{username.lower().lstrip('@')} from sub-admins.")
    else:
        await update.message.reply_text("Username not found in sub-admins.")
    return ConversationHandler.END


async def upload_urls_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle URLs for immediate upload - then show account picker."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END

    urls = parse_urls(update.message.text or "")
    if not urls:
        await update.message.reply_text("No valid URLs found. Please send valid video URLs.")
        return UPLOAD_URLS

    context.user_data["urls"] = urls
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = InstagramAccountRepository(session)
        accounts = repo.list_all()

    if not accounts:
        await update.message.reply_text(
            "No Instagram accounts configured. Add one in Manage credentials (main admin only)."
        )
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(
        "Pick Instagram account:",
        reply_markup=_build_account_picker_keyboard(accounts),
    )
    return UPLOAD_PICK_ACCOUNT


async def upload_account_picked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle account selection for immediate upload."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith(CB_ACCOUNT_PREFIX):
        return ConversationHandler.END
    acc_id = int(data[len(CB_ACCOUNT_PREFIX) :])
    urls = context.user_data.get("urls", [])
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = VideoJobRepository(session)
        job_ids = create_job(repo, urls, schedule_time=None, instagram_account_id=acc_id)
    await query.edit_message_text(
        f"Created {len(job_ids)} job(s) for Instagram. Jobs will be processed shortly.\nIDs: {job_ids}"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def schedule_urls_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle URLs for scheduled upload - show account picker."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END

    urls = parse_urls(update.message.text or "")
    if not urls:
        await update.message.reply_text("No valid URLs found. Please send valid video URLs.")
        return SCHEDULE_URLS

    context.user_data["urls"] = urls
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = InstagramAccountRepository(session)
        accounts = repo.list_all()

    if not accounts:
        await update.message.reply_text(
            "No Instagram accounts configured. Add one in Manage credentials (main admin only)."
        )
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(
        "Pick Instagram account:",
        reply_markup=_build_account_picker_keyboard(accounts),
    )
    return SCHEDULE_PICK_ACCOUNT


async def schedule_account_picked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle account selection for schedule - then ask for time."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith(CB_ACCOUNT_PREFIX):
        return ConversationHandler.END
    acc_id = int(data[len(CB_ACCOUNT_PREFIX) :])
    context.user_data["instagram_account_id"] = acc_id
    await query.edit_message_text(
        "Send schedule time (e.g. 2025-03-08 14:00 or 2025-03-08 14:00:00):"
    )
    return SCHEDULE_TIME


async def schedule_time_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle schedule time and create jobs."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    try:
        if len(text) == 16:  # 2025-03-08 14:00
            schedule_time = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        else:
            schedule_time = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        await update.message.reply_text(
            "Invalid format. Use YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM:SS"
        )
        return SCHEDULE_TIME

    urls = context.user_data.get("urls", [])
    instagram_account_id = context.user_data.get("instagram_account_id")
    SessionLocal = context.bot_data["SessionLocal"]

    with get_db_session(SessionLocal) as session:
        repo = VideoJobRepository(session)
        job_ids = create_job(
            repo, urls, schedule_time=schedule_time, instagram_account_id=instagram_account_id
        )

    await update.message.reply_text(
        f"Scheduled {len(job_ids)} job(s) for Instagram at {schedule_time}.\nIDs: {job_ids}"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel current conversation."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def _error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle errors - log Conflict briefly, others with full traceback."""
    if context.error and isinstance(context.error, Conflict):
        logger.warning(
            "Telegram Conflict: another bot instance is polling. "
            "Stop other instances (local python, other containers) and restart."
        )
    else:
        logger.exception("Update %s caused error: %s", update, context.error)


def create_application(
    bot_token: str,
    admin_chat_id: str,
    admin_username: str,
    SessionLocal,
) -> Application:
    """Create and configure the Telegram bot application."""
    app = (
        Application.builder()
        .token(bot_token)
        .build()
    )
    app.bot_data["admin_chat_id"] = admin_chat_id
    app.bot_data["admin_username"] = admin_username
    app.bot_data["SessionLocal"] = SessionLocal

    # Conversation handler for upload, schedule, admin and credential management flows
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_handler)],
        states={
            UPLOAD_URLS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, upload_urls_received),
            ],
            UPLOAD_PICK_ACCOUNT: [
                CallbackQueryHandler(upload_account_picked),
            ],
            SCHEDULE_URLS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_urls_received),
            ],
            SCHEDULE_PICK_ACCOUNT: [
                CallbackQueryHandler(schedule_account_picked),
            ],
            SCHEDULE_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_time_received),
            ],
            ADD_ADMIN_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_username_received),
            ],
            REMOVE_ADMIN_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, remove_admin_username_received),
            ],
            ADD_GEMINI_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_gemini_key_received),
            ],
            ADD_INSTA_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_insta_username_received),
            ],
            ADD_INSTA_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_insta_password_received),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(conv_handler)
    app.add_error_handler(_error_handler)

    return app
