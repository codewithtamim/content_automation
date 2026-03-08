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
ADD_COOKIES = 23

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
CB_ADD_COOKIES = "add_cookies"
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

# Time picker (telegraf-time-picker style)
CB_TP_PREFIX = "tp_"


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


async def _get_main_menu_for_completion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    """Get main menu keyboard for showing after task completion."""
    main_admin, sub_perms = _get_current_user_permissions(update, context)
    return build_main_menu_keyboard(main_admin, sub_perms)


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
        [InlineKeyboardButton("Upload YouTube cookies", callback_data=CB_ADD_COOKIES)],
        [InlineKeyboardButton("List Gemini keys", callback_data=CB_LIST_GEMINI)],
        [InlineKeyboardButton("List Instagram accounts", callback_data=CB_LIST_INSTA)],
        [InlineKeyboardButton("← Back", callback_data=CB_BACK)],
    ]
    return InlineKeyboardMarkup(keyboard)


def _days_in_month(month: int, year: int) -> int:
    """Return number of days in month (1-12)."""
    if month in (4, 6, 9, 11):
        return 30
    if month == 2:
        return 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    return 31


def _build_time_picker_keyboard(
    month: int, day: int, hour: int, minute: int, year: int
) -> InlineKeyboardMarkup:
    """
    Build inline time picker keyboard with month, day, year, hour, minute.
    """
    minute = (minute // 5) * 5
    hour = max(0, min(23, hour))
    minute = max(0, min(55, minute))
    month = max(1, min(12, month))
    cur_year = datetime.now().year
    year = max(cur_year - 1, min(cur_year + 2, year))
    max_day = _days_in_month(month, year)
    day = max(1, min(max_day, day))

    def _cb(action: str) -> str:
        return f"{CB_TP_PREFIX}{action}_{month}_{day}_{hour}_{minute}_{year}"

    month_names = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

    # Row 1: Month [−] [Mar] [+]
    month_row = [
        InlineKeyboardButton("−", callback_data=_cb("mo-")),
        InlineKeyboardButton(month_names[month], callback_data=f"{CB_TP_PREFIX}noop"),
        InlineKeyboardButton("+", callback_data=_cb("mo+")),
    ]
    # Row 2: Day [−] [8] [+]
    day_row = [
        InlineKeyboardButton("−", callback_data=_cb("dd-")),
        InlineKeyboardButton(str(day), callback_data=f"{CB_TP_PREFIX}noop"),
        InlineKeyboardButton("+", callback_data=_cb("dd+")),
    ]
    # Row 3: Year [−] [2025] [+]
    year_row = [
        InlineKeyboardButton("−", callback_data=_cb("yr-")),
        InlineKeyboardButton(str(year), callback_data=f"{CB_TP_PREFIX}noop"),
        InlineKeyboardButton("+", callback_data=_cb("yr+")),
    ]
    # Row 4: Hour [−] [14] [+]
    hour_row = [
        InlineKeyboardButton("−", callback_data=_cb("h-")),
        InlineKeyboardButton(f"{hour:02d}", callback_data=f"{CB_TP_PREFIX}noop"),
        InlineKeyboardButton("+", callback_data=_cb("h+")),
    ]
    # Row 5: Minute [−] [30] [+]
    min_row = [
        InlineKeyboardButton("−", callback_data=_cb("m-")),
        InlineKeyboardButton(f"{minute:02d}", callback_data=f"{CB_TP_PREFIX}noop"),
        InlineKeyboardButton("+", callback_data=_cb("m+")),
    ]
    # Row 6: Confirm / Cancel
    submit_row = [
        InlineKeyboardButton("✓ Confirm", callback_data=_cb("ok")),
        InlineKeyboardButton("Cancel", callback_data=f"{CB_TP_PREFIX}cancel"),
    ]

    keyboard = [month_row, day_row, year_row, hour_row, min_row, submit_row]
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
    elif data == CB_ADD_COOKIES:
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        await query.edit_message_text(
            "Send the cookies file (Netscape format).\n\n"
            "Export from your PC: yt-dlp --cookies-from-browser chrome -o cookies.txt\n"
            "Then send the file here."
        )
        return ADD_COOKIES
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
                menu = build_main_menu_keyboard(main_admin, sub_perms)
                if repo.remove(key_id):
                    await query.edit_message_text(f"Removed Gemini key {key_id}. ✓", reply_markup=menu)
                else:
                    await query.edit_message_text("That key wasn't found.", reply_markup=menu)
        except ValueError:
            await query.edit_message_text("Invalid key ID.", reply_markup=build_main_menu_keyboard(main_admin, sub_perms))
        return ConversationHandler.END
    elif data and data.startswith(CB_REMOVE_INSTA_PREFIX):
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        try:
            acc_id = int(data[len(CB_REMOVE_INSTA_PREFIX) :])
            SessionLocal = context.bot_data["SessionLocal"]
            with get_db_session(SessionLocal) as session:
                repo = InstagramAccountRepository(session)
                menu = build_main_menu_keyboard(main_admin, sub_perms)
                if repo.remove(acc_id):
                    await query.edit_message_text(f"Removed Instagram account {acc_id}. ✓", reply_markup=menu)
                else:
                    await query.edit_message_text("That account wasn't found.", reply_markup=menu)
        except ValueError:
            await query.edit_message_text(
                "Invalid account ID.",
                reply_markup=build_main_menu_keyboard(main_admin, sub_perms),
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
        menu = await _get_main_menu_for_completion(update, context)
        await update.message.reply_text("Got it! Gemini key added. ✓", reply_markup=menu)
    except Exception:
        await update.message.reply_text("Couldn't add the key. Try again?")
    return ConversationHandler.END


async def add_cookies_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle cookies file upload - save to cookies path."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END

    if not update.message:
        return ADD_COOKIES

    if not update.message.document:
        menu = await _get_main_menu_for_completion(update, context)
        await update.message.reply_text(
            "Send the cookies file as a document, or /cancel to abort.",
            reply_markup=menu,
        )
        return ConversationHandler.END

    cookies_path = context.bot_data.get("cookies_path")
    if not cookies_path:
        await update.message.reply_text("Cookies path not configured.")
        return ConversationHandler.END

    try:
        doc = update.message.document
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(cookies_path)
        menu = await _get_main_menu_for_completion(update, context)
        await update.message.reply_text(
            "Cookies file saved! ✓ yt-dlp will use it for YouTube downloads.",
            reply_markup=menu,
        )
    except Exception as e:
        logger.exception("Failed to save cookies file: %s", e)
        await update.message.reply_text(f"Couldn't save the file: {e}")
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
        menu = await _get_main_menu_for_completion(update, context)
        await update.message.reply_text(f"Added @{username} – Instagram account ready! ✓", reply_markup=menu)
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
            menu = await _get_main_menu_for_completion(update, context)
            await query.edit_message_text(
                f"Done! @{username} is now a sub-admin with {perms_str}. ✓",
                reply_markup=menu,
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
    menu = await _get_main_menu_for_completion(update, context)
    if removed:
        await update.message.reply_text(f"Removed @{username.lower().lstrip('@')} from sub-admins. ✓", reply_markup=menu)
    else:
        await update.message.reply_text("That user isn't a sub-admin.", reply_markup=menu)
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
    user = update.effective_user
    submitted_by = (user.username or f"user_{user.id}") if user else None
    SessionLocal = context.bot_data["SessionLocal"]
    with get_db_session(SessionLocal) as session:
        repo = VideoJobRepository(session)
        job_ids = create_job(
            repo, urls, schedule_time=None, instagram_account_id=acc_id,
            submitted_by_username=submitted_by,
        )
    menu = await _get_main_menu_for_completion(update, context)
    await query.edit_message_text(
        f"Uploading! 🎬 {len(job_ids)} video(s) queued – they'll be going live on Instagram shortly.\n\nJob IDs: {job_ids}",
        reply_markup=menu,
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
    """Handle account selection for schedule - then show time picker."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith(CB_ACCOUNT_PREFIX):
        return ConversationHandler.END
    acc_id = int(data[len(CB_ACCOUNT_PREFIX) :])
    context.user_data["instagram_account_id"] = acc_id

    # Default: today, current time (or 2:00 PM) in Bangladesh timezone
    now_bd = datetime.now(BANGLADESH_TZ)
    month, day, year = now_bd.month, now_bd.day, now_bd.year
    hour, minute = 14, 0
    if 0 <= now_bd.hour < 24 and 0 <= now_bd.minute < 60:
        hour = now_bd.hour
        minute = (now_bd.minute // 5) * 5

    await query.edit_message_text(
        "When should we post? (Bangladesh time)\n\n"
        "Use the picker below or type: month day time am/pm\n"
        "e.g. 3 8 2:30 pm",
        reply_markup=_build_time_picker_keyboard(month, day, hour, minute, year),
    )
    return SCHEDULE_TIME


def _parse_time_picker_callback(data: str) -> tuple[str, int, int, int, int, int] | None:
    """Parse tp_{action}_{month}_{day}_{hour}_{minute}_{year}. Returns (action, month, day, hour, minute, year) or None."""
    if not data or not data.startswith(CB_TP_PREFIX):
        return None
    rest = data[len(CB_TP_PREFIX) :]
    if rest == "cancel":
        return ("cancel", 1, 1, 0, 0, 2025)
    if rest == "noop":
        return ("noop", 1, 1, 0, 0, 2025)
    parts = rest.split("_")
    if len(parts) != 6:
        return None
    action, mo, dd, h, m, yr = parts
    try:
        return (action, int(mo), int(dd), int(h), int(m), int(yr))
    except ValueError:
        return None


def _apply_time_picker_action(
    action: str, month: int, day: int, hour: int, minute: int, year: int
) -> tuple[int, int, int, int, int]:
    """Apply +/- action and return new (month, day, hour, minute, year)."""
    if action == "mo+":
        new_mo = (month % 12) + 1
        max_d = _days_in_month(new_mo, year)
        return (new_mo, min(day, max_d), hour, minute, year)
    if action == "mo-":
        new_mo = month - 1 if month > 1 else 12
        max_d = _days_in_month(new_mo, year)
        return (new_mo, min(day, max_d), hour, minute, year)
    if action == "dd+":
        max_d = _days_in_month(month, year)
        new_d = (day % max_d) + 1
        return (month, new_d, hour, minute, year)
    if action == "dd-":
        max_d = _days_in_month(month, year)
        new_d = day - 1 if day > 1 else max_d
        return (month, new_d, hour, minute, year)
    cur_year = datetime.now().year
    if action == "yr+":
        return (month, day, hour, minute, min(cur_year + 2, year + 1))
    if action == "yr-":
        return (month, day, hour, minute, max(cur_year - 1, year - 1))
    if action == "h+":
        return (month, day, (hour + 1) % 24, minute, year)
    if action == "h-":
        return (month, day, (hour - 1) % 24, minute, year)
    if action == "m+":
        new_min = minute + 5
        if new_min >= 60:
            return (month, day, (hour + 1) % 24, 0, year)
        return (month, day, hour, new_min, year)
    if action == "m-":
        new_min = minute - 5
        if new_min < 0:
            return (month, day, (hour - 1) % 24, 55, year)
        return (month, day, hour, new_min, year)
    return (month, day, hour, minute, year)


async def schedule_time_picker_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle time picker button callbacks (hour/date +/- , confirm, cancel)."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END

    query = update.callback_query
    parsed = _parse_time_picker_callback(query.data or "")
    if not parsed:
        await query.answer()
        return SCHEDULE_TIME

    action, month, day, hour, minute, year = parsed

    if action == "noop":
        await query.answer()
        return SCHEDULE_TIME

    if action == "cancel":
        await query.answer()
        context.user_data.clear()
        main_admin, sub_perms = _get_current_user_permissions(update, context)
        await query.edit_message_text(
            "Cancelled. No worries!",
            reply_markup=build_main_menu_keyboard(main_admin, sub_perms),
        )
        return ConversationHandler.END

    if action == "ok":
        await query.answer()
        dt_bd = datetime(
            year, month, day, hour, minute, 0, tzinfo=BANGLADESH_TZ,
        )
        schedule_time = dt_bd.astimezone(timezone.utc)

        urls = context.user_data.get("urls", [])
        instagram_account_id = context.user_data.get("instagram_account_id")
        user = update.effective_user
        submitted_by = (user.username or f"user_{user.id}") if user else None
        SessionLocal = context.bot_data["SessionLocal"]

        with get_db_session(SessionLocal) as session:
            repo = VideoJobRepository(session)
            job_ids = create_job(
                repo, urls, schedule_time=schedule_time, instagram_account_id=instagram_account_id,
                submitted_by_username=submitted_by,
            )

        menu = await _get_main_menu_for_completion(update, context)
        await query.edit_message_text(
            f"Done! 📅 {len(job_ids)} video(s) scheduled for "
            f"{dt_bd.strftime('%b %d, %Y %I:%M %p')} (BD time). They'll post automatically!\n\nJob IDs: {job_ids}",
            reply_markup=menu,
        )
        context.user_data.clear()
        return ConversationHandler.END

    # mo+/-, dd+/-, yr+/-, h+/-, m+/-: update keyboard
    new_mo, new_dd, new_h, new_m, new_yr = _apply_time_picker_action(
        action, month, day, hour, minute, year
    )
    await query.answer()
    await query.edit_message_reply_markup(
        reply_markup=_build_time_picker_keyboard(new_mo, new_dd, new_h, new_m, new_yr),
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
    user = update.effective_user
    submitted_by = (user.username or f"user_{user.id}") if user else None
    SessionLocal = context.bot_data["SessionLocal"]

    with get_db_session(SessionLocal) as session:
        repo = VideoJobRepository(session)
        job_ids = create_job(
            repo, urls, schedule_time=schedule_time, instagram_account_id=instagram_account_id,
            submitted_by_username=submitted_by,
        )

    bd_time = schedule_time.astimezone(BANGLADESH_TZ)
    menu = await _get_main_menu_for_completion(update, context)
    await update.message.reply_text(
        f"Done! 📅 {len(job_ids)} video(s) scheduled for {bd_time.strftime('%b %d, %Y %I:%M %p')} (BD time). They'll post automatically!\n\nJob IDs: {job_ids}",
        reply_markup=menu,
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
    cookies_path: str = "",
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
    app.bot_data["cookies_path"] = cookies_path

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
                CallbackQueryHandler(
                    schedule_time_picker_callback,
                    pattern=f"^{CB_TP_PREFIX}",
                ),
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
            ADD_COOKIES: [
                MessageHandler(filters.Document.ALL, add_cookies_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_cookies_received),
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
