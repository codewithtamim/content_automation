"""Telegram bot interface with admin-only access control."""

import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

BANGLADESH_TZ = ZoneInfo("Asia/Dhaka")

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
    ALL_PERMISSIONS,
    PERM_MANAGE_ADMINS,
    PERM_MANAGE_CREDS,
    PERM_SCHEDULE_UPLOADS,
    PERM_UPLOAD_VIDEOS,
    PERM_VIEW_SCHEDULED_TASKS,
    GeminiKeyRepository,
    InstagramAccountRepository,
    SubAdminRepository,
    VideoJobRepository,
)
from app.infrastructure.database.session import get_db_session

logger = logging.getLogger(__name__)


def _parse_schedule_time_bd(text: str) -> datetime | None:
    """
    Parse schedule time in Bangladesh time.
    Format: month day time am/pm (year = current year).
    Examples: "3 8 2:30 pm", "12-25 9:00 am", "3/8 14:30" (24h also ok).
    """
    text = text.strip().lower()
    # Match: month day time (am|pm) - month/day can be separated by space, - or /
    m = re.match(
        r"(\d{1,2})[-/\s]+(\d{1,2})\s+(\d{1,2}):(\d{2})\s*(am|pm)?",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    month, day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    ampm = (m.group(5) or "").lower()

    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= minute <= 59):
        return None

    if ampm:
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    if hour > 23:
        hour = 23

    year = datetime.now(BANGLADESH_TZ).year
    try:
        dt_bd = datetime(year, month, day, hour, minute, 0, tzinfo=BANGLADESH_TZ)
        return dt_bd.astimezone(timezone.utc)
    except ValueError:
        return None


# Conversation states
UPLOAD_URLS = 0
SCHEDULE_URLS, SCHEDULE_TIME = 1, 2
UPLOAD_PICK_ACCOUNT, SCHEDULE_PICK_ACCOUNT = 3, 4
ADD_ADMIN_USERNAME, ADD_ADMIN_PERMISSIONS, REMOVE_ADMIN_USERNAME = 10, 12, 11
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
CB_BACK = "back"
CB_PERM_FULL = "perm_full"
CB_PERM_UPLOAD = "perm_upload"
CB_PERM_SCHEDULE = "perm_schedule"
CB_PERM_VIEW = "perm_view"
CB_PERM_MANAGE_ADMINS = "perm_manage_admins"
CB_PERM_MANAGE_CREDS = "perm_manage_creds"
CB_PERM_DONE = "perm_done"


def _get_sub_admin_usernames(context: ContextTypes.DEFAULT_TYPE) -> set[str]:
    """Load sub-admin usernames from DB."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = SubAdminRepository(session)
        return {username for username, _ in repo.list_all()}


def _get_sub_admin_permissions(
    context: ContextTypes.DEFAULT_TYPE, username: str
) -> set[str] | None:
    """Get permissions for a sub-admin, or None if not a sub-admin."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = SubAdminRepository(session)
        perms = repo.get_permissions(username)
    return set(perms) if perms else None


def _user_has_permission(permissions: set[str] | None, permission: str) -> bool:
    """Check if user has permission. None = main admin (all perms)."""
    if permissions is None:
        return True
    return permission in permissions


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


def build_main_menu_keyboard(
    is_main_admin_flag: bool,
    sub_admin_permissions: set[str] | None = None,
) -> InlineKeyboardMarkup:
    """Build the main menu keyboard. Filter by permissions for sub-admins."""
    perms = None if is_main_admin_flag else sub_admin_permissions
    keyboard = []
    if _user_has_permission(perms, PERM_UPLOAD_VIDEOS):
        keyboard.append([InlineKeyboardButton("Upload videos", callback_data=CB_UPLOAD)])
    if _user_has_permission(perms, PERM_SCHEDULE_UPLOADS):
        keyboard.append([InlineKeyboardButton("Schedule uploads", callback_data=CB_SCHEDULE)])
    if _user_has_permission(perms, PERM_VIEW_SCHEDULED_TASKS):
        keyboard.append([InlineKeyboardButton("View scheduled tasks", callback_data=CB_VIEW)])
    if _user_has_permission(perms, PERM_MANAGE_ADMINS):
        keyboard.append([InlineKeyboardButton("Manage admins", callback_data=CB_MANAGE_ADMINS)])
    if _user_has_permission(perms, PERM_MANAGE_CREDS):
        keyboard.append([InlineKeyboardButton("Manage credentials", callback_data=CB_MANAGE_CREDS)])
    return InlineKeyboardMarkup(keyboard)


