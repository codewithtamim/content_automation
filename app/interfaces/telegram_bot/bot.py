"""Telegram bot interface with admin-only access control."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

BANGLADESH_TZ = ZoneInfo("Asia/Dhaka")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Conflict
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
from app.infrastructure.database.session import get_db_session, run_db_async
from app.infrastructure.instagram.remove_dead_videos import remove_dead_videos
from app.infrastructure.scheduler.immediate_prep import start_immediate_prep

logger = logging.getLogger(__name__)


def _parse_schedule_time_bd(text: str) -> datetime | None:
    """
    Parse schedule time in Bangladesh time.
    Formats: month day time am/pm, "tomorrow 9am", "in 1 hour", "3/8 2:30pm".
    Examples: "3 8 2:30 pm", "12-25 9:00 am", "3/8 14:30" (24h also ok).
    """
    text = text.strip().lower()
    now_bd = datetime.now(BANGLADESH_TZ)

    # Preset: "in 1 hour" / "in 2 hours"
    m_hours = re.match(r"in\s+(\d+)\s*hour", text)
    if m_hours:
        from datetime import timedelta
        hours = int(m_hours.group(1))
        return (now_bd + timedelta(hours=hours)).astimezone(timezone.utc)

    # Preset: "tomorrow 9am" / "tomorrow 9:30 pm"
    m_tomorrow = re.match(r"tomorrow\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if m_tomorrow:
        hour = int(m_tomorrow.group(1))
        minute = int(m_tomorrow.group(2) or 0)
        ampm = (m_tomorrow.group(3) or "am").lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        from datetime import timedelta
        tomorrow = now_bd.date() + timedelta(days=1)
        try:
            dt_bd = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute, 0, tzinfo=BANGLADESH_TZ)
            return dt_bd.astimezone(timezone.utc)
        except ValueError:
            return None

    # Preset: "same time tomorrow"
    if re.match(r"same\s+time\s+tomorrow", text):
        from datetime import timedelta
        tomorrow = now_bd + timedelta(days=1)
        return tomorrow.astimezone(timezone.utc)
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
ADD_VIDEOS_URLS = 0
ADD_VIDEOS_PICK_ACCOUNT = 1
ADD_VIDEOS_PICK_MODE = 2
ADD_VIDEOS_SCHEDULE_TIME = 3
# Legacy aliases for compatibility
UPLOAD_URLS = ADD_VIDEOS_URLS
SCHEDULE_URLS, SCHEDULE_TIME = ADD_VIDEOS_URLS, ADD_VIDEOS_SCHEDULE_TIME
UPLOAD_PICK_ACCOUNT, SCHEDULE_PICK_ACCOUNT = ADD_VIDEOS_PICK_ACCOUNT, ADD_VIDEOS_PICK_ACCOUNT
ADD_ADMIN_USERNAME, ADD_ADMIN_PERMISSIONS, REMOVE_ADMIN_USERNAME = 10, 12, 11
ADD_GEMINI_KEY = 20
ADD_INSTA_USERNAME, ADD_INSTA_PASSWORD, ADD_INSTA_WATERMARK = 21, 22, 24
ADD_COOKIES = 23
UPDATE_INSTA_WATERMARK = 25
REMOVE_DEAD_VIDEOS_PICK_ACCOUNT = 26

# Callback data
CB_ADD_VIDEOS = "add_videos"
CB_UPLOAD = "upload"  # Legacy
CB_SCHEDULE = "schedule"  # Legacy
CB_VIEW = "view"
CB_MODE_UPLOAD_NOW = "mode_upload_now"
CB_MODE_SCHEDULE = "mode_schedule"
CB_PRESET_1H = "preset_1h"
CB_PRESET_TOMORROW_9AM = "preset_tomorrow_9am"
CB_PRESET_SAME_TOMORROW = "preset_same_tomorrow"
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
CB_UPDATE_WM_PREFIX = "upd_wm_"
CB_REMOVE_WM_PREFIX = "rm_wm_"
CB_REMOVE_DEAD_VIDEOS = "remove_dead_videos"
CB_RDV_ACCOUNT_PREFIX = "rdv_acc_"
CB_RETRY_JOB_PREFIX = "retry_job_"
CB_CANCEL_JOB_PREFIX = "cancel_job_"
CB_CLEAR_ALL_JOBS = "clear_all_jobs"
CB_CLEAR_ALL_JOBS_CONFIRM = "clear_all_jobs_confirm"
CB_RETRY_ALL_FAILED = "retry_all_failed"
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
    if _user_has_permission(perms, PERM_UPLOAD_VIDEOS) or _user_has_permission(perms, PERM_SCHEDULE_UPLOADS):
        keyboard.append([InlineKeyboardButton("Add videos", callback_data=CB_ADD_VIDEOS)])
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
        [InlineKeyboardButton("Remove dead videos", callback_data=CB_REMOVE_DEAD_VIDEOS)],
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


def _hour24_to_12(hour24: int) -> tuple[int, int]:
    """Convert 24h (0-23) to (hour12 1-12, ampm 0=AM 1=PM)."""
    if hour24 == 0:
        return (12, 0)
    if hour24 < 12:
        return (hour24, 0)
    if hour24 == 12:
        return (12, 1)
    return (hour24 - 12, 1)


def _hour12_to_24(hour12: int, ampm: int) -> int:
    """Convert (hour12 1-12, ampm 0=AM 1=PM) to 24h (0-23)."""
    if ampm == 0:  # AM
        return 0 if hour12 == 12 else hour12
    # PM
    return 12 if hour12 == 12 else hour12 + 12


def _build_mode_picker_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for Upload now vs Schedule for later."""
    keyboard = [
        [InlineKeyboardButton("Upload now", callback_data=CB_MODE_UPLOAD_NOW)],
        [InlineKeyboardButton("Schedule for later", callback_data=CB_MODE_SCHEDULE)],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_time_picker_with_presets(
    month: int, day: int, hour12: int, minute: int, ampm: int, year: int
) -> InlineKeyboardMarkup:
    """Build time picker with preset buttons at top."""
    base_kb = _build_time_picker_keyboard(month, day, hour12, minute, ampm, year)
    # Add preset row at top
    preset_row = [
        InlineKeyboardButton("In 1 hour", callback_data=CB_PRESET_1H),
        InlineKeyboardButton("Tomorrow 9 AM", callback_data=CB_PRESET_TOMORROW_9AM),
        InlineKeyboardButton("Same time tomorrow", callback_data=CB_PRESET_SAME_TOMORROW),
    ]
    # InlineKeyboardMarkup has inline_keyboard - we need to prepend
    new_keyboard = [preset_row] + list(base_kb.inline_keyboard)
    return InlineKeyboardMarkup(new_keyboard)


def _build_time_picker_keyboard(
    month: int, day: int, hour12: int, minute: int, ampm: int, year: int
) -> InlineKeyboardMarkup:
    """
    Build inline time picker keyboard with month, day, year, hour (12h 1-12), minute, AM/PM.
    """
    minute = (minute // 5) * 5
    minute = max(0, min(55, minute))
    month = max(1, min(12, month))
    cur_year = datetime.now().year
    year = max(cur_year - 1, min(cur_year + 2, year))
    max_day = _days_in_month(month, year)
    day = max(1, min(max_day, day))
    hour12 = max(1, min(12, hour12))
    ampm = max(0, min(1, ampm))

    def _cb(action: str) -> str:
        return f"{CB_TP_PREFIX}{action}_{month}_{day}_{hour12}_{minute}_{ampm}_{year}"

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
    # Row 4: Hour (12h) [−] [2] [+]
    hour_row = [
        InlineKeyboardButton("−", callback_data=_cb("h-")),
        InlineKeyboardButton(str(hour12), callback_data=f"{CB_TP_PREFIX}noop"),
        InlineKeyboardButton("+", callback_data=_cb("h+")),
    ]
    # Row 5: Minute [−] [30] [+]
    min_row = [
        InlineKeyboardButton("−", callback_data=_cb("m-")),
        InlineKeyboardButton(f"{minute:02d}", callback_data=f"{CB_TP_PREFIX}noop"),
        InlineKeyboardButton("+", callback_data=_cb("m+")),
    ]
    # Row 6: AM / PM
    ampm_row = [
        InlineKeyboardButton("✓ AM" if ampm == 0 else "AM", callback_data=_cb("ap0")),
        InlineKeyboardButton("✓ PM" if ampm == 1 else "PM", callback_data=_cb("ap1")),
    ]
    # Row 7: Confirm / Cancel
    submit_row = [
        InlineKeyboardButton("✓ Confirm", callback_data=_cb("ok")),
        InlineKeyboardButton("Cancel", callback_data=f"{CB_TP_PREFIX}cancel"),
    ]

    keyboard = [month_row, day_row, year_row, hour_row, min_row, ampm_row, submit_row]
    return InlineKeyboardMarkup(keyboard)


def _build_account_picker_keyboard(accounts: list[tuple[int, str, str | None]]) -> InlineKeyboardMarkup:
    """Build inline keyboard for picking Instagram account."""
    keyboard = [
        [InlineKeyboardButton(f"@{username}", callback_data=f"{CB_ACCOUNT_PREFIX}{acc_id}")]
        for acc_id, username, _wm in accounts
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
    data = query.data
    main_admin, sub_perms = _get_current_user_permissions(update, context)
    user_perms = None if main_admin else sub_perms

    if data and data.startswith(CB_CANCEL_JOB_PREFIX):
        if not _user_has_permission(user_perms, PERM_VIEW_SCHEDULED_TASKS):
            await query.answer()
            return ConversationHandler.END
        try:
            job_id = int(data[len(CB_CANCEL_JOB_PREFIX):])
            SessionLocal = context.bot_data["SessionLocal"]

            def _cancel_job():
                with get_db_session(SessionLocal) as session:
                    repo = VideoJobRepository(session)
                    job = repo.get_by_id(job_id)
                    if job and job.status in ("pending", "ready_to_upload"):
                        job.status = "cancelled"
                        repo.update(job)
                        return job.local_path
                return None

            local_path = await run_db_async(_cancel_job)
            if local_path is not None:
                import os
                if local_path and os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass
            cancelled = local_path is not None
            if cancelled:
                await query.answer("Job cancelled")
                await _show_scheduled_tasks(query, context)
            else:
                await query.answer("Job not found or already processed", show_alert=True)
        except (ValueError, Exception) as e:
            logger.exception("Cancel job failed: %s", e)
            await query.answer("Could not cancel job", show_alert=True)
        return ConversationHandler.END

    await query.answer()

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
    elif data == CB_REMOVE_DEAD_VIDEOS:
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        SessionLocal = context.bot_data["SessionLocal"]

        def _get_insta_accounts():
            with get_db_session(SessionLocal) as session:
                repo = InstagramAccountRepository(session)
                return repo.list_all()

        accounts = await run_db_async(_get_insta_accounts)
        if not accounts:
            await query.edit_message_text(
                "No Instagram accounts yet. Add one first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]]),
            )
            return ConversationHandler.END
        keyboard = [
            [InlineKeyboardButton(f"@{username}", callback_data=f"{CB_RDV_ACCOUNT_PREFIX}{acc_id}")]
            for acc_id, username, _wm in accounts
        ]
        keyboard.append([InlineKeyboardButton("← Back", callback_data=CB_BACK)])
        await query.edit_message_text(
            "Remove reels with 0 views (older than 1 day). Which account?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return REMOVE_DEAD_VIDEOS_PICK_ACCOUNT
    elif data and data.startswith(CB_REMOVE_GEMINI_PREFIX):
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        try:
            key_id = int(data[len(CB_REMOVE_GEMINI_PREFIX) :])
            SessionLocal = context.bot_data["SessionLocal"]

            def _remove_gemini_key():
                with get_db_session(SessionLocal) as session:
                    repo = GeminiKeyRepository(session)
                    return repo.remove(key_id)

            removed = await run_db_async(_remove_gemini_key)
            menu = build_main_menu_keyboard(main_admin, sub_perms)
            if removed:
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

            def _remove_insta_account():
                with get_db_session(SessionLocal) as session:
                    repo = InstagramAccountRepository(session)
                    return repo.remove(acc_id)

            removed = await run_db_async(_remove_insta_account)
            menu = build_main_menu_keyboard(main_admin, sub_perms)
            if removed:
                import os
                wm_file = _watermark_dir() / f"{acc_id}.png"
                if wm_file.exists():
                    os.remove(str(wm_file))
                await query.edit_message_text(f"Removed Instagram account {acc_id}. ✓", reply_markup=menu)
            else:
                await query.edit_message_text("That account wasn't found.", reply_markup=menu)
        except ValueError:
            await query.edit_message_text(
                "Invalid account ID.",
                reply_markup=build_main_menu_keyboard(main_admin, sub_perms),
            )
        return ConversationHandler.END
    elif data and data.startswith(CB_REMOVE_WM_PREFIX):
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        try:
            acc_id = int(data[len(CB_REMOVE_WM_PREFIX):])
            SessionLocal = context.bot_data["SessionLocal"]

            def _remove_watermark():
                with get_db_session(SessionLocal) as session:
                    repo = InstagramAccountRepository(session)
                    repo.update_watermark(acc_id, None)

            await run_db_async(_remove_watermark)
            import os
            wm_file = _watermark_dir() / f"{acc_id}.png"
            if wm_file.exists():
                os.remove(str(wm_file))
            menu = build_main_menu_keyboard(main_admin, sub_perms)
            await query.edit_message_text("Watermark removed. ✓", reply_markup=menu)
        except ValueError:
            await query.edit_message_text("Invalid account ID.", reply_markup=build_main_menu_keyboard(main_admin, sub_perms))
        return ConversationHandler.END
    elif data and data.startswith(CB_UPDATE_WM_PREFIX):
        if not _user_has_permission(user_perms, PERM_MANAGE_CREDS):
            return ConversationHandler.END
        try:
            acc_id = int(data[len(CB_UPDATE_WM_PREFIX):])
            context.user_data["wm_update_account_id"] = acc_id
            await query.edit_message_text("Send the new watermark logo image for this account:")
            return UPDATE_INSTA_WATERMARK
        except ValueError:
            await query.edit_message_text("Invalid account ID.", reply_markup=build_main_menu_keyboard(main_admin, sub_perms))
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

    if data == CB_ADD_VIDEOS or data == CB_UPLOAD or data == CB_SCHEDULE:
        if data == CB_UPLOAD and not _user_has_permission(user_perms, PERM_UPLOAD_VIDEOS):
            return ConversationHandler.END
        if data == CB_SCHEDULE and not _user_has_permission(user_perms, PERM_SCHEDULE_UPLOADS):
            return ConversationHandler.END
        if data == CB_ADD_VIDEOS and not _user_has_permission(user_perms, PERM_UPLOAD_VIDEOS) and not _user_has_permission(user_perms, PERM_SCHEDULE_UPLOADS):
            return ConversationHandler.END
        await query.edit_message_text(
            "Send me the video URLs (comma or newline separated):"
        )
        context.user_data["action"] = "upload" if data == CB_UPLOAD else ("schedule" if data == CB_SCHEDULE else None)
        return ADD_VIDEOS_URLS
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

    def _get_sub_admins():
        with get_db_session(SessionLocal) as session:
            repo = SubAdminRepository(session)
            return repo.list_all()

    admins = await run_db_async(_get_sub_admins)
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

    def _get_gemini_keys():
        with get_db_session(SessionLocal) as session:
            repo = GeminiKeyRepository(session)
            return repo.list_all_ordered()

    keys = await run_db_async(_get_gemini_keys)
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
    """Show list of Instagram accounts with watermark status and management buttons."""
    SessionLocal = context.bot_data["SessionLocal"]

    def _get_insta_accounts():
        with get_db_session(SessionLocal) as session:
            repo = InstagramAccountRepository(session)
            return repo.list_all()

    accounts = await run_db_async(_get_insta_accounts)
    if not accounts:
        await query.edit_message_text(
            "No Instagram accounts yet. Add one to get started!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]]),
        )
        return

    lines = []
    keyboard = []
    for acc_id, username, wm_path in accounts:
        wm_status = "watermark" if wm_path else "no watermark"
        lines.append(f"• @{username} ({wm_status})")
        row = [InlineKeyboardButton(f"@{username} - Remove", callback_data=f"{CB_REMOVE_INSTA_PREFIX}{acc_id}")]
        if wm_path:
            row.append(InlineKeyboardButton("Update WM", callback_data=f"{CB_UPDATE_WM_PREFIX}{acc_id}"))
            row.append(InlineKeyboardButton("Remove WM", callback_data=f"{CB_REMOVE_WM_PREFIX}{acc_id}"))
        else:
            row.append(InlineKeyboardButton("Add WM", callback_data=f"{CB_UPDATE_WM_PREFIX}{acc_id}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("← Back", callback_data=CB_BACK)])
    text = "Instagram accounts:\n\n" + "\n".join(lines)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


def _get_pending_jobs(SessionLocal):
    """Sync helper: fetch pending/scheduled jobs from DB."""
    with get_db_session(SessionLocal) as session:
        repo = VideoJobRepository(session)
        return repo.get_all_pending_and_scheduled()


def _get_failed_jobs_count(SessionLocal) -> int:
    """Sync helper: count failed jobs."""
    with get_db_session(SessionLocal) as session:
        repo = VideoJobRepository(session)
        return len(repo.get_failed_jobs())


async def _show_scheduled_tasks(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show pending/scheduled jobs to the user with Cancel buttons."""
    SessionLocal = context.bot_data["SessionLocal"]
    jobs = await run_db_async(_get_pending_jobs, SessionLocal)
    failed_count = await run_db_async(_get_failed_jobs_count, SessionLocal)

    if not jobs:
        text = "No pending or scheduled tasks. All clear! ✓"
        keyboard = [[InlineKeyboardButton("Clear all jobs", callback_data=CB_CLEAR_ALL_JOBS)]]
        if failed_count > 0:
            keyboard.append([InlineKeyboardButton(f"Retry {failed_count} failed", callback_data=CB_RETRY_ALL_FAILED)])
        keyboard.append([InlineKeyboardButton("← Back", callback_data=CB_BACK)])
        reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        lines = []
        keyboard = []
        for j in jobs[:20]:
            if j.schedule_time:
                dt = j.schedule_time if j.schedule_time.tzinfo else j.schedule_time.replace(tzinfo=timezone.utc)
                bd = dt.astimezone(BANGLADESH_TZ)
                schedule_str = bd.strftime("%b %d, %I:%M %p")
            else:
                schedule_str = "ASAP"
            status_tag = "ready" if j.status == "ready_to_upload" else "pending"
            lines.append(f"• [{j.id}] {j.original_url[:50]}... @ {schedule_str} ({status_tag})")
            keyboard.append([InlineKeyboardButton(f"Cancel #{j.id}", callback_data=f"{CB_CANCEL_JOB_PREFIX}{j.id}")])
        keyboard.append([InlineKeyboardButton("Clear all jobs", callback_data=CB_CLEAR_ALL_JOBS)])
        if failed_count > 0:
            keyboard.append([InlineKeyboardButton(f"Retry {failed_count} failed", callback_data=CB_RETRY_ALL_FAILED)])
        keyboard.append([InlineKeyboardButton("← Back", callback_data=CB_BACK)])
        text = "Scheduled tasks:\n\n" + "\n".join(lines)
        if len(jobs) > 20:
            text += f"\n\n... and {len(jobs) - 20} more"
        reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


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
        def _add_gemini_key():
            with get_db_session(SessionLocal) as session:
                repo = GeminiKeyRepository(session)
                keys = repo.list_all_ordered()
                priority = len(keys)
                repo.add(key, priority=priority)

        await run_db_async(_add_gemini_key)
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
        from pathlib import Path

        path = Path(cookies_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        doc = update.message.document
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(custom_path=str(path))
        logger.info("Cookies saved to %s (%d bytes)", path, path.stat().st_size)

        menu = await _get_main_menu_for_completion(update, context)
        await update.message.reply_text(
            "Cookies file saved! ✓ yt-dlp will use it for YouTube downloads.",
            reply_markup=menu,
        )
    except Exception as e:
        logger.exception("Failed to save cookies file to %s: %s", cookies_path, e)
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
    """Handle Instagram password - store in DB, then ask for watermark."""
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
        def _add_insta_account():
            with get_db_session(SessionLocal) as session:
                repo = InstagramAccountRepository(session)
                return repo.add(username, password)

        model = await run_db_async(_add_insta_account)
        context.user_data["insta_account_id"] = model.id
        await update.message.reply_text(
            f"@{username} saved! Now send a watermark logo image for this account, "
            "or /skip to skip watermark."
        )
        return ADD_INSTA_WATERMARK
    except Exception:
        await update.message.reply_text("Couldn't add – username may already exist.")
    context.user_data.pop("insta_username", None)
    return ConversationHandler.END


def _watermark_dir() -> "Path":
    """Return the watermarks directory, creating it if needed."""
    from pathlib import Path
    wm_dir = Path(__file__).resolve().parents[3] / "data" / "watermarks"
    wm_dir.mkdir(parents=True, exist_ok=True)
    return wm_dir


async def _save_watermark_from_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int,
) -> str | None:
    """Download a photo or document from the message and save as watermark. Returns path or None."""
    msg = update.message
    if not msg:
        return None

    if msg.photo:
        file = await context.bot.get_file(msg.photo[-1].file_id)
    elif msg.document:
        file = await context.bot.get_file(msg.document.file_id)
    else:
        return None

    dest = _watermark_dir() / f"{account_id}.png"
    await file.download_to_drive(custom_path=str(dest))
    return str(dest)


async def add_insta_watermark_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle watermark image upload for a newly added Instagram account."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END

    account_id = context.user_data.get("insta_account_id")
    insta_username = context.user_data.get("insta_username", "")

    if not account_id:
        await update.message.reply_text("Session expired. Start over from Manage credentials.")
        return ConversationHandler.END

    text = (update.message.text or "").strip().lower()
    if text in ("/skip", "skip"):
        menu = await _get_main_menu_for_completion(update, context)
        await update.message.reply_text(
            f"Done! @{insta_username} added without watermark.",
            reply_markup=menu,
        )
        context.user_data.pop("insta_username", None)
        context.user_data.pop("insta_account_id", None)
        return ConversationHandler.END

    path = await _save_watermark_from_message(update, context, account_id)
    if not path:
        await update.message.reply_text("Please send an image (photo or file), or /skip to skip.")
        return ADD_INSTA_WATERMARK

    SessionLocal = context.bot_data["SessionLocal"]

    def _update_watermark():
        with get_db_session(SessionLocal) as session:
            repo = InstagramAccountRepository(session)
            repo.update_watermark(account_id, path)

    await run_db_async(_update_watermark)
    menu = await _get_main_menu_for_completion(update, context)
    await update.message.reply_text(
        f"Done! @{insta_username} added with watermark logo.",
        reply_markup=menu,
    )
    context.user_data.pop("insta_username", None)
    context.user_data.pop("insta_account_id", None)
    return ConversationHandler.END


async def update_insta_watermark_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle watermark image upload for an existing Instagram account."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    if not is_main_admin(update, admin_chat_id, admin_username):
        return ConversationHandler.END

    account_id = context.user_data.get("wm_update_account_id")
    if not account_id:
        await update.message.reply_text("Session expired. Start over from Manage credentials.")
        return ConversationHandler.END

    path = await _save_watermark_from_message(update, context, account_id)
    if not path:
        await update.message.reply_text("Please send an image (photo or file).")
        return UPDATE_INSTA_WATERMARK

    SessionLocal = context.bot_data["SessionLocal"]

    def _update_watermark():
        with get_db_session(SessionLocal) as session:
            repo = InstagramAccountRepository(session)
            repo.update_watermark(account_id, path)

    await run_db_async(_update_watermark)
    menu = await _get_main_menu_for_completion(update, context)
    await update.message.reply_text("Watermark updated! ✓", reply_markup=menu)
    context.user_data.pop("wm_update_account_id", None)
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

    def _check_admin_exists():
        with get_db_session(SessionLocal) as session:
            repo = SubAdminRepository(session)
            return repo.exists(username)

    if await run_db_async(_check_admin_exists):
        await update.message.reply_text(f"@{username} is already a sub-admin.")
        return ConversationHandler.END
    context.user_data["new_admin_username"] = username
    context.user_data["new_admin_permissions"] = set()  # Default: no permissions (user picks what to add)
    await update.message.reply_text(
        f"Select permissions for @{username}:",
        reply_markup=_build_permission_picker_keyboard(set()),
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
        if not selected:
            await query.edit_message_text(
                f"Select at least one permission for @{username}:",
                reply_markup=_build_permission_picker_keyboard(selected),
            )
            return ADD_ADMIN_PERMISSIONS
        SessionLocal = context.bot_data["SessionLocal"]
        try:
            def _add_sub_admin():
                with get_db_session(SessionLocal) as session:
                    repo = SubAdminRepository(session)
                    repo.add(username, list(selected))

            await run_db_async(_add_sub_admin)
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


async def remove_dead_videos_account_picked(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle account selection for Remove dead videos - run service and reply."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END
    main_admin, sub_perms = _get_current_user_permissions(update, context)
    if not _user_has_permission(None if main_admin else sub_perms, PERM_MANAGE_CREDS):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    menu = build_main_menu_keyboard(main_admin, sub_perms)
    creds_menu = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK)]])

    if data == CB_BACK:
        await query.edit_message_text(
            "Manage credentials:", reply_markup=_build_manage_creds_keyboard()
        )
        return ConversationHandler.END

    if not data.startswith(CB_RDV_ACCOUNT_PREFIX):
        return ConversationHandler.END
    try:
        acc_id = int(data[len(CB_RDV_ACCOUNT_PREFIX) :])
    except ValueError:
        await query.edit_message_text("Invalid account.", reply_markup=creds_menu)
        return ConversationHandler.END

    SessionLocal = context.bot_data["SessionLocal"]

    def _get_insta_account():
        with get_db_session(SessionLocal) as session:
            repo = InstagramAccountRepository(session)
            return repo.get_by_id(acc_id)

    account = await run_db_async(_get_insta_account)
    if not account:
        await query.edit_message_text("Account not found.", reply_markup=creds_menu)
        return ConversationHandler.END

    username, password, _ = account
    await query.edit_message_text(f"Scanning @{username} for all 0-view reels...")

    prep_config = context.bot_data.get("prep_config") or {}
    session_path = prep_config.get("instagram_session_path")

    try:
        deleted_count, deleted_codes = remove_dead_videos(
            username, password, min_age_days=None, session_path=session_path
        )
        if deleted_count == 0:
            msg = f"No 0-view reels found for @{username}."
        else:
            codes_str = ", ".join(deleted_codes[:10])
            if len(deleted_codes) > 10:
                codes_str += f" ... and {len(deleted_codes) - 10} more"
            msg = f"Deleted {deleted_count} dead reel(s) (0 views) from @{username}: {codes_str}"
        await query.edit_message_text(msg, reply_markup=menu)
    except Exception as e:
        logger.exception("Remove dead videos failed for @%s: %s", username, e)
        await query.edit_message_text(
            f"Could not remove dead videos: {e}\n\nCheck credentials and try again.",
            reply_markup=menu,
        )
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

    def _remove_sub_admin():
        with get_db_session(SessionLocal) as session:
            repo = SubAdminRepository(session)
            return repo.remove(username)

    removed = await run_db_async(_remove_sub_admin)
    menu = await _get_main_menu_for_completion(update, context)
    if removed:
        await update.message.reply_text(f"Removed @{username.lower().lstrip('@')} from sub-admins. ✓", reply_markup=menu)
    else:
        await update.message.reply_text("That user isn't a sub-admin.", reply_markup=menu)
    return ConversationHandler.END


async def add_videos_urls_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle URLs for Add videos - then show account picker."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END

    urls = parse_urls(update.message.text or "")
    if not urls:
        await update.message.reply_text("Hmm, I couldn't find any valid URLs. Try sending video links (YouTube, Instagram, etc.)")
        return ADD_VIDEOS_URLS

    context.user_data["urls"] = urls
    SessionLocal = context.bot_data["SessionLocal"]

    def _get_accounts():
        with get_db_session(SessionLocal) as session:
            repo = InstagramAccountRepository(session)
            return repo.list_all()

    accounts = await run_db_async(_get_accounts)
    if not accounts:
        await update.message.reply_text(
            "No Instagram accounts set up yet. Add one in Manage credentials (main admin only)."
        )
        context.user_data.clear()
        return ConversationHandler.END

    default_acc_id = context.bot_data.get("default_instagram_account_id")
    if len(accounts) == 1 or (default_acc_id and any(a[0] == default_acc_id for a in accounts)):
        acc_id = default_acc_id if default_acc_id and any(a[0] == default_acc_id for a in accounts) else accounts[0][0]
        context.user_data["instagram_account_id"] = acc_id
        action = context.user_data.get("action")
        if action == "upload":
            user = update.effective_user
            submitted_by = (user.username or f"user_{user.id}") if user else None
            job_ids = await _create_jobs_sync(context, acc_id, schedule_time=None, submitted_by=submitted_by)
            menu = await _get_main_menu_for_completion(update, context)
            await update.message.reply_text(
                f"Uploading! 🎬 {len(job_ids)} video(s) queued – they'll be going live on Instagram shortly.\n\nJob IDs: {job_ids}",
                reply_markup=menu,
            )
            context.user_data.clear()
            return ConversationHandler.END
        if action == "schedule":
            now_bd = datetime.now(BANGLADESH_TZ)
            month, day, year = now_bd.month, now_bd.day, now_bd.year
            hour12, ampm = _hour24_to_12(now_bd.hour if 0 <= now_bd.hour < 24 else 14)
            minute = (now_bd.minute // 5) * 5 if 0 <= now_bd.minute < 60 else 0
            await update.message.reply_text(
                "When should we post? (Bangladesh time)\n\n"
                "Use presets above or type: month day time am/pm, tomorrow 9am, in 1 hour",
                reply_markup=_build_time_picker_with_presets(month, day, hour12, minute, ampm, year),
            )
            return ADD_VIDEOS_SCHEDULE_TIME
        await update.message.reply_text(
            "Upload now or schedule for later?",
            reply_markup=_build_mode_picker_keyboard(),
        )
        return ADD_VIDEOS_PICK_MODE

    await update.message.reply_text(
        "Which Instagram account should we use?",
        reply_markup=_build_account_picker_keyboard(accounts),
    )
    return ADD_VIDEOS_PICK_ACCOUNT


async def add_videos_account_picked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle account selection - show mode picker or create jobs / time picker based on action."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith(CB_ACCOUNT_PREFIX):
        return ConversationHandler.END
    acc_id = int(data[len(CB_ACCOUNT_PREFIX) :])
    context.user_data["instagram_account_id"] = acc_id
    action = context.user_data.get("action")

    if action == "upload":
        return await _do_create_upload_jobs(update, context, acc_id)
    if action == "schedule":
        return await _show_schedule_time_picker(query, context)

    # action is None (Add videos) - show mode picker
    await query.edit_message_text(
        "Upload now or schedule for later?",
        reply_markup=_build_mode_picker_keyboard(),
    )
    return ADD_VIDEOS_PICK_MODE


async def add_videos_mode_picked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle mode selection: Upload now -> create jobs; Schedule -> show time picker."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    acc_id = context.user_data.get("instagram_account_id")
    if not acc_id:
        context.user_data.clear()
        return ConversationHandler.END

    if data == CB_MODE_UPLOAD_NOW:
        context.user_data["action"] = "upload"
        return await _do_create_upload_jobs(update, context, acc_id)
    if data == CB_MODE_SCHEDULE:
        context.user_data["action"] = "schedule"
        return await _show_schedule_time_picker(query, context)
    return ConversationHandler.END


async def _create_jobs_sync(
    context: ContextTypes.DEFAULT_TYPE,
    acc_id: int,
    schedule_time: datetime | None,
    submitted_by: str | None = None,
) -> list[int]:
    """Create jobs and return job IDs. Used by both message and callback flows."""
    urls = context.user_data.get("urls", [])
    SessionLocal = context.bot_data["SessionLocal"]

    def _create():
        with get_db_session(SessionLocal) as session:
            repo = VideoJobRepository(session)
            return create_job(
                repo, urls, schedule_time=schedule_time, instagram_account_id=acc_id,
                submitted_by_username=submitted_by,
            )

    return await run_db_async(_create)


async def _do_create_upload_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE, acc_id: int) -> int:
    """Create jobs with schedule_time=None and show completion."""
    query = update.callback_query
    user = query.from_user if query else update.effective_user
    submitted_by = (user.username or f"user_{user.id}") if user else None
    job_ids = await _create_jobs_sync(context, acc_id, schedule_time=None, submitted_by=submitted_by)
    menu = await _get_main_menu_for_completion(update, context)
    await query.edit_message_text(
        f"Uploading! 🎬 {len(job_ids)} video(s) queued – they'll be going live on Instagram shortly.\n\nJob IDs: {job_ids}",
        reply_markup=menu,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def _show_schedule_time_picker(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show time picker with presets."""
    now_bd = datetime.now(BANGLADESH_TZ)
    month, day, year = now_bd.month, now_bd.day, now_bd.year
    hour12, ampm = _hour24_to_12(now_bd.hour if 0 <= now_bd.hour < 24 else 14)
    minute = (now_bd.minute // 5) * 5 if 0 <= now_bd.minute < 60 else 0

    await query.edit_message_text(
        "When should we post? (Bangladesh time)\n\n"
        "Use presets above or type: month day time am/pm, tomorrow 9am, in 1 hour",
        reply_markup=_build_time_picker_with_presets(month, day, hour12, minute, ampm, year),
    )
    return ADD_VIDEOS_SCHEDULE_TIME


def _parse_time_picker_callback(data: str) -> tuple[str, int, int, int, int, int, int] | None:
    """Parse tp_{action}_{month}_{day}_{hour12}_{minute}_{ampm}_{year}. Returns (action, month, day, hour12, minute, ampm, year) or None."""
    if not data or not data.startswith(CB_TP_PREFIX):
        return None
    rest = data[len(CB_TP_PREFIX) :]
    if rest == "cancel":
        return ("cancel", 1, 1, 12, 0, 0, 2025)
    if rest == "noop":
        return ("noop", 1, 1, 12, 0, 0, 2025)
    parts = rest.split("_")
    if len(parts) != 7:
        return None
    action, mo, dd, h, m, ap, yr = parts
    try:
        return (action, int(mo), int(dd), int(h), int(m), int(ap), int(yr))
    except ValueError:
        return None


def _apply_time_picker_action(
    action: str, month: int, day: int, hour12: int, minute: int, ampm: int, year: int
) -> tuple[int, int, int, int, int, int]:
    """Apply +/- action and return new (month, day, hour12, minute, ampm, year)."""
    if action == "mo+":
        new_mo = (month % 12) + 1
        max_d = _days_in_month(new_mo, year)
        return (new_mo, min(day, max_d), hour12, minute, ampm, year)
    if action == "mo-":
        new_mo = month - 1 if month > 1 else 12
        max_d = _days_in_month(new_mo, year)
        return (new_mo, min(day, max_d), hour12, minute, ampm, year)
    if action == "dd+":
        max_d = _days_in_month(month, year)
        new_d = (day % max_d) + 1
        return (month, new_d, hour12, minute, ampm, year)
    if action == "dd-":
        max_d = _days_in_month(month, year)
        new_d = day - 1 if day > 1 else max_d
        return (month, new_d, hour12, minute, ampm, year)
    cur_year = datetime.now().year
    if action == "yr+":
        return (month, day, hour12, minute, ampm, min(cur_year + 2, year + 1))
    if action == "yr-":
        return (month, day, hour12, minute, ampm, max(cur_year - 1, year - 1))
    if action == "h+":
        new_h12 = (hour12 % 12) + 1
        new_ap = 1 - ampm if hour12 == 11 else ampm  # 11->12 crosses noon/midnight
        return (month, day, new_h12, minute, new_ap, year)
    if action == "h-":
        new_h12 = hour12 - 1 if hour12 > 1 else 12
        new_ap = 1 - ampm if hour12 == 12 else ampm  # 12->11 crosses noon/midnight
        return (month, day, new_h12, minute, new_ap, year)
    if action == "m+":
        new_min = minute + 5
        if new_min >= 60:
            new_h12 = (hour12 % 12) + 1
            new_ap = 1 - ampm if hour12 == 11 else ampm
            return (month, day, new_h12, 0, new_ap, year)
        return (month, day, hour12, new_min, ampm, year)
    if action == "m-":
        new_min = minute - 5
        if new_min < 0:
            new_h12 = hour12 - 1 if hour12 > 1 else 12
            new_ap = 1 - ampm if hour12 == 12 else ampm
            return (month, day, new_h12, 55, new_ap, year)
        return (month, day, hour12, new_min, ampm, year)
    if action == "ap0":
        return (month, day, hour12, minute, 0, year)
    if action == "ap1":
        return (month, day, hour12, minute, 1, year)
    return (month, day, hour12, minute, ampm, year)


def _get_schedule_time_for_preset(preset: str) -> datetime | None:
    """Get schedule_time (UTC) for a preset."""
    from datetime import timedelta
    now_bd = datetime.now(BANGLADESH_TZ)
    if preset == CB_PRESET_1H:
        return (now_bd + timedelta(hours=1)).astimezone(timezone.utc)
    if preset == CB_PRESET_TOMORROW_9AM:
        tomorrow = now_bd.date() + timedelta(days=1)
        dt_bd = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, 0, 0, tzinfo=BANGLADESH_TZ)
        return dt_bd.astimezone(timezone.utc)
    if preset == CB_PRESET_SAME_TOMORROW:
        return (now_bd + timedelta(days=1)).astimezone(timezone.utc)
    return None


async def schedule_time_picker_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle time picker button callbacks (presets, hour/date +/- , confirm, cancel)."""
    admin_chat_id = context.bot_data["admin_chat_id"]
    admin_username = context.bot_data["admin_username"]
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        return ConversationHandler.END

    query = update.callback_query
    data = query.data or ""

    # Handle presets
    if data in (CB_PRESET_1H, CB_PRESET_TOMORROW_9AM, CB_PRESET_SAME_TOMORROW):
        await query.answer()
        schedule_time = _get_schedule_time_for_preset(data)
        if schedule_time:
            urls = context.user_data.get("urls", [])
            instagram_account_id = context.user_data.get("instagram_account_id")
            user = query.from_user
            submitted_by = (user.username or f"user_{user.id}") if user else None
            SessionLocal = context.bot_data["SessionLocal"]

            def _create_scheduled_jobs():
                with get_db_session(SessionLocal) as session:
                    repo = VideoJobRepository(session)
                    return create_job(
                        repo, urls, schedule_time=schedule_time, instagram_account_id=instagram_account_id,
                        submitted_by_username=submitted_by,
                    )

            job_ids = await run_db_async(_create_scheduled_jobs)
            start_immediate_prep(job_ids, context.bot_data)
            menu = await _get_main_menu_for_completion(update, context)
            dt_bd = schedule_time.astimezone(BANGLADESH_TZ)
            await query.edit_message_text(
                f"Done! 📅 {len(job_ids)} video(s) scheduled for "
                f"{dt_bd.strftime('%b %d, %Y %I:%M %p')} (BD time).\n\n"
                f"Processing videos now (download, watermark, metadata) – they'll be ready when the time comes.\n\nJob IDs: {job_ids}",
                reply_markup=menu,
            )
            context.user_data.clear()
            return ConversationHandler.END

    parsed = _parse_time_picker_callback(data)
    if not parsed:
        await query.answer()
        return ADD_VIDEOS_SCHEDULE_TIME

    action, month, day, hour12, minute, ampm, year = parsed

    if action == "noop":
        await query.answer()
        return ADD_VIDEOS_SCHEDULE_TIME

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
        hour24 = _hour12_to_24(hour12, ampm)
        dt_bd = datetime(
            year, month, day, hour24, minute, 0, tzinfo=BANGLADESH_TZ,
        )
        schedule_time = dt_bd.astimezone(timezone.utc)

        urls = context.user_data.get("urls", [])
        instagram_account_id = context.user_data.get("instagram_account_id")
        user = update.effective_user
        submitted_by = (user.username or f"user_{user.id}") if user else None
        SessionLocal = context.bot_data["SessionLocal"]

        def _create_scheduled_jobs():
            with get_db_session(SessionLocal) as session:
                repo = VideoJobRepository(session)
                return create_job(
                    repo, urls, schedule_time=schedule_time, instagram_account_id=instagram_account_id,
                    submitted_by_username=submitted_by,
                )

        job_ids = await run_db_async(_create_scheduled_jobs)
        start_immediate_prep(job_ids, context.bot_data)
        menu = await _get_main_menu_for_completion(update, context)
        await query.edit_message_text(
            f"Done! 📅 {len(job_ids)} video(s) scheduled for "
            f"{dt_bd.strftime('%b %d, %Y %I:%M %p')} (BD time).\n\n"
            f"Processing videos now (download, watermark, metadata) – they'll be ready when the time comes.\n\nJob IDs: {job_ids}",
            reply_markup=menu,
        )
        context.user_data.clear()
        return ConversationHandler.END

    # mo+/-, dd+/-, yr+/-, h+/-, m+/-, ap0/ap1: update keyboard
    new_mo, new_dd, new_h12, new_m, new_ap, new_yr = _apply_time_picker_action(
        action, month, day, hour12, minute, ampm, year
    )
    await query.answer()
    await query.edit_message_reply_markup(
        reply_markup=_build_time_picker_with_presets(new_mo, new_dd, new_h12, new_m, new_ap, new_yr),
    )
    return ADD_VIDEOS_SCHEDULE_TIME


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
            return ADD_VIDEOS_SCHEDULE_TIME

    urls = context.user_data.get("urls", [])
    instagram_account_id = context.user_data.get("instagram_account_id")
    user = update.effective_user
    submitted_by = (user.username or f"user_{user.id}") if user else None
    SessionLocal = context.bot_data["SessionLocal"]

    def _create_scheduled_jobs():
        with get_db_session(SessionLocal) as session:
            repo = VideoJobRepository(session)
            return create_job(
                repo, urls, schedule_time=schedule_time, instagram_account_id=instagram_account_id,
                submitted_by_username=submitted_by,
            )

    job_ids = await run_db_async(_create_scheduled_jobs)
    start_immediate_prep(job_ids, context.bot_data)
    bd_time = schedule_time.astimezone(BANGLADESH_TZ)
    menu = await _get_main_menu_for_completion(update, context)
    await update.message.reply_text(
        f"Done! 📅 {len(job_ids)} video(s) scheduled for {bd_time.strftime('%b %d, %Y %I:%M %p')} (BD time).\n\n"
        f"Processing videos now (download, watermark, metadata) – they'll be ready when the time comes.\n\nJob IDs: {job_ids}",
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


def _delete_all_jobs_sync(SessionLocal) -> int:
    """Sync delete - run in thread to avoid blocking event loop."""
    with get_db_session(SessionLocal) as session:
        repo = VideoJobRepository(session)
        return repo.delete_all()


async def clear_all_jobs_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Yes, clear all button."""
    query = update.callback_query
    if not query or not query.data:
        return
    admin_chat_id = context.bot_data.get("admin_chat_id")
    admin_username = context.bot_data.get("admin_username")
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        await query.answer()
        return
    await query.answer("Clearing...")
    pause_event = context.bot_data.get("worker_pause_event")
    try:
        if pause_event:
            pause_event.set()
            await asyncio.sleep(6)
        SessionLocal = context.bot_data["SessionLocal"]
        count = await run_db_async(_delete_all_jobs_sync, SessionLocal, timeout=15.0)
        await query.edit_message_text(
            f"Cleared {count} jobs. ✓",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_VIEW)]]),
        )
    except asyncio.TimeoutError:
        logger.warning("Clear all jobs timed out (DB locked?)")
        await query.edit_message_text(
            "Database busy. Try again in a moment, or stop the app and run: python clear_jobs.py",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_VIEW)]]),
        )
    except Exception as e:
        logger.exception("Clear all jobs failed: %s", e)
        await query.edit_message_text(
            f"Failed: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_VIEW)]]),
        )
    finally:
        if pause_event:
            pause_event.clear()


async def clear_all_jobs_show_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Clear all jobs button - show confirmation."""
    query = update.callback_query
    if not query or not query.data:
        return
    admin_chat_id = context.bot_data.get("admin_chat_id")
    admin_username = context.bot_data.get("admin_username")
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        await query.answer()
        return
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Yes, clear all", callback_data=CB_CLEAR_ALL_JOBS_CONFIRM)],
        [InlineKeyboardButton("← Cancel", callback_data=CB_VIEW)],
    ]
    await query.edit_message_text(
        "Clear all jobs?\n\nThis will delete ALL jobs (pending, failed, completed).",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cancel_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Cancel button on scheduled tasks."""
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        job_id = int(query.data[len(CB_CANCEL_JOB_PREFIX):])
        SessionLocal = context.bot_data["SessionLocal"]

        def _cancel_job():
            with get_db_session(SessionLocal) as session:
                repo = VideoJobRepository(session)
                job = repo.get_by_id(job_id)
                if job and job.status in ("pending", "ready_to_upload"):
                    job.status = "cancelled"
                    repo.update(job)
                    return job.local_path
            return None

        local_path = await run_db_async(_cancel_job)
        if local_path is not None:
            import os
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass
        cancelled = local_path is not None
        if cancelled:
            await query.answer("Job cancelled")
            await _show_scheduled_tasks(query, context)
        else:
            await query.answer("Job not found or already processed", show_alert=True)
    except (ValueError, Exception) as e:
        logger.exception("Cancel job failed: %s", e)
        await query.answer("Could not cancel job", show_alert=True)