def _get_current_user_permissions(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[bool, set[str] | None]:
    """Return (is_main_admin, sub_admin_permissions). sub_admin_permissions is None for main admin."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if is_main_admin(update, admin_chat_id, admin_username):
        return True, None
    user_username = (update.effective_user.username or "").lower() if update.effective_user else ""
    perms = _get_sub_admin_permissions(context, user_username)
    return False, perms or set()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start - show main menu."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return
    main_admin, sub_perms = _get_current_user_permissions(update, context)
    await update.message.reply_text(
        "Hey boss! 👋 What would you like to do?",
        reply_markup=build_main_menu_keyboard(main_admin, sub_perms),
    )


def _build_manage_admins_keyboard() -> InlineKeyboardMarkup:
    """Build the manage admins sub-menu."""
    keyboard = [
        [InlineKeyboardButton("Add sub-admin", callback_data=CB_ADD_ADMIN)],
        [InlineKeyboardButton("Remove sub-admin", callback_data=CB_REMOVE_ADMIN)],
        [InlineKeyboardButton("List sub-admins", callback_data=CB_LIST_ADMINS)],
        [InlineKeyboardButton("← Back", callback_data=CB_BACK)],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_manage_creds_keyboard() -> InlineKeyboardMarkup:
    """Build the manage credentials sub-menu."""
    keyboard = [
        [InlineKeyboardButton("Add Gemini key", callback_data=CB_ADD_GEMINI)],
        [InlineKeyboardButton("Add Instagram account", callback_data=CB_ADD_INSTA)],
        [InlineKeyboardButton("List Gemini keys", callback_data=CB_LIST_GEMINI)],
        [InlineKeyboardButton("List Instagram accounts", callback_data=CB_LIST_INSTA)],
        [InlineKeyboardButton("← Back", callback_data=CB_BACK)],
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

    main_admin, sub_perms = _get_current_user_permissions(update, context)
    user_perms = None if main_admin else sub_perms
    data = query.data

    if data == CB_BACK:
        await query.edit_message_text(
            "Hey boss! 👋 What would you like to do?",
            reply_markup=build_main_menu_keyboard(main_admin, sub_perms),
        )
        return ConversationHandler.END

    if data == CB_MANAGE_CREDS:
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        await query.edit_message_text(
            "Manage credentials:", reply_markup=_build_manage_creds_keyboard()
        )
        return ConversationHandler.END
    elif data == CB_ADD_GEMINI:
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        await query.edit_message_text("Send your Gemini API key:")
        return ADD_GEMINI_KEY
    elif data == CB_ADD_INSTA:
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        await query.edit_message_text("Send Instagram username:")
        return ADD_INSTA_USERNAME
    elif data == CB_LIST_GEMINI:
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        await _show_gemini_keys(query, context)
        return ConversationHandler.END
    elif data == CB_LIST_INSTA:
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        await _show_instagram_accounts(query, context)
        return ConversationHandler.END
    elif data and data.startswith(CB_REMOVE_GEMINI_PREFIX):
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        try:
            key_id = int(data[len(CB_REMOVE_GEMINI_PREFIX) :])
            SessionLocal = context.bot_data["SessionLocal"]
            with get_db_session(SessionLocal) as session:
                repo = GeminiKeyRepository(session)
                back_mk = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]])
                if repo.remove(key_id):
                    await query.edit_message_text(f"Removed Gemini key {key_id}. ✓", reply_markup=back_mk)
                else:
                    await query.edit_message_text("That key wasn't found.", reply_markup=back_mk)
        except ValueError:
            await query.edit_message_text("Invalid key ID.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]]))
        return ConversationHandler.END
    elif data and data.startswith(CB_REMOVE_INSTA_PREFIX):
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        try:
            acc_id = int(data[len(CB_REMOVE_INSTA_PREFIX) :])
            SessionLocal = context.bot_data["SessionLocal"]
            with get_db_session(SessionLocal) as session:
                repo = InstagramAccountRepository(session)
                back_mk = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]])
                if repo.remove(acc_id):
                    await query.edit_message_text(f"Removed Instagram account {acc_id}. ✓", reply_markup=back_mk)
                else:
                    await query.edit_message_text("That account wasn't found.", reply_markup=back_mk)
        except ValueError:
            await query.edit_message_text(
                "Invalid account ID.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]]),
            )
        return ConversationHandler.END

    if data == CB_MANAGE_ADMINS:
        if not _user_has_permission(user_perms, PERM_MANAGE_ADMINS):
            return ConversationHandler.END
        await query.edit_message_text("Manage admins:", reply_markup=_build_manage_admins_keyboard())
        return ConversationHandler.END
    elif data == CB_ADD_ADMIN:
        if not _user_has_permission(user_perms, PERM_MANAGE_ADMINS):
            return ConversationHandler.END
        await query.edit_message_text("Send the username to add (without @):")
        return ADD_ADMIN_USERNAME
    elif data == CB_REMOVE_ADMIN:
        if not _user_has_permission(user_perms, PERM_MANAGE_ADMINS):
            return ConversationHandler.END
        await query.edit_message_text("Send the username to remove:")
        return REMOVE_ADMIN_USERNAME
    elif data == CB_LIST_ADMINS:
        if not _user_has_permission(user_perms, PERM_MANAGE_ADMINS):
            return ConversationHandler.END
        await _show_sub_admins(query, context)
        return ConversationHandler.END

    if data == CB_UPLOAD:
        if not _user_has_permission(user_perms, PERM_UPLOAD_VIDEOS):
            return ConversationHandler.END
        await query.edit_message_text(
            "Send me the video URLs (comma or newline separated):"
        )
        context.user_data["action"] = "upload"
        return UPLOAD_URLS
    elif data == CB_SCHEDULE:
        if not _user_has_permission(user_perms, PERM_SCHEDULE_UPLOADS):
            return ConversationHandler.END
        await query.edit_message_text(
            "Send me the video URLs (comma or newline separated):"
        )
        context.user_data["action"] = "schedule"
        return SCHEDULE_URLS
    elif data == CB_VIEW:
        if not _user_has_permission(user_perms, PERM_VIEW_SCHEDULED_TASKS):
            return ConversationHandler.END
        await _show_scheduled_tasks(query, context)
        return ConversationHandler.END

    return ConversationHandler.END


def _format_permissions_display(perms: list[str]) -> str:
    """Format permissions for display, e.g. 'Full access' or 'Upload, Schedule, View'."""
    if set(perms) >= set(ALL_PERMISSIONS):
        return "Full access"
    labels = {
        PERM_UPLOAD_VIDEOS: "Upload",
        PERM_SCHEDULE_UPLOADS: "Schedule",
        PERM_VIEW_SCHEDULED_TASKS: "View",
        PERM_MANAGE_ADMINS: "Manage admins",
        PERM_MANAGE_CREDS: "Manage creds",
    }
    return ", ".join(labels.get(p, p) for p in perms)


def _build_permission_picker_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    """Build keyboard for permission selection with checkmarks."""
    def btn(label: str, cb: str, is_on: bool) -> InlineKeyboardButton:
        prefix = "✓ " if is_on else ""
        return InlineKeyboardButton(f"{prefix}{label}", callback_data=cb)

    keyboard = [
        [btn("Full access", CB_PERM_FULL, selected >= set(ALL_PERMISSIONS))],
        [
            btn("Upload", CB_PERM_UPLOAD, PERM_UPLOAD_VIDEOS in selected),
            btn("Schedule", CB_PERM_SCHEDULE, PERM_SCHEDULE_UPLOADS in selected),
            btn("View", CB_PERM_VIEW, PERM_VIEW_SCHEDULED_TASKS in selected),
        ],
        [
            btn("Manage admins", CB_PERM_MANAGE_ADMINS, PERM_MANAGE_ADMINS in selected),
            btn("Manage creds", CB_PERM_MANAGE_CREDS, PERM_MANAGE_CREDS in selected),
        ],
        [InlineKeyboardButton("Done – Add sub-admin", callback_data=CB_PERM_DONE)],
    ]
    return InlineKeyboardMarkup(keyboard)


async def _show_sub_admins(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of sub-admins with their permissions."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = SubAdminRepository(session)
        admins = repo.list_all()
    if not admins:
        await query.edit_message_text(
            "No sub-admins yet. Add one below!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]]),
        )
        return
    lines = [f"• @{u} ({_format_permissions_display(p)})" for u, p in admins]
    text = "Sub-admins:\n\n" + "\n".join(lines)
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]]),
    )


async def _show_gemini_keys(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of Gemini keys with remove buttons."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = GeminiKeyRepository(session)
        keys = repo.list_all_ordered()
    if not keys:
        await query.edit_message_text(
            "No Gemini keys yet. Add one to get started!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]]),
        )
        return
    keyboard = [
        [InlineKeyboardButton(f"Key #{kid} - Remove", callback_data=f"{CB_REMOVE_GEMINI_PREFIX}{kid}")]
        for kid, _ in keys
    ]
    keyboard.append([InlineKeyboardButton("← Back", callback_data=CB_BACK)])
    text = "Gemini keys (tried in order for failover):\n\n" + "\n".join(f"• Key #{kid}" for kid, _ in keys)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_instagram_accounts(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of Instagram accounts with remove buttons."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = InstagramAccountRepository(session)
        accounts = repo.list_all()
    if not accounts:
        await query.edit_message_text(
            "No Instagram accounts yet. Add one to get started!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]]),
        )
        return
    keyboard = [
        [InlineKeyboardButton(f"@{username} - Remove", callback_data=f"{CB_REMOVE_INSTA_PREFIX}{acc_id}")]
        for acc_id, username in accounts
    ]
    keyboard.append([InlineKeyboardButton("← Back", callback_data=CB_BACK)])
    text = "Instagram accounts:\n\n" + "\n".join(f"• @{username} (ID: {acc_id})" for acc_id, username in accounts)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_scheduled_tasks(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show pending/scheduled jobs to the user."""
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = VideoJobRepository(session)
        jobs = repo.get_all_pending_and_scheduled()

    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]])
    if not jobs:
        await query.edit_message_text("No pending or scheduled tasks. All clear! ✓", reply_markup=back_btn)
        return

    lines = []
    for j in jobs[:20]:
        if j.schedule_time:
            dt = j.schedule_time if j.schedule_time.tzinfo else j.schedule_time.replace(tzinfo=timezone.utc)
            bd = dt.astimezone(BANGLADESH_TZ)
            schedule_str = bd.strftime("%b %d, %I:%M %p")
        else:
            schedule_str = "ASAP"
        lines.append(f"• [{j.id}] {j.original_url[:50]}... @ {schedule_str}")
    text = "Scheduled tasks:\n\n" + "\n".join(lines)
    if len(jobs) > 20:
        text += f"\n\n... and {len(jobs) - 20} more"
    await query.edit_message_text(text, reply_markup=back_btn)


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
        await update.message.reply_text("Got it! Gemini key added. ✓")
    except Exception:
        await update.message.reply_text("Couldn't add the key. Try again?")
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
        await update.message.reply_text(f"Added @{username} – Instagram account ready! ✓")
    except Exception:
        await update.message.reply_text("Couldn't add – username may already exist.")
    context.user_data.pop("insta_username", None)
    return ConversationHandler.END