async def retry_all_failed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Retry all failed button - requeue all failed jobs."""
    query = update.callback_query
    if not query or not query.data:
        return
    admin_chat_id = context.bot_data.get("admin_chat_id")
    admin_username = context.bot_data.get("admin_username")
    sub_admin_usernames = _get_sub_admin_usernames(context)
    if not is_admin(update, admin_chat_id, admin_username, sub_admin_usernames):
        await query.answer()
        return
    SessionLocal = context.bot_data["SessionLocal"]

    def _retry_all():
        with get_db_session(SessionLocal) as session:
            repo = VideoJobRepository(session)
            return repo.retry_all_failed()

    count = await run_db_async(_retry_all)
    await query.answer(f"Retried {count} job(s)")
    await _show_scheduled_tasks(query, context)


async def retry_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Retry button on failed job notifications."""
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        job_id = int(query.data[len(CB_RETRY_JOB_PREFIX):])
        SessionLocal = context.bot_data["SessionLocal"]

        def _retry_job():
            with get_db_session(SessionLocal) as session:
                repo = VideoJobRepository(session)
                job = repo.get_by_id(job_id)
                if job and job.status == "failed":
                    job.status = "pending"
                    job.error_message = None
                    repo.update(job)
                    return True
            return False

        retried = await run_db_async(_retry_job)
        if retried:
            await query.answer("Job queued for retry")
            await query.edit_message_text(
                query.message.text + "\n\n✓ Queued for retry.",
                reply_markup=None,
            )
        else:
            await query.answer("Job not found or already retried", show_alert=True)
    except (ValueError, Exception) as e:
        logger.exception("Retry job failed: %s", e)
        await query.answer("Could not retry job", show_alert=True)


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
    worker_pause_event=None,
    default_instagram_account_id: int | None = None,
    prep_config: dict | None = None,
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
    app.bot_data["worker_pause_event"] = worker_pause_event
    app.bot_data["default_instagram_account_id"] = default_instagram_account_id
    app.bot_data["prep_config"] = prep_config or {}

    # Conversation handler for Add videos, admin and credential management flows
    preset_pattern = f"^({CB_PRESET_1H}|{CB_PRESET_TOMORROW_9AM}|{CB_PRESET_SAME_TOMORROW})$"
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_handler)],
        states={
            ADD_VIDEOS_URLS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_videos_urls_received),
            ],
            ADD_VIDEOS_PICK_ACCOUNT: [
                CallbackQueryHandler(add_videos_account_picked, pattern=f"^{CB_ACCOUNT_PREFIX}"),
            ],
            ADD_VIDEOS_PICK_MODE: [
                CallbackQueryHandler(add_videos_mode_picked, pattern=f"^({CB_MODE_UPLOAD_NOW}|{CB_MODE_SCHEDULE})$"),
            ],
            ADD_VIDEOS_SCHEDULE_TIME: [
                CallbackQueryHandler(schedule_time_picker_callback, pattern=f"^{CB_TP_PREFIX}"),
                CallbackQueryHandler(schedule_time_picker_callback, pattern=preset_pattern),
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
            ADD_INSTA_WATERMARK: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, add_insta_watermark_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_insta_watermark_received),
            ],
            UPDATE_INSTA_WATERMARK: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, update_insta_watermark_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, update_insta_watermark_received),
            ],
            ADD_COOKIES: [
                MessageHandler(filters.Document.ALL, add_cookies_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_cookies_received),
            ],
            REMOVE_DEAD_VIDEOS_PICK_ACCOUNT: [
                CallbackQueryHandler(remove_dead_videos_account_picked),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CommandHandler("start", start_fallback),
            CallbackQueryHandler(callback_fallback),
        ],
    )

    app.add_handler(
        CallbackQueryHandler(clear_all_jobs_show_confirm_callback, pattern=f"^{CB_CLEAR_ALL_JOBS}$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(clear_all_jobs_confirm_callback, pattern=f"^{CB_CLEAR_ALL_JOBS_CONFIRM}$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(cancel_job_callback, pattern=f"^{CB_CANCEL_JOB_PREFIX}"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(retry_job_callback, pattern=f"^{CB_RETRY_JOB_PREFIX}"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(retry_all_failed_callback, pattern=f"^{CB_RETRY_ALL_FAILED}$"),
        group=0,
        group=0,
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start_command))
    app.add_error_handler(_error_handler)

    return app