async def add_admin_username_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle username for adding sub-admin - then show permission picker."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END
    username = (update.message.text or "").strip().lower().lstrip("@")
    if not username:
        await update.message.reply_text("Username cannot be empty. Send the username to add (without @):")
        return ADD_ADMIN_USERNAME
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = SubAdminRepository(session)
        if repo.exists(username):
            await update.message.reply_text(f"@{username} is already a sub-admin.")
            return ConversationHandler.END
    context.user_data["new_admin_username"] = username
    context.user_data["new_admin_permissions"] = set(ALL_PERMISSIONS)  # Default: full access
    await update.message.reply_text(
        f"Select permissions for @{username}:",
        reply_markup=_build_permission_picker_keyboard(set(ALL_PERMISSIONS)),
    )
    return ADD_ADMIN_PERMISSIONS


async def add_admin_permissions_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle permission picker callbacks (toggle/done)."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()

    username = context.user_data.get("new_admin_username", "")
    selected = set(context.user_data.get("new_admin_permissions", set()))

    data = query.data or ""
    if data == CB_PERM_DONE:
        if not username:
            await query.edit_message_text("Session expired. Start over from Manage admins.")
            context.user_data.pop("new_admin_username", None)
            context.user_data.pop("new_admin_permissions", None)
            return ConversationHandler.END
        SessionLocal = context.bot_data["SessionLocal"]
        try:
            with get_db_session(SessionLocal) as session:
                repo = SubAdminRepository(session)
                repo.add(username, list(selected))
            perms_str = _format_permissions_display(list(selected))
            await query.edit_message_text(
                f"Done! @{username} is now a sub-admin with {perms_str}. ✓",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]]),
            )
        except ValueError as e:
            await query.edit_message_text(str(e))
        except Exception:
            await query.edit_message_text("Couldn't add – username may already exist.")
        context.user_data.pop("new_admin_username", None)
        context.user_data.pop("new_admin_permissions", None)
        return ConversationHandler.END

    if data == CB_PERM_FULL:
        if selected >= set(ALL_PERMISSIONS):
            selected.clear()
        else:
            selected = set(ALL_PERMISSIONS)
    elif data == CB_PERM_UPLOAD:
        if PERM_UPLOAD_VIDEOS in selected:
            selected.discard(PERM_UPLOAD_VIDEOS)
        else:
            selected.add(PERM_UPLOAD_VIDEOS)
    elif data == CB_PERM_SCHEDULE:
        if PERM_SCHEDULE_UPLOADS in selected:
            selected.discard(PERM_SCHEDULE_UPLOADS)
        else:
            selected.add(PERM_SCHEDULE_UPLOADS)
    elif data == CB_PERM_VIEW:
        if PERM_VIEW_SCHEDULED_TASKS in selected:
            selected.discard(PERM_VIEW_SCHEDULED_TASKS)
        else:
            selected.add(PERM_VIEW_SCHEDULED_TASKS)
    elif data == CB_PERM_MANAGE_ADMINS:
        if PERM_MANAGE_ADMINS in selected:
            selected.discard(PERM_MANAGE_ADMINS)
        else:
            selected.add(PERM_MANAGE_ADMINS)
    elif data == CB_PERM_MANAGE_CREDS:
        if PERM_MANAGE_CREDS in selected:
            selected.discard(PERM_MANAGE_CREDS)
        else:
            selected.add(PERM_MANAGE_CREDS)

    context.user_data["new_admin_permissions"] = selected
    await query.edit_message_text(
        f"Select permissions for @{username}:",
        reply_markup=_build_permission_picker_keyboard(selected),
    )
    return ADD_ADMIN_PERMISSIONS


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
        await update.message.reply_text(f"Removed @{username.lower().lstrip('@')} from sub-admins. ✓")
    else:
        await update.message.reply_text("That user isn't a sub-admin.")
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
        await update.message.reply_text("Hmm, I couldn't find any valid URLs. Try sending video links (YouTube, Instagram, etc.)")
        return UPLOAD_URLS

    context.user_data["urls"] = urls
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = InstagramAccountRepository(session)
        accounts = repo.list_all()

    if not accounts:
        await update.message.reply_text(
            "No Instagram accounts set up yet. Add one in Manage credentials (main admin only)."
        )
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(
        "Which Instagram account should we use?",
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
        f"Uploaded boss! 🎬 {len(job_ids)} video(s) queued – they'll be going live on Instagram shortly.\n\nJob IDs: {job_ids}"
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
        await update.message.reply_text("Hmm, I couldn't find any valid URLs. Try sending video links (YouTube, Instagram, etc.)")
        return SCHEDULE_URLS

    context.user_data["urls"] = urls
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = InstagramAccountRepository(session)
        accounts = repo.list_all()

    if not accounts:
        await update.message.reply_text(
            "No Instagram accounts set up yet. Add one in Manage credentials (main admin only)."
        )
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(
        "Which Instagram account should we use?",
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
        "When should we post? (Bangladesh time)\n\n"
        "Format: month day time am/pm\n"
        "e.g. 3 8 2:30 pm or 12-25 9:00 am"
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
    schedule_time = _parse_schedule_time_bd(text)
    if not schedule_time:
        try:
            if len(text) == 16:  # 2025-03-08 14:00
                schedule_time = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            else:
                schedule_time = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            await update.message.reply_text(
                "Oops! Use this format: month day time am/pm\n"
                "e.g. 3 8 2:30 pm or 12-25 9:00 am"
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

    bd_time = schedule_time.astimezone(BANGLADESH_TZ)
    await update.message.reply_text(
        f"Done! 📅 {len(job_ids)} video(s) scheduled for {bd_time.strftime('%b %d, %Y %I:%M %p')} (BD time). They'll post automatically!\n\nJob IDs: {job_ids}"
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
    await update.message.reply_text("Cancelled. No worries!")
    return ConversationHandler.END


async def start_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fallback: /start clears conversation state and shows main menu."""
    context.user_data.clear()
    await start_command(update, context)
    return ConversationHandler.END


async def callback_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fallback: button click while in a flow - reset to main menu."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END
    main_admin, sub_perms = _get_current_user_permissions(update, context)
    await query.edit_message_text(
        "Hey boss! 👋 What would you like to do?",
        reply_markup=build_main_menu_keyboard(main_admin, sub_perms),
    )
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
            ADD_ADMIN_PERMISSIONS: [
                CallbackQueryHandler(add_admin_permissions_callback),
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
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CommandHandler("start", start_fallback),
            CallbackQueryHandler(callback_fallback),
        ],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start_command))
    app.add_error_handler(_error_handler)

    return app
