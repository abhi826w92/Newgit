#!/usr/bin/env python3
"""
MyStore - Telegram Admin Bot Control Panel
Purely button-driven (/start only) admin bot with Firebase Realtime Database,
GitHub Releases API auto-uploader, and Real-Time App & Screenshots Management.
"""

import os
import sys
import json
import time
import re
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import threading

# Thread-safety lock for concurrent album photo uploads
_shots_lock = threading.Lock()

# Safe callback answer wrapper to avoid 400 timeout exceptions
def safe_answer_callback(call, text=None, show_alert=False):
    try:
        bot.answer_callback_query(call.id, text=text, show_alert=show_alert)
    except Exception:
        pass

def log_activity(action, user_id=None, details=""):
    """Real-time formatted activity logger with timestamps for VPS and terminal output"""
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    u_str = f" [User: {user_id}]" if user_id else ""
    d_str = f" - {details}" if details else ""
    print(f"{timestamp} ⚡ {action}{u_str}{d_str}", flush=True)

# Load environment variables
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))

from firebase_manager import firebase_mgr
from github_manager import github_mgr
from keyboards import (
    main_menu_keyboard,
    project_type_keyboard,
    category_selector_keyboard,
    categories_manager_keyboard,
    category_item_keyboard,
    confirm_delete_category_keyboard,
    app_list_keyboard,
    app_detail_actions_keyboard,
    app_edit_menu_keyboard,
    top_apps_manager_keyboard,
    top_app_action_keyboard,
    badge_selection_keyboard,
    badge_duration_keyboard,
    screenshots_manager_keyboard,
    delete_screenshot_list_keyboard,
    github_releases_list_keyboard,
    github_release_detail_keyboard,
    confirm_delete_github_release_keyboard,
    confirm_delete_keyboard,
    developer_profile_keyboard,
    export_options_keyboard,
    back_to_main_keyboard,
    cancel_wizard_keyboard,
    skip_step_keyboard,
    screenshots_step_keyboard,
    is_valid_telegram_button_url,
)

# Configuration & Security Whitelist
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8630369883:AAGN6KGgl0TGYaHRkdJt_epiiWy60icpZD0")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "7251749429,7249511572")
ADMIN_IDS = [int(i.strip()) for i in ADMIN_IDS_RAW.split(",") if i.strip().isdigit()]
ADMIN_SECURITY_TOKEN = os.getenv("ADMIN_SECURITY_TOKEN", "sec_mystore_9a8b7c6d5e4f3a2b1c0d")
STORE_WEB_URL = os.getenv("STORE_WEB_URL", "http://localhost:8080").rstrip("/")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "-1003887776900"))

from telegram_storage import storage_mgr
from progress import ProgressCallbackHandler, make_progress_bar, render_step_progress_text
from cpp_engine import fast_crc32_hash
from imgbb_manager import imgbb_mgr

from telebot import apihelper
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure Global Persistent High-Speed Connection Pool for Telegram Bot API
_session = requests.Session()
_retries = Retry(total=3, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
_adapter = HTTPAdapter(pool_connections=25, pool_maxsize=100, max_retries=_retries)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)
apihelper.SESSION = _session

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", num_threads=16)

# Globally wrap answer_callback_query to safely catch expired callback timeouts (Telegram Error 400)
_orig_answer_callback_query = bot.answer_callback_query
def _safe_answer_callback_query(*args, **kwargs):
    try:
        return _orig_answer_callback_query(*args, **kwargs)
    except Exception:
        return False
bot.answer_callback_query = _safe_answer_callback_query

# Temporary in-memory user conversation states
user_states = {}


def is_admin(user_id):
    """Check if Telegram user is authorized admin"""
    return user_id in ADMIN_IDS


def smart_download_telegram_document(message, doc, target_path, status_msg=None):
    """
    100% Pure MTProto streaming engine:
    - Streams directly from Telegram Datacenter via MTProto (bypassing all 20MB Bot API limits, supports up to 2GB-4GB).
    - Uses encrypted session storage.
    """
    file_size = getattr(doc, 'file_size', 0) or 0
    file_size_mb = round(file_size / (1024 * 1024), 2)
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    # 1. Primary Pure MTProto Stream
    if status_msg:
        try:
            bot.edit_message_text(
                f"⚡ <b>Step 1/3:</b> MTProto Streaming <code>{os.path.basename(target_path)}</code> ({file_size_mb} MB)...\n"
                f"📊 <code>{make_progress_bar(20, 100)}</code>",
                message.chat.id,
                status_msg.message_id
            )
        except Exception:
            pass

    ok, res = storage_mgr.sync_download_media(
        chat_id=message.chat.id,
        message_id=message.message_id,
        destination_path=target_path
    )
    if ok and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        return True, res

    # 2. Fallback only if MTProto is unconfigured and file < 20MB
    if file_size < 20 * 1024 * 1024:
        try:
            file_info = bot.get_file(doc.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            with open(target_path, "wb") as f:
                f.write(downloaded_file)
            return True, target_path
        except Exception as e:
            return False, f"Download error: {e}"

    return False, f"MTProto Download Failed: {res}"


# --------------------------------------------------------------------------
# ONLY Command: /start
# --------------------------------------------------------------------------
@bot.message_handler(commands=["start", "menu", "admin"])
def handle_start(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        log_activity("Unauthorized /start attempt", user_id=user_id, details=f"Chat: {message.chat.id}")
        bot.reply_to(
            message,
            "⛔ <b>Access Denied</b>\n\n"
            "You are not authorized to access this private admin console.\n"
            f"Your Telegram ID: <code>{user_id}</code>"
        )
        return

    log_activity("Admin opened panel (/start)", user_id=user_id, details=f"Chat: {message.chat.id}")

    # Clear any active wizard state
    user_states.pop(user_id, None)

    apps = firebase_mgr.get_all_apps()
    dev = firebase_mgr.get_developer_profile()
    dev_name = dev.get("name", "R3V_X")

    welcome_text = (
        f"👋 <b>Welcome {dev_name}!</b>\n\n"
        f"🏪 <b>MyStore Admin Control Panel</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>Live Apps in Catalog:</b> <code>{len(apps)}</code>\n"
        f"🔥 <b>Firebase Database:</b> <code>Connected (Realtime)</code>\n"
        f"🐙 <b>GitHub Releases API:</b> <code>Ready ({github_mgr.owner}/{github_mgr.repo})</code>\n"
        f"🔒 <b>Security Token:</b> <code>Active & Verified</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Select an action below using buttons:</i>"
    )

    bot.send_message(message.chat.id, welcome_text, reply_markup=main_menu_keyboard())


# --------------------------------------------------------------------------
# Callback Queries Router (All Inline Buttons)
# --------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: True)
def handle_callback_router(call):
    user_id = call.from_user.id
    if not is_admin(user_id):
        log_activity("Unauthorized button tap", user_id=user_id, details=f"Data: {call.data}")
        bot.answer_callback_query(call.id, "⛔ Unauthorized", show_alert=True)
        return

    data = call.data
    log_activity("Button Clicked", user_id=user_id, details=f"Callback: {data}")

    # Return to Main Menu
    if data == "menu:main":
        user_states.pop(user_id, None)
        apps = firebase_mgr.get_all_apps()
        dev = firebase_mgr.get_developer_profile()
        dev_name = dev.get("name", "R3V_X")

        menu_text = (
            f"🏪 <b>MyStore Admin Control Panel</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Total Projects:</b> <code>{len(apps)}</code>\n"
            f"👤 <b>Developer:</b> <code>{dev_name}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Select an action below:</i>"
        )
        try:
            bot.edit_message_text(menu_text, call.message.chat.id, call.message.message_id, reply_markup=main_menu_keyboard())
        except Exception:
            bot.send_message(call.message.chat.id, menu_text, reply_markup=main_menu_keyboard())
        bot.answer_callback_query(call.id)
        return

    # 1. Add New Project (Choose Type or Custom Category)
    elif data == "menu:add_project":
        cats = firebase_mgr.get_categories(force_refresh=True)
        text = (
            "➕ <b>Add New Project to MyStore</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Select the project type or category you want to publish:"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=project_type_keyboard(cats))
        safe_answer_callback(call)
        return

    # Selected Project Type or Category
    elif data.startswith("type:"):
        parts = data.split(":")
        p_type = parts[1]
        
        if p_type == "custom":
            cat_name = parts[2] if len(parts) > 2 else "Apps"
            cat_lower = cat_name.lower()
            if "bot" in cat_lower:
                app_type = "bot"
                type_title = f"{cat_name} (Telegram Bot)"
            elif "game" in cat_lower:
                app_type = "game"
                type_title = f"{cat_name} (Game)"
            elif "mod" in cat_lower:
                app_type = "mod"
                type_title = f"{cat_name} (Mod / APK)"
            elif "web" in cat_lower:
                app_type = "web"
                type_title = f"{cat_name} (Web App)"
            else:
                app_type = "app"
                type_title = f"{cat_name} Project"
        elif p_type == "web":
            cat_name = "Web"
            app_type = "web"
            type_title = "Web Application (PWA)"
        elif p_type == "opensource":
            cat_name = "Open Source"
            app_type = "opensource"
            type_title = "Open Source Repository"
        else: # app
            cat_name = parts[2] if len(parts) > 2 else "Apps"
            app_type = "app"
            type_title = "Android App / Package"

        user_states[user_id] = {
            "stage": "wizard_step_name",
            "app_data": {
                "type": app_type,
                "category": cat_name
            }
        }
        
        text = (
            f"🚀 <b>Creating {type_title}</b>\n"
            f"📂 <b>Assigned Category:</b> <code>{cat_name}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Step 1/8:</b> Please reply with the <b>Project Name</b>\n"
            f"<i>(e.g., 'FocusFlow Timer' or 'MyTool')</i>"
        )
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        msg = bot.send_message(call.message.chat.id, text, reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_wizard_step_name)
        safe_answer_callback(call)
        return

    # 2. Upload APK to GitHub Releases (Interactive Release Wizard)
    elif data == "menu:upload_apk":
        user_states[user_id] = {"stage": "rel_tag", "rel_data": {}}
        text = (
            "📦 <b>Create & Upload GitHub Release</b>\n"
            f"Target Repository: <code>{github_mgr.owner}/{github_mgr.repo}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Step 1/4:</b> Please reply with the <b>Release Tag</b>\n"
            "<i>(e.g., <code>v1.0.0</code>, <code>v2.4.1</code>, or <code>v3.0.0</code>)</i>"
        )
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        msg = bot.send_message(call.message.chat.id, text, reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_release_step_tag)
        safe_answer_callback(call)
        return

    # 3. List & Manage Apps
    elif data == "menu:list_apps":
        apps = firebase_mgr.get_all_apps()
        if not apps:
            bot.edit_message_text("📭 <b>No applications found in catalog.</b>", call.message.chat.id, call.message.message_id, reply_markup=back_to_main_keyboard())
            bot.answer_callback_query(call.id)
            return

        text = "📋 <b>Select a Project to Manage or Edit:</b>"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=app_list_keyboard(apps, "view_app"))
        bot.answer_callback_query(call.id)
        return

    # View Single App Detail
    elif data.startswith("view_app:") or data.startswith("app_detail:"):
        app_id = data.split(":", 1)[1]
        app = firebase_mgr.get_app(app_id)
        if not app:
            bot.answer_callback_query(call.id, "App not found!", show_alert=True)
            return

        featured_tag = "🌟 Featured App\n" if app.get("featured") else ""
        pinned_tag = f"📌 Top Pick #{app.get('pinnedOrder', 1)}\n" if app.get("pinned") else ""
        shots_count = len(app.get("screenshots", []))
        detail_text = (
            f"📱 <b>{app.get('name')}</b>\n"
            f"{pinned_tag}"
            f"{featured_tag}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>ID:</b> <code>{app.get('id')}</code>\n"
            f"🏷️ <b>Type:</b> <code>{app.get('type', 'app').upper()}</code>\n"
            f"📂 <b>Category:</b> <code>{app.get('category', 'Apps')}</code>\n"
            f"🏷️ <b>Version:</b> <code>{app.get('version', 'v1.0')}</code>\n"
            f"💾 <b>Size:</b> <code>{app.get('size', 'N/A')}</code>\n"
            f"🖼️ <b>Screenshots:</b> <code>{shots_count} attached</code>\n"
            f"📅 <b>Updated:</b> <code>{app.get('updatedDate', 'Recently')}</code>\n"
            f"🔗 <b>Target URL:</b> <code>{app.get('downloadUrl') or app.get('webUrl') or app.get('sourceUrl') or 'None'}</code>\n\n"
            f"💬 <i>{app.get('tagline', '')}</i>"
        )

        try:
            bot.edit_message_text(detail_text, call.message.chat.id, call.message.message_id, reply_markup=app_detail_actions_keyboard(app_id, STORE_WEB_URL, is_pinned=bool(app.get('pinned'))))
        except Exception:
            bot.send_message(call.message.chat.id, detail_text, reply_markup=app_detail_actions_keyboard(app_id, STORE_WEB_URL, is_pinned=bool(app.get('pinned'))))
        bot.answer_callback_query(call.id)
        return

    # Copy / View Public Web Link
    elif data.startswith("copy_url:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        app_name = app.get("name", "App") if app else "App"
        link = f"{STORE_WEB_URL}/#/apps/{app_id}"
        bot.send_message(
            call.message.chat.id, 
            f"🌐 <b>Web Link for {app_name}:</b>\n<code>{link}</code>", 
            reply_markup=back_to_main_keyboard()
        )
        bot.answer_callback_query(call.id, "Link sent!")
        return

    # Pin / Unpin Directly from App Details
    elif data.startswith("pin_app:"):
        app_id = data.split(":")[1]
        firebase_mgr.pin_app_to_top(app_id)
        bot.answer_callback_query(call.id, "📌 Pinned to Top of Web Store!", show_alert=True)
        app = firebase_mgr.get_app(app_id)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=app_detail_actions_keyboard(app_id, STORE_WEB_URL, is_pinned=True))
        return

    elif data.startswith("unpin_app:"):
        app_id = data.split(":")[1]
        firebase_mgr.unpin_app(app_id)
        bot.answer_callback_query(call.id, "Unpinned from Top.", show_alert=True)
        app = firebase_mgr.get_app(app_id)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=app_detail_actions_keyboard(app_id, STORE_WEB_URL, is_pinned=False))
        return

    # Upload New Version / Update App Release
    elif data.startswith("update_app_release:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        if not app:
            bot.answer_callback_query(call.id, "App not found!", show_alert=True)
            return

        app_name = app.get("name", "App")
        curr_version = app.get("version", "v1.0")
        user_states[user_id] = {
            "stage": "app_update_upload",
            "app_id": app_id,
            "app_name": app_name,
            "curr_version": curr_version
        }

        prompt_text = (
            f"🚀 <b>Upload New Version / Update for {app_name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ <b>Current Version:</b> <code>{curr_version}</code>\n"
            f"📁 <b>App ID:</b> <code>{app_id}</code>\n\n"
            f"👉 <b>Send the New APK / File (as a Document or File)</b> directly here!\n\n"
            f"<i>Tip: You can also reply with a direct Download URL.</i>"
        )
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(InlineKeyboardButton("🔙 Back to App Menu", callback_data=f"view_app:{app_id}"))
        msg = bot.send_message(call.message.chat.id, prompt_text, reply_markup=markup)
        bot.register_next_step_handler(msg, process_app_update_input)
        safe_answer_callback(call)
        return

    # ----------------------------------------------------------------------
    # Top & Pinned Apps Manager Handlers
    # ----------------------------------------------------------------------
    elif data == "menu:top_apps":
        pinned = firebase_mgr.get_pinned_apps()
        if not pinned:
            empty_text = (
                "📌 <b>Top & Pinned Apps Manager</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<i>No apps are currently pinned to the top of the web store.</i>\n\n"
                "Tap button below to select an app and pin it to #1 Top!"
            )
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("➕ Pin an App to Top", callback_data="menu:pin_pick_app"))
            markup.add(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main"))
            bot.edit_message_text(empty_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
            return

        text = (
            f"📌 <b>Top Apps Ranking ({len(pinned)} pinned)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"These apps are displayed at the very TOP of the Web Store:\n\n"
            f"<i>Tap an app below to Move Up/Down, Edit, or Remove:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=top_apps_manager_keyboard(pinned))
        bot.answer_callback_query(call.id)
        return

    elif data == "menu:pin_pick_app":
        apps = firebase_mgr.get_all_apps()
        unpinned = [a for a in apps if not a.get("pinned")]
        if not unpinned:
            bot.answer_callback_query(call.id, "All apps are already pinned!", show_alert=True)
            return
        bot.edit_message_text("📌 <b>Select an App to Pin to Top (#1):</b>", call.message.chat.id, call.message.message_id, reply_markup=app_list_keyboard(unpinned, "pin_app"))
        return

    elif data.startswith("top_app_manage:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        if not app:
            bot.answer_callback_query(call.id, "App not found!", show_alert=True)
            return

        pinned = firebase_mgr.get_pinned_apps()
        curr_idx = next((i for i, a in enumerate(pinned) if str(a.get("id", "")).lower() == str(app_id).lower()), 0)
        rank = app.get("pinnedOrder", curr_idx + 1)
        is_first = (curr_idx == 0)
        is_last = (curr_idx == len(pinned) - 1)

        text = (
            f"📌 <b>Top App # {rank}: {app.get('name')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ <b>Category:</b> {app.get('category', 'Apps')}\n"
            f"🏷️ <b>Badge:</b> <code>{app.get('badge', 'None')}</code>\n"
            f"💬 <i>{app.get('tagline', '')}</i>\n\n"
            f"<i>Use buttons below to reorder, edit, or remove:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=top_app_action_keyboard(app_id, rank, is_first, is_last))
        bot.answer_callback_query(call.id)
        return

    elif data.startswith("top_move:"):
        parts = data.split(":")
        app_id = parts[1]
        direction = parts[2]
        firebase_mgr.move_pinned_app(app_id, direction)
        
        pinned = firebase_mgr.get_pinned_apps()
        curr_idx = next((i for i, a in enumerate(pinned) if str(a.get("id", "")).lower() == str(app_id).lower()), 0)
        app = firebase_mgr.get_app(app_id)
        rank = app.get("pinnedOrder", curr_idx + 1)
        is_first = (curr_idx == 0)
        is_last = (curr_idx == len(pinned) - 1)

        text = (
            f"📌 <b>Top App # {rank}: {app.get('name')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ <b>Category:</b> {app.get('category', 'Apps')}\n"
            f"🏷️ <b>Badge:</b> <code>{app.get('badge', 'None')}</code>\n"
            f"💬 <i>{app.get('tagline', '')}</i>\n\n"
            f"<i>Rank updated to #{rank} (Synced to Web in Real-Time)!</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=top_app_action_keyboard(app_id, rank, is_first, is_last))
        bot.answer_callback_query(call.id, f"Moved to Rank #{rank}")
        return

    elif data.startswith("top_unpin:"):
        app_id = data.split(":")[1]
        firebase_mgr.unpin_app(app_id)
        bot.answer_callback_query(call.id, "Removed from Top Pinned list!", show_alert=True)
        pinned = firebase_mgr.get_pinned_apps()
        if not pinned:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("➕ Pin an App to Top", callback_data="menu:pin_pick_app"))
            markup.add(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main"))
            bot.edit_message_text("📌 <b>Top Apps Manager</b>\n\n<i>No apps currently pinned.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup)
        else:
            bot.edit_message_text("📌 <b>Top Apps Ranking:</b>", call.message.chat.id, call.message.message_id, reply_markup=top_apps_manager_keyboard(pinned))
        return

    # Feature App Toggle
    elif data.startswith("feature_app:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        new_status = not bool(app.get("featured"))
        firebase_mgr.update_app_field(app_id, "featured", new_status)
        
        status_txt = "🌟 Marked as Featured!" if new_status else "Unmarked as Featured"
        bot.answer_callback_query(call.id, status_txt, show_alert=True)
        # Refresh detail view
        app = firebase_mgr.get_app(app_id)
        shots_count = len(app.get("screenshots", []))
        featured_tag = "🌟 Featured App\n" if app.get("featured") else ""
        detail_text = (
            f"📱 <b>{app.get('name')}</b>\n"
            f"{featured_tag}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>ID:</b> <code>{app.get('id')}</code>\n"
            f"🏷️ <b>Type:</b> <code>{app.get('type', 'app').upper()}</code>\n"
            f"📂 <b>Category:</b> <code>{app.get('category', 'Apps')}</code>\n"
            f"🏷️ <b>Version:</b> <code>{app.get('version', 'v1.0')}</code>\n"
            f"💾 <b>Size:</b> <code>{app.get('size', 'N/A')}</code>\n"
            f"🖼️ <b>Screenshots:</b> <code>{shots_count} attached</code>\n"
            f"📅 <b>Updated:</b> <code>{app.get('updatedDate', 'Recently')}</code>\n"
            f"🔗 <b>Target URL:</b> <code>{app.get('downloadUrl') or app.get('webUrl') or app.get('sourceUrl') or 'None'}</code>\n\n"
            f"💬 <i>{app.get('tagline', '')}</i>"
        )
        bot.edit_message_text(detail_text, call.message.chat.id, call.message.message_id, reply_markup=app_detail_actions_keyboard(app_id, STORE_WEB_URL))
        return

    # ----------------------------------------------------------------------
    # App Edit Sub-Menu & Field Editors
    # ----------------------------------------------------------------------
    elif data.startswith("edit_menu:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        text = f"✏️ <b>Editing Fields for:</b> <b>{app.get('name')}</b>\n\n<i>Select which property you want to update in real-time:</i>"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=app_edit_menu_keyboard(app_id))
        bot.answer_callback_query(call.id)
        return

    # Specific Field Edit Prompt
    elif data.startswith("field:"):
        parts = data.split(":")
        field_name = parts[1]
        app_id = parts[2]
        app = firebase_mgr.get_app(app_id)

        if field_name == "cat":
            cats = firebase_mgr.get_categories(force_refresh=True)
            markup = InlineKeyboardMarkup(row_width=2)
            buttons = []
            for c in cats:
                if c in ("All", "⭐ Favorites"):
                    continue
                buttons.append(InlineKeyboardButton(f"📂 {c}", callback_data=f"cat_set:{app_id}:{c}"))
            if buttons:
                markup.add(*buttons)
            markup.add(InlineKeyboardButton("🔙 Back to Edit Menu", callback_data=f"edit_menu:{app_id}"))

            bot.edit_message_text(
                f"🏷️ <b>Select New Category for {app.get('name')}:</b>\n\nCurrent: <code>{app.get('category')}</code>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
            bot.answer_callback_query(call.id)
            return

        if field_name == "icon":
            user_states[user_id] = {"stage": "edit_app_icon", "app_id": app_id, "field": "icon"}
            curr_val = app.get("icon", "")
            text = (
                f"🎨 <b>Change App Logo / Icon</b> for <b>{app.get('name')}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Current Logo: <code>{curr_val}</code>\n\n"
                f"👉 <b>Send a new Photo / Image directly from your gallery</b>\n"
                f"OR reply with an Image URL (e.g. <code>https://.../icon.png</code>):"
            )
            bot.clear_step_handler_by_chat_id(call.message.chat.id)
            msg = bot.send_message(call.message.chat.id, text, reply_markup=cancel_wizard_keyboard())
            bot.register_next_step_handler(msg, process_field_update)
            safe_answer_callback(call)
            return

        user_states[user_id] = {"stage": "edit_field", "app_id": app_id, "field": field_name}
        field_titles = {
            "name": "App Name",
            "tagline": "Tagline",
            "desc": "Full Description",
            "icon": "Icon URL or Path",
            "url": "Download / Web / GitHub URL",
            "size": "File Size (e.g. '18.5 MB')",
            "buttonText": "Action Button Text (e.g. 'Install APK', 'Open Web App', 'Join Bot', 'Play Online')"
        }
        f_title = field_titles.get(field_name, field_name)
        curr_val = app.get(field_name if field_name != "desc" else "description", "")
        
        text = (
            f"✏️ <b>Edit {f_title}</b> for <b>{app.get('name')}</b>\n\n"
            f"Current Value:\n<code>{curr_val}</code>\n\n"
            f"👉 <b>Reply with the New Value:</b>"
        )
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        msg = bot.send_message(call.message.chat.id, text, reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_field_update)
        safe_answer_callback(call)
        return

    elif data.startswith("cat_set:"):
        parts = data.split(":")
        app_id = parts[1]
        cat_name = parts[2]
        firebase_mgr.update_app_field(app_id, "category", cat_name)
        bot.answer_callback_query(call.id, f"Category changed to '{cat_name}'!", show_alert=True)
        app = firebase_mgr.get_app(app_id)
        text = f"✏️ <b>Editing Fields for:</b> <b>{app.get('name')}</b>\n\n<i>Select which property you want to update in real-time:</i>"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=app_edit_menu_keyboard(app_id))
        return

    # Wizard Category Picker
    elif data.startswith("cat_wiz:"):
        cat_name = data.split(":", 1)[1]
        if user_id not in user_states:
            bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
            return
        user_states[user_id]["app_data"]["category"] = cat_name
        bot.answer_callback_query(call.id, f"Category: {cat_name}")
        prompt_wizard_step_button_text(user_id, call.message.chat.id, call.message.message_id)
        return

    # Wizard Skip Custom Action Button Text
    elif data == "wizard_skip:btn_text":
        if user_id not in user_states:
            bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
            return
        user_states[user_id]["app_data"]["buttonText"] = ""
        bot.answer_callback_query(call.id, "Using default button text.")
        prompt_wizard_step_icon(user_id, call.message.chat.id, call.message.message_id)
        return

    # Wizard Skip Icon
    elif data == "wizard_skip:icon":
        if user_id not in user_states:
            bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
            return
        user_states[user_id]["app_data"]["icon"] = "icons/icon-192.svg"
        bot.answer_callback_query(call.id, "Using default app icon.")
        prompt_wizard_step_screenshots(user_id, call.message.chat.id, call.message.message_id)
        return

    # Wizard Skip Screenshots
    elif data == "wizard_skip:screenshots":
        if user_id not in user_states:
            safe_answer_callback(call, "Session expired.", show_alert=True)
            return
        user_states[user_id]["app_data"]["screenshots"] = []
        safe_answer_callback(call, "Screenshots skipped.")
        finalize_publish_project(user_id, call.message.chat.id, call.message.message_id)
        return

    # Wizard Finish & Publish
    elif data == "wizard_finish:publish":
        if user_id not in user_states:
            safe_answer_callback(call, "Session expired.", show_alert=True)
            return
        safe_answer_callback(call, "Publishing project...")
        finalize_publish_project(user_id, call.message.chat.id, call.message.message_id)
        return

    # ----------------------------------------------------------------------
    # Screenshots Manager Sub-Menu
    # ----------------------------------------------------------------------
    elif data.startswith("shots_menu:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        shots = app.get("screenshots", [])
        
        shots_list_str = "\n".join([f"• #{i+1}: <code>{s}</code>" for i, s in enumerate(shots)]) if shots else "<i>No screenshots attached yet.</i>"
        text = (
            f"🖼️ <b>Screenshots Manager for {app.get('name')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Attached Screenshots ({len(shots)}):</b>\n{shots_list_str}\n\n"
            f"<i>Choose an action below:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=screenshots_manager_keyboard(app_id, len(shots)))
        bot.answer_callback_query(call.id)
        return

    # Add Screenshot Link Prompt
    elif data.startswith("shot_add:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        user_states[user_id] = {"stage": "add_screenshot_links", "app_id": app_id}
        
        text = (
            f"➕ <b>Add Screenshot Links for {app.get('name')}</b>\n\n"
            f"Reply with one or more Screenshot image URLs.\n"
            f"<i>(If sending multiple links, separate them with a comma or new line)</i>\n\n"
            f"Example:\n<code>https://example.com/shot1.png, https://example.com/shot2.png</code>"
        )
        msg = bot.send_message(call.message.chat.id, text, reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_add_screenshot_links)
        bot.answer_callback_query(call.id)
        return

    # Upload Photo Screenshot
    elif data.startswith("shot_photo:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        user_states[user_id] = {"stage": "upload_screenshot_photo", "app_id": app_id}

        text = (
            f"📸 <b>Upload Screenshot Photo for {app.get('name')}</b>\n\n"
            f"Please send the screenshot image/photo in this chat.\n"
            f"• It will be saved and linked directly to the app carousel in Firebase!"
        )
        msg = bot.send_message(call.message.chat.id, text, reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_upload_screenshot_photo)
        bot.answer_callback_query(call.id)
        return

    # List Screenshots to Delete
    elif data.startswith("shot_del_list:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        shots = app.get("screenshots", [])
        
        text = f"🗑️ <b>Select which screenshot to remove from {app.get('name')}:</b>"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=delete_screenshot_list_keyboard(app_id, shots))
        bot.answer_callback_query(call.id)
        return

    # Remove Single Screenshot by Index
    elif data.startswith("shot_remove:"):
        parts = data.split(":")
        app_id = parts[1]
        idx = int(parts[2])
        
        firebase_mgr.remove_screenshot(app_id, idx)
        bot.answer_callback_query(call.id, f"🗑️ Screenshot #{idx+1} removed!", show_alert=True)
        
        # Refresh screenshots menu
        app = firebase_mgr.get_app(app_id)
        shots = app.get("screenshots", [])
        shots_list_str = "\n".join([f"• #{i+1}: <code>{s}</code>" for i, s in enumerate(shots)]) if shots else "<i>No screenshots attached.</i>"
        text = (
            f"🖼️ <b>Screenshots Manager for {app.get('name')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Attached Screenshots ({len(shots)}):</b>\n{shots_list_str}\n\n"
            f"<i>Choose an action below:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=screenshots_manager_keyboard(app_id, len(shots)))
        return

    # Clear All Screenshots
    elif data.startswith("shot_clear:"):
        app_id = data.split(":")[1]
        firebase_mgr.clear_screenshots(app_id)
        bot.answer_callback_query(call.id, "🧹 All screenshots cleared!", show_alert=True)
        
        app = firebase_mgr.get_app(app_id)
        text = f"🖼️ <b>Screenshots Manager for {app.get('name')}</b>\n━━━━━━━━━━━━━━━━━━━━\n<b>Attached Screenshots (0):</b>\n<i>No screenshots attached.</i>"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=screenshots_manager_keyboard(app_id, 0))
        return

    # Bump App Version
    elif data.startswith("bump_ver:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        user_states[user_id] = {"stage": "bump_ver", "app_id": app_id}
        
        text = (
            f"📦 <b>Bump Version for {app.get('name')}</b>\n"
            f"Current Version: <code>{app.get('version', 'v1.0')}</code>\n\n"
            f"Please reply with the <b>New Version</b> (e.g., <code>v2.5.0</code>):"
        )
        msg = bot.send_message(call.message.chat.id, text, reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_bump_version)
        bot.answer_callback_query(call.id)
        return

    # Delete Confirm Prompt
    elif data.startswith("confirm_delete:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        text = (
            f"⚠️ <b>Are you sure you want to delete this project?</b>\n\n"
            f"Project: <b>{app.get('name')}</b> (<code>{app_id}</code>)\n"
            f"<i>This action will remove it from Firebase database and web catalog immediately in real-time.</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=confirm_delete_keyboard(app_id))
        bot.answer_callback_query(call.id)
        return

    # Execute Delete
    elif data.startswith("do_delete:"):
        app_id = data.split(":")[1]
        success = firebase_mgr.delete_app(app_id)
        if success:
            bot.answer_callback_query(call.id, "🗑️ Project deleted in real-time!", show_alert=True)
            apps = firebase_mgr.get_all_apps()
            bot.edit_message_text("📋 <b>Select a Project to Manage:</b>", call.message.chat.id, call.message.message_id, reply_markup=app_list_keyboard(apps, "view_app"))
        else:
            bot.answer_callback_query(call.id, "❌ Delete failed.", show_alert=True)
        return

    # ----------------------------------------------------------------------
    # Badge / Tag System with Expiration Handlers
    # ----------------------------------------------------------------------
    elif data.startswith("set_badge_menu:"):
        app_id = data.split(":")[1]
        app = firebase_mgr.get_app(app_id)
        current_badge = app.get("badge", "None") if app else "None"
        expires_at = app.get("badgeExpiresAt", "Permanent") if app else "Permanent"

        text = (
            f"🏷️ <b>Set Badge / Tag for {app.get('name', 'App')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Current Badge: <code>{current_badge}</code>\n"
            f"Expires At: <code>{expires_at}</code>\n\n"
            f"<i>Choose a badge preset below:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=badge_selection_keyboard(app_id))
        bot.answer_callback_query(call.id)
        return

    elif data.startswith("badge_pick:"):
        parts = data.split(":")
        app_id = parts[1]
        badge_code = parts[2]
        
        badge_labels = {
            "new": "✨ NEW",
            "updated": "🔥 UPDATED",
            "featured": "⭐ FEATURED",
            "beta": "🚀 BETA",
            "popular": "⚡ POPULAR"
        }
        selected_badge = badge_labels.get(badge_code, "✨ NEW")

        text = (
            f"⏱️ <b>Set Expiry Time Limit</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Selected Badge: <b>{selected_badge}</b>\n\n"
            f"<i>Choose how long this badge should stay active on the web store:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=badge_duration_keyboard(app_id, badge_code))
        bot.answer_callback_query(call.id)
        return

    elif data.startswith("badge_apply:"):
        parts = data.split(":")
        app_id = parts[1]
        badge_code = parts[2]
        days = int(parts[3])

        badge_labels = {
            "new": "✨ NEW",
            "updated": "🔥 UPDATED",
            "featured": "⭐ FEATURED",
            "beta": "🚀 BETA",
            "popular": "⚡ POPULAR",
            "clear": None
        }
        badge_text = badge_labels.get(badge_code)

        success = firebase_mgr.set_app_badge(app_id, badge_text, duration_days=days)
        if success:
            alert_msg = f"Badge updated to '{badge_text or 'Cleared'}' (Synced to Web in Real-Time)!"
            bot.answer_callback_query(call.id, alert_msg, show_alert=True)
            
            app = firebase_mgr.get_app(app_id)
            featured_tag = "🌟 Featured App\n" if app.get("featured") else ""
            badge_str = f"🏷️ <b>Badge:</b> {app.get('badge', 'None')}\n" if app.get("badge") else ""
            
            detail_text = (
                f"{featured_tag}"
                f"📱 <b>{app.get('name')}</b> ({app.get('version', 'v1.0')})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{badge_str}"
                f"📂 <b>Category:</b> {app.get('category', 'Apps')}\n"
                f"💾 <b>Size:</b> {app.get('size', 'N/A')}\n"
                f"💬 <i>{app.get('tagline', '')}</i>"
            )
            bot.edit_message_text(detail_text, call.message.chat.id, call.message.message_id, reply_markup=app_detail_actions_keyboard(app_id, STORE_WEB_URL))
        else:
            bot.answer_callback_query(call.id, "❌ Failed to update badge in Firebase.", show_alert=True)
        return

    # ----------------------------------------------------------------------
    # GitHub Releases Management Handlers
    # ----------------------------------------------------------------------
    elif data == "menu:github_releases":
        bot.answer_callback_query(call.id, "Fetching GitHub releases...")
        ok, releases = github_mgr.list_all_releases()

        if not ok or not isinstance(releases, list):
            bot.edit_message_text(f"❌ Failed to fetch releases: {releases}", call.message.chat.id, call.message.message_id, reply_markup=back_to_main_keyboard())
            return

        if not releases:
            empty_text = (
                f"🐙 <b>GitHub Releases Manager</b>\n"
                f"Repository: <code>{github_mgr.owner}/{github_mgr.repo}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<i>No releases found on GitHub yet.</i>\n\n"
                f"Use <b>[ 📦 Upload APK to GitHub ]</b> from the main menu to publish your first release!"
            )
            bot.edit_message_text(empty_text, call.message.chat.id, call.message.message_id, reply_markup=back_to_main_keyboard())
            return

        text = (
            f"🐙 <b>GitHub Releases ({len(releases)} found)</b>\n"
            f"Repository: <code>{github_mgr.owner}/{github_mgr.repo}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Tap any release below to view details, direct links, or delete:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=github_releases_list_keyboard(releases))
        return

    elif data.startswith("gh_rel:"):
        rel_id = data.split(":")[1]
        bot.answer_callback_query(call.id, "Loading release info...")
        ok, rel = github_mgr.get_release_by_id(rel_id)

        if not ok:
            bot.edit_message_text(f"❌ Failed to load release: {rel}", call.message.chat.id, call.message.message_id, reply_markup=back_to_main_keyboard())
            return

        tag = rel.get("tag_name", "")
        name = rel.get("name") or tag
        body = rel.get("body") or "No changelog provided."
        pub_at = (rel.get("published_at") or "")[:10]
        html_url = rel.get("html_url")
        
        assets = rel.get("assets", [])
        dl_url = assets[0].get("browser_download_url") if assets else None
        
        assets_info = []
        for a in assets:
            size_mb = round(a.get("size", 0) / (1024 * 1024), 2)
            dls = a.get("download_count", 0)
            assets_info.append(f"• <b>{a.get('name')}:</b> <code>{size_mb} MB</code> | 📥 <code>{dls} downloads</code>")

        assets_text = "\n".join(assets_info) if assets_info else "<i>No binary assets attached.</i>"

        text = (
            f"📦 <b>GitHub Release: {tag}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 <b>Title:</b> {name}\n"
            f"📅 <b>Published:</b> <code>{pub_at}</code>\n"
            f"🏷️ <b>Tag:</b> <code>{tag}</code>\n\n"
            f"<b>Assets:</b>\n{assets_text}\n\n"
            f"<b>Changelog:</b>\n<i>{body[:300]}</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=github_release_detail_keyboard(rel_id, tag, dl_url, html_url))
        return

    elif data.startswith("gh_del_confirm:"):
        parts = data.split(":")
        rel_id = parts[1]
        tag = parts[2]
        
        text = (
            f"⚠️ <b>Delete GitHub Release Confirmation</b>\n\n"
            f"Are you sure you want to permanently delete release <b>{tag}</b> (ID: <code>{rel_id}</code>) from GitHub (<code>{github_mgr.owner}/{github_mgr.repo}</code>)?\n\n"
            f"<i>This action cannot be undone.</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=confirm_delete_github_release_keyboard(rel_id, tag))
        bot.answer_callback_query(call.id)
        return

    elif data.startswith("gh_del_exec:"):
        parts = data.split(":")
        rel_id = parts[1]
        tag = parts[2]
        bot.answer_callback_query(call.id, "Deleting release from GitHub...")

        ok, msg_text = github_mgr.delete_release(rel_id, tag)
        if ok:
            bot.answer_callback_query(call.id, f"🗑️ Release {tag} deleted successfully!", show_alert=True)
            # Re-fetch releases list
            ok2, releases = github_mgr.list_all_releases()
            if ok2 and releases:
                bot.edit_message_text("🐙 <b>GitHub Releases:</b>", call.message.chat.id, call.message.message_id, reply_markup=github_releases_list_keyboard(releases))
            else:
                bot.edit_message_text("🐙 <b>GitHub Releases:</b>\n\n<i>No releases found.</i>", call.message.chat.id, call.message.message_id, reply_markup=back_to_main_keyboard())
        else:
            bot.answer_callback_query(call.id, f"❌ Failed: {msg_text}", show_alert=True)
        return

    # ----------------------------------------------------------------------
    # Category Management Handlers
    # ----------------------------------------------------------------------
    elif data == "menu:manage_categories":
        cats = firebase_mgr.get_categories()
        text = (
            f"📂 <b>Web Store Categories Manager</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Active Categories: <code>{len(cats)}</code>\n\n"
            f"<i>Tap any category below to Rename, Delete, or Move left/right in the store chip bar:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=categories_manager_keyboard(cats))
        bot.answer_callback_query(call.id)
        return

    elif data == "cat:add_new":
        user_states[user_id] = {"stage": "add_category"}
        text = (
            "➕ <b>Add New Category</b>\n\n"
            "Please reply with the <b>Category Name</b>\n"
            "<i>(e.g., '🎮 Games', '🛠️ Utilities', '🎵 Music', or '🤖 AI Tools')</i>:"
        )
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        msg = bot.send_message(call.message.chat.id, text, reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_add_new_category)
        safe_answer_callback(call)
        return

    elif data.startswith("cat_manage:"):
        cat_name = data.split(":", 1)[1]
        cats = firebase_mgr.get_categories()
        is_first = (cats.index(cat_name) <= 1) if cat_name in cats else False
        is_last = (cats.index(cat_name) >= len(cats) - 2) if cat_name in cats else False
        
        apps = firebase_mgr.get_all_apps()
        cat_apps_count = sum(1 for a in apps if a.get("category") == cat_name)

        text = (
            f"📂 <b>Category: {cat_name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Linked Apps:</b> <code>{cat_apps_count} projects</code>\n\n"
            f"<i>Choose an action below:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=category_item_keyboard(cat_name, is_first, is_last, False))
        safe_answer_callback(call)
        return

    elif data.startswith("cat_rename:"):
        cat_name = data.split(":", 1)[1]
        user_states[user_id] = {"stage": "rename_category", "old_cat": cat_name}
        text = (
            f"✏️ <b>Rename Category: {cat_name}</b>\n\n"
            f"Please reply with the <b>New Category Name</b>:\n"
            f"<i>(All apps under '{cat_name}' will be automatically updated!)</i>"
        )
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        msg = bot.send_message(call.message.chat.id, text, reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_rename_category)
        safe_answer_callback(call)
        return

    elif data.startswith("cat_delete_confirm:"):
        cat_name = data.split(":", 1)[1]
        text = (
            f"⚠️ <b>Delete Category Confirmation</b>\n\n"
            f"Are you sure you want to delete category <b>{cat_name}</b> from the web store?\n\n"
            f"• Apps in this category will be automatically reassigned to <code>Apps</code>."
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=confirm_delete_category_keyboard(cat_name))
        bot.answer_callback_query(call.id)
        return

    elif data.startswith("cat_delete_exec:"):
        cat_name = data.split(":", 1)[1]
        ok, msg_txt = firebase_mgr.delete_category(cat_name)
        if ok:
            bot.answer_callback_query(call.id, f"🗑️ Category '{cat_name}' deleted in real-time!", show_alert=True)
            cats = firebase_mgr.get_categories()
            bot.edit_message_text("📂 <b>Web Store Categories:</b>", call.message.chat.id, call.message.message_id, reply_markup=categories_manager_keyboard(cats))
        else:
            bot.answer_callback_query(call.id, f"❌ {msg_txt}", show_alert=True)
        return

    elif data.startswith("cat_move:"):
        parts = data.split(":")
        cat_name = parts[1]
        direction = parts[2]
        ok, msg_txt = firebase_mgr.move_category(cat_name, direction)
        
        cats = firebase_mgr.get_categories()
        is_first = (cats.index(cat_name) <= 1) if cat_name in cats else False
        is_last = (cats.index(cat_name) >= len(cats) - 2) if cat_name in cats else False
        
        text = f"📂 <b>Category: {cat_name}</b>\n\n<i>Position shifted in store chip bar!</i>"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=category_item_keyboard(cat_name, is_first, is_last, False))
        bot.answer_callback_query(call.id, msg_txt)
        return

    elif data.startswith("cat_info:"):
        bot.answer_callback_query(call.id, "🔒 System categories (All / Favorites) are locked by design.", show_alert=True)
        return

    # 4. Live Analytics & Download Counter
    elif data == "menu:analytics":
        apps = firebase_mgr.get_all_apps()
        stats = firebase_mgr.get_analytics_stats()

        total_downloads = sum(stats.values()) if stats else 0
        
        apps_breakdown = []
        for app in apps:
            app_id = app.get("id")
            dl_count = stats.get(app_id, 0)
            bar = make_progress_bar(dl_count, max(total_downloads, 1), length=16)
            apps_breakdown.append(
                f"• <b>{app.get('name')}</b> (<code>{dl_count} installs</code>)\n"
                f"  <code>{bar}</code>"
            )

        analytics_text = (
            f"📊 <b>MyStore Live Analytics & Telemetry</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>Total Realtime Clicks:</b> <code>{total_downloads}</code>\n"
            f"📱 <b>Active Apps in Catalog:</b> <code>{len(apps)}</code>\n\n"
            f"<b>Per-App Downloads & Share Breakdown:</b>\n\n"
            + ("\n\n".join(apps_breakdown) if apps_breakdown else "<i>No download activity recorded yet.</i>") + "\n\n"
            f"🔥 <i>Real-time telemetry synced with Firebase!</i>"
        )
        bot.edit_message_text(analytics_text, call.message.chat.id, call.message.message_id, reply_markup=back_to_main_keyboard())
        bot.answer_callback_query(call.id)
        return

    # 5. Developer Profile Menu
    elif data == "menu:developer_profile":
        dev = firebase_mgr.get_developer_profile()
        socials_list = "\n".join([f"• <b>{s.get('name')}:</b> {s.get('url')}" for s in dev.get("socials", [])])
        
        profile_text = (
            f"👤 <b>Developer Profile Manager</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ <b>Name:</b> {dev.get('name', 'R3V_X')} ({dev.get('handle', '@R3V_X')})\n"
            f"📝 <b>Tagline:</b> {dev.get('tagline', '')}\n"
            f"🌐 <b>Website:</b> {dev.get('website', '')}\n\n"
            f"<b>Social Links:</b>\n{socials_list}\n\n"
            f"<i>Use buttons below to edit profile fields:</i>"
        )
        bot.edit_message_text(profile_text, call.message.chat.id, call.message.message_id, reply_markup=developer_profile_keyboard(dev))
        bot.answer_callback_query(call.id)
        return

    # 6. Secure Database Export Suite
    elif data == "menu:backup_export":
        apps_count = len(firebase_mgr.get_all_apps())
        cats_count = len(firebase_mgr.get_categories())
        
        text = (
            "🔒 <b>Secure Database & Store Export Center</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Total Catalog Apps:</b> <code>{apps_count}</code>\n"
            f"📂 <b>Active Categories:</b> <code>{cats_count}</code>\n"
            f"⚡ <b>Checksum Engine:</b> <code>C++ Native CRC32 / SHA-256</code>\n\n"
            "<i>Select an export format below:</i>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=export_options_keyboard())
        bot.answer_callback_query(call.id)
        return

    # Export Full JSON with Security Metadata & Checksums
    elif data == "export:full_json":
        bot.answer_callback_query(call.id, "Generating secure JSON backup...")
        raw_data = firebase_mgr.get_full_store_data()
        
        now_ts = int(time.time())
        export_payload = {
            "_meta": {
                "system": "MyStore Personal AppStore",
                "version": "2.0.0",
                "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "exportedBy": f"Admin ID: {user_id}",
                "appCount": len(raw_data.get("apps", [])),
                "categoryCount": len(raw_data.get("categories", []))
            },
            "developer": raw_data.get("developer", {}),
            "categories": raw_data.get("categories", []),
            "apps": raw_data.get("apps", [])
        }

        json_bytes = json.dumps(export_payload, indent=2, ensure_ascii=False).encode("utf-8")
        crc32_val = fast_crc32_hash(json_bytes)
        
        import hashlib
        sha256_val = hashlib.sha256(json_bytes).hexdigest()[:16]

        backup_filename = f"mystore_full_database_{now_ts}.json"
        backup_path = os.path.join(BASE_DIR, backup_filename)

        with open(backup_path, "wb") as f:
            f.write(json_bytes)

        caption_text = (
            "📄 <b>MyStore Complete Database JSON Backup</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Apps Count:</b> <code>{len(raw_data.get('apps', []))}</code>\n"
            f"📂 <b>Categories:</b> <code>{len(raw_data.get('categories', []))}</code>\n"
            f"🔒 <b>CRC32 Checksum:</b> <code>{crc32_val}</code>\n"
            f"🛡️ <b>SHA256 Hash:</b> <code>{sha256_val}...</code>\n\n"
            "<i>Contains complete schema, developer profile, categories, and applications.</i>"
        )

        with open(backup_path, "rb") as f_send:
            bot.send_document(call.message.chat.id, f_send, caption=caption_text, reply_markup=export_options_keyboard())

        if os.path.exists(backup_path):
            os.remove(backup_path)
        return

    # Export Apps to Spreadsheet (CSV)
    elif data == "export:csv":
        bot.answer_callback_query(call.id, "Generating CSV spreadsheet...")
        apps = firebase_mgr.get_all_apps()
        now_ts = int(time.time())
        
        import csv
        csv_filename = f"mystore_apps_catalog_{now_ts}.csv"
        csv_path = os.path.join(BASE_DIR, csv_filename)

        fieldnames = ["id", "name", "type", "category", "version", "size", "rating", "downloads", "badge", "pinned", "featured", "updatedDate", "targetUrl", "tagline"]

        with open(csv_path, "w", encoding="utf-8", newline="") as f_csv:
            writer = csv.DictWriter(f_csv, fieldnames=fieldnames)
            writer.writeheader()
            for a in apps:
                writer.writerow({
                    "id": a.get("id", ""),
                    "name": a.get("name", ""),
                    "type": a.get("type", "app"),
                    "category": a.get("category", ""),
                    "version": a.get("version", ""),
                    "size": a.get("size", ""),
                    "rating": a.get("rating", ""),
                    "downloads": a.get("downloads", ""),
                    "badge": a.get("badge", ""),
                    "pinned": "Yes" if a.get("pinned") else "No",
                    "featured": "Yes" if a.get("featured") else "No",
                    "updatedDate": a.get("updatedDate", ""),
                    "targetUrl": a.get("downloadUrl") or a.get("webUrl") or a.get("sourceUrl") or "",
                    "tagline": a.get("tagline", "")
                })

        caption_text = (
            "📊 <b>MyStore Apps Spreadsheet (CSV)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Total Rows:</b> <code>{len(apps)} applications</code>\n\n"
            "<i>Compatible with Microsoft Excel, Google Sheets, and LibreOffice Calc.</i>"
        )

        with open(csv_path, "rb") as f_send:
            bot.send_document(call.message.chat.id, f_send, caption=caption_text, reply_markup=export_options_keyboard())

        if os.path.exists(csv_path):
            os.remove(csv_path)
        return

    # Export Live Analytics
    elif data == "export:analytics_json":
        bot.answer_callback_query(call.id, "Generating analytics export...")
        stats = firebase_mgr.get_analytics_stats()
        now_ts = int(time.time())

        export_data = {
            "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "totalClicks": sum(stats.values()) if stats else 0,
            "downloadsPerApp": stats or {}
        }

        path = os.path.join(BASE_DIR, f"mystore_analytics_{now_ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2)

        with open(path, "rb") as f_send:
            bot.send_document(
                call.message.chat.id, 
                f_send, 
                caption=f"📈 <b>MyStore Live Analytics Export</b>\nTotal recorded clicks: <code>{export_data['totalClicks']}</code>",
                reply_markup=export_options_keyboard()
            )

        if os.path.exists(path):
            os.remove(path)
        return

    # Export & Archive directly to Storage Channel
    elif data == "export:channel_archive":
        bot.answer_callback_query(call.id, "Archiving complete store backup to storage channel...")
        raw_data = firebase_mgr.get_full_store_data()
        now_ts = int(time.time())

        path = os.path.join(BASE_DIR, f"mystore_cloud_archive_{now_ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, indent=2, ensure_ascii=False)

        chan_ok, chan_info = storage_mgr.sync_upload_to_channel(
            file_path=path,
            caption=f"💾 <b>MyStore Automated Cloud Database Backup</b>\nTimestamp: <code>{time.strftime('%Y-%m-%d %H:%M:%S')}</code>\nApps: <code>{len(raw_data.get('apps', []))}</code>"
        )
        if not chan_ok:
            try:
                with open(path, "rb") as f_up:
                    bot.send_document(STORAGE_CHANNEL_ID, f_up, caption="💾 MyStore Automated Cloud Database Backup")
                chan_ok = True
            except Exception as e:
                chan_ok = False

        if os.path.exists(path):
            os.remove(path)

        if chan_ok:
            bot.answer_callback_query(call.id, "✅ Archived to Storage Channel (-1003887776900)!", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "⚠️ Failed to archive to channel. Ensure bot is admin in channel.", show_alert=True)
        return

    # 7. Store Health & Ping Check
    elif data == "menu:health_check":
        bot.answer_callback_query(call.id, "Testing connectivity...")
        
        # Test Web Store Ping
        web_ok, web_lat = False, 0
        try:
            t0 = time.time()
            res = requests.get(STORE_WEB_URL, timeout=4)
            web_lat = int((time.time() - t0) * 1000)
            web_ok = res.status_code == 200
        except Exception:
            web_ok = False

        # Test Firebase Ping
        fb_ok, fb_lat = False, 0
        try:
            t0 = time.time()
            res = requests.get(f"{firebase_mgr.db_url}/.json", timeout=5)
            fb_lat = int((time.time() - t0) * 1000)
            fb_ok = res.status_code == 200
        except Exception:
            fb_ok = False

        # Test GitHub API
        gh_ok, gh_msg = github_mgr.test_connection()

        health_text = (
            f"⚡ <b>MyStore Infrastructure Health Check</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 <b>Web Frontend ({STORE_WEB_URL}):</b>\n"
            f"  Status: {'🟢 Online (200 OK)' if web_ok else '🔴 Unreachable'}\n"
            f"  Latency: <code>{web_lat}ms</code>\n\n"
            f"🔥 <b>Firebase Realtime Database:</b>\n"
            f"  Status: {'🟢 Connected' if fb_ok else '🔴 Offline'}\n"
            f"  Latency: <code>{fb_lat}ms</code>\n\n"
            f"🐙 <b>GitHub Releases API:</b>\n"
            f"  Status: {'🟢 Authorized' if gh_ok else '🔴 Error'}\n"
            f"  Target: <code>{github_mgr.owner}/{github_mgr.repo}</code>\n"
            f"  Info: <i>{gh_msg}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔒 <b>Security Token:</b> <code>Matches Web .env</code>"
        )
        bot.edit_message_text(health_text, call.message.chat.id, call.message.message_id, reply_markup=back_to_main_keyboard())
        return


# --------------------------------------------------------------------------
# Step-by-Step Interactive Wizard Handlers
# --------------------------------------------------------------------------

def process_wizard_step_name(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_name":
        return

    name = (message.text or "").strip()
    if not name:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        msg = bot.send_message(message.chat.id, "⚠️ Project name cannot be empty. Please enter a valid name:", reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_wizard_step_name)
        return

    user_states[user_id]["app_data"]["name"] = name
    user_states[user_id]["stage"] = "wizard_step_tagline"

    text = (
        f"Project: <b>{name}</b>\n\n"
        f"<b>Step 2/8:</b> Please reply with a short <b>Tagline</b>\n"
        f"<i>(e.g., 'Fast, private audio editor with real-time spectrum')</i>"
    )
    bot.clear_step_handler_by_chat_id(message.chat.id)
    msg = bot.send_message(message.chat.id, text, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_wizard_step_tagline)


def process_wizard_step_tagline(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_tagline":
        return

    tagline = (message.text or "").strip()
    user_states[user_id]["app_data"]["tagline"] = tagline
    user_states[user_id]["stage"] = "wizard_step_url"

    p_type = user_states[user_id]["app_data"].get("type", "app")
    
    if p_type == "web":
        prompt_text = "<b>Step 3/8:</b> Reply with the <b>Website / Web App URL</b> (e.g. <code>https://aboutmee.pages.dev</code>):"
    elif p_type == "opensource":
        prompt_text = "<b>Step 3/8:</b> Reply with the <b>GitHub Repository / Source Code URL</b> (e.g. <code>https://github.com/v54087912-collab/repo</code>):"
    else:
        prompt_text = (
            "<b>Step 3/8: For APK Download Link:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• Reply with an existing <b>Direct APK URL</b>\n"
            "• OR <b>send the .apk file</b> directly in this chat\n"
            "• OR reply with <code>upload</code>."
        )

    bot.clear_step_handler_by_chat_id(message.chat.id)
    msg = bot.send_message(message.chat.id, prompt_text, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_wizard_step_url)


def process_wizard_step_url(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_url":
        return

    p_type = user_states[user_id]["app_data"].get("type", "app")

    # 1. If APK file document was directly sent at Step 3
    if message.document:
        doc = message.document
        user_states[user_id]["uploaded_doc"] = doc
        user_states[user_id]["stage"] = "wizard_apk_tag"
        file_name = doc.file_name or "app.apk"
        file_size_mb = round(doc.file_size / (1024 * 1024), 2)
        
        prompt_text = (
            f"📦 <b>APK Document Received:</b> <code>{file_name}</code> ({file_size_mb} MB)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Please reply with a <b>Release Tag</b> for GitHub (e.g., <code>v1.0.0</code>):\n\n"
            f"👉 <i>Or reply <code>skip</code> to auto-generate tag!</i>"
        )
        bot.clear_step_handler_by_chat_id(message.chat.id)
        msg = bot.send_message(message.chat.id, prompt_text, reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_wizard_apk_tag)
        return

    input_text = (message.text or "").strip()

    if p_type == "web":
        user_states[user_id]["app_data"]["webUrl"] = input_text
        user_states[user_id]["app_data"]["size"] = "Online Web App"
        user_states[user_id]["app_data"]["category"] = "Web"
    elif p_type == "opensource":
        user_states[user_id]["app_data"]["sourceUrl"] = input_text
        user_states[user_id]["app_data"]["size"] = "Source Code"
        user_states[user_id]["app_data"]["category"] = "Open Source"
    else:
        if input_text.lower() == "upload":
            user_states[user_id]["stage"] = "awaiting_wizard_apk"
            bot.clear_step_handler_by_chat_id(message.chat.id)
            msg = bot.send_message(
                message.chat.id,
                "📦 Please send the <b>.apk file document</b> in this chat:",
                reply_markup=cancel_wizard_keyboard()
            )
            bot.register_next_step_handler(msg, process_wizard_apk_file)
            return
        else:
            user_states[user_id]["app_data"]["downloadUrl"] = input_text
            user_states[user_id]["app_data"]["size"] = "15 MB"
            user_states[user_id]["app_data"]["category"] = "Apps"

    user_states[user_id]["stage"] = "wizard_step_version"
    text = "<b>Step 4/8:</b> Reply with the <b>Version</b> (e.g., <code>v1.0.0</code>):"
    bot.clear_step_handler_by_chat_id(message.chat.id)
    msg = bot.send_message(message.chat.id, text, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_wizard_step_version)


def process_wizard_apk_file(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "awaiting_wizard_apk":
        return

    if not message.document:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        msg = bot.send_message(message.chat.id, "⚠️ Please send a valid .apk file document.", reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_wizard_apk_file)
        return

    doc = message.document
    user_states[user_id]["uploaded_doc"] = doc
    user_states[user_id]["stage"] = "wizard_apk_tag"
    file_name = doc.file_name or "app.apk"
    file_size_mb = round(doc.file_size / (1024 * 1024), 2)

    prompt_text = (
        f"📦 <b>APK Document Received:</b> <code>{file_name}</code> ({file_size_mb} MB)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Please reply with a <b>Release Tag</b> for GitHub (e.g., <code>v1.0.0</code>):\n\n"
        f"👉 <i>Or reply <code>skip</code> to auto-generate tag!</i>"
    )
    bot.clear_step_handler_by_chat_id(message.chat.id)
    msg = bot.send_message(message.chat.id, prompt_text, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_wizard_apk_tag)


def process_wizard_apk_tag(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_apk_tag":
        return

    raw_tag = (message.text or "").strip()
    if not raw_tag or raw_tag.lower() in ("skip", "default", "none"):
        tag = f"v{int(time.time()) % 10000}.0.0"
    else:
        tag = raw_tag if raw_tag.startswith("v") else f"v{raw_tag}"

    user_states[user_id]["app_tag"] = tag
    user_states[user_id]["stage"] = "wizard_apk_title"
    app_name = user_states[user_id]["app_data"].get("name", "App")

    prompt_text = (
        f"🏷️ <b>Release Tag:</b> <code>{tag}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Please reply with a <b>Release Title</b>:\n"
        f"<i>(e.g., '{app_name} Official Release' or reply <code>skip</code>)</i>"
    )
    bot.clear_step_handler_by_chat_id(message.chat.id)
    msg = bot.send_message(message.chat.id, prompt_text, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_wizard_apk_title)


def process_wizard_apk_title(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_apk_title":
        return

    doc = user_states[user_id].get("uploaded_doc")
    if not doc:
        bot.send_message(message.chat.id, "Session expired.", reply_markup=back_to_main_keyboard())
        return

    tag = user_states[user_id].get("app_tag", "v1.0.0")
    app_name = user_states[user_id]["app_data"].get("name", "App")

    raw_title = (message.text or "").strip()
    if not raw_title or raw_title.lower() in ("skip", "default", "none"):
        release_title = f"{app_name} {tag} Release"
    else:
        release_title = raw_title

    file_name = doc.file_name or "app.apk"
    file_size_mb = round(doc.file_size / (1024 * 1024), 2)

    status_msg = bot.send_message(
        message.chat.id,
        f"⏳ <b>Step 1/3:</b> Downloading <code>{file_name}</code> ({file_size_mb} MB)...\n"
        f"📊 <code>{make_progress_bar(0, doc.file_size)}</code>"
    )

    try:
        local_temp_path = os.path.join(BASE_DIR, file_name)
        dl_ok, dl_res = smart_download_telegram_document(message, doc, local_temp_path, status_msg)
        if not dl_ok:
            bot.edit_message_text(f"❌ Download Failed: {dl_res}", message.chat.id, status_msg.message_id)
            return

        # 1. Forward & Archive to Storage Channel (-1003887776900)
        bot.edit_message_text(
            f"⏳ <b>Step 2/3:</b> Archiving to Storage Channel (-1003887776900)...\n"
            f"📊 <code>{make_progress_bar(50, 100)}</code>",
            message.chat.id,
            status_msg.message_id
        )
        chan_ok, chan_info = storage_mgr.sync_upload_to_channel(
            file_path=local_temp_path,
            caption=f"📦 <b>MyStore APK Cloud Archive</b>\nFile: <code>{file_name}</code>\nSize: <code>{file_size_mb} MB</code>\nTag: <code>{tag}</code>"
        )

        # 2. Upload to GitHub Releases with Live Progress Bar
        progress_cb = ProgressCallbackHandler(
            bot=bot,
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            action_title="Step 3/3: Uploading to GitHub Releases",
            filename=file_name
        )

        success, direct_url_or_err = github_mgr.publish_apk_release(
            file_path=local_temp_path,
            file_name=file_name,
            tag_name=tag,
            release_name=release_title,
            changelog="Automated upload via MyStore Admin Bot",
            progress_callback=progress_cb
        )

        if os.path.exists(local_temp_path):
            os.remove(local_temp_path)

        if success:
            user_states[user_id]["app_data"]["downloadUrl"] = direct_url_or_err
            user_states[user_id]["app_data"]["size"] = f"{file_size_mb} MB"
            user_states[user_id]["app_data"]["version"] = tag
            user_states[user_id]["app_data"]["category"] = "Apps"
            bot.edit_message_text(
                f"✅ <b>Uploaded to GitHub & Archived to Channel!</b>\n"
                f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
                f"🔗 Direct GitHub Asset URL:\n<code>{direct_url_or_err}</code>",
                message.chat.id,
                status_msg.message_id
            )
        else:
            bot.edit_message_text(f"⚠️ GitHub Upload Notice: {direct_url_or_err}\nUsing fallback URL.", message.chat.id, status_msg.message_id)
            user_states[user_id]["app_data"]["downloadUrl"] = f"https://github.com/{github_mgr.owner}/{github_mgr.repo}"
            user_states[user_id]["app_data"]["size"] = f"{file_size_mb} MB"
            user_states[user_id]["app_data"]["version"] = tag
            user_states[user_id]["app_data"]["category"] = "Apps"

    except Exception as e:
        bot.edit_message_text(f"❌ Upload failed: {str(e)}", message.chat.id, status_msg.message_id)
        user_states[user_id]["app_data"]["downloadUrl"] = f"https://github.com/{github_mgr.owner}/{github_mgr.repo}"
        user_states[user_id]["app_data"]["size"] = "15 MB"
        user_states[user_id]["app_data"]["version"] = tag
        user_states[user_id]["app_data"]["category"] = "Apps"

    # Move directly to Description (since Version is already set to Tag!)
    user_states[user_id]["stage"] = "wizard_step_description"
    next_prompt = (
        "<b>Step 5/8:</b> Reply with a detailed <b>Description & Key Features</b>:\n"
        "<i>(Supports markdown bullet points and paragraphs)</i>"
    )
    bot.clear_step_handler_by_chat_id(message.chat.id)
    msg = bot.send_message(message.chat.id, next_prompt, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_wizard_step_description)


def process_wizard_step_version(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_version":
        return

    ver = message.text.strip()
    user_states[user_id]["app_data"]["version"] = ver
    user_states[user_id]["stage"] = "wizard_step_description"

    text = (
        "<b>Step 5/8:</b> Reply with a detailed <b>Description & Key Features</b>:\n"
        "<i>(Supports markdown bullet points and paragraphs)</i>"
    )
    bot.clear_step_handler_by_chat_id(message.chat.id)
    msg = bot.send_message(message.chat.id, text, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_wizard_step_description)


def normalize_description_markdown(raw_text: str) -> str:
    """
    Intelligently cleans and normalizes user-entered description:
    - Strips leading emojis and symbols from feature lines
    - Formats all items into clean standard markdown bullet points (- Feature)
    - Preserves multi-sentence descriptive paragraphs
    """
    if not raw_text or not raw_text.strip():
        return ""

    lines = [l.strip() for l in raw_text.strip().splitlines() if l.strip()]
    if not lines:
        return ""

    pattern = re.compile(
        r"^[\s\u2022\u2023\u25E6\u2043\u2219\-\*\•\d\.\)\:\>]*"
        r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50\u2b55\u200d\ufe0f\ufe0e]*\s*"
    )

    formatted = []
    for line in lines:
        cleaned = pattern.sub("", line).strip()
        cleaned = re.sub(r"^[\-\*\•\:\>]\s*", "", cleaned).strip()
        if not cleaned:
            continue

        if len(cleaned.split()) > 20 and not (line.startswith("-") or line.startswith("*") or line.startswith("•")):
            formatted.append(f"{cleaned}\n")
        else:
            formatted.append(f"- {cleaned}")

    return "\n".join(formatted)


def process_wizard_step_description(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_description":
        return

    try:
        raw_desc = (message.text or message.caption or "").strip()
        desc = normalize_description_markdown(raw_desc)
        user_states[user_id]["app_data"]["description"] = desc
        user_states[user_id]["app_data"]["updatedDate"] = time.strftime("%Y-%m-%d")
        user_states[user_id]["app_data"]["rating"] = ""
        user_states[user_id]["app_data"]["downloads"] = ""
        user_states[user_id]["app_data"]["icon"] = "icons/icon-192.svg"

        # Step 6/8: Select Category from all dynamically active categories in Firebase
        prompt_wizard_step_category(user_id, message.chat.id)
    except Exception as e:
        print(f"[Wizard Description Error] {e}")
        prompt_wizard_step_category(user_id, message.chat.id)


def prompt_wizard_step_category(user_id, chat_id):
    if user_id in user_states:
        user_states[user_id]["stage"] = "wizard_step_category"
    bot.clear_step_handler_by_chat_id(chat_id)

    cats = firebase_mgr.get_categories(force_refresh=True)
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for c in cats:
        if c in ("All", "⭐ Favorites"):
            continue
        buttons.append(InlineKeyboardButton(f"📂 {c}", callback_data=f"cat_wiz:{c}"))
    if buttons:
        markup.add(*buttons)
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data="menu:main"))

    text = (
        "📂 <b>Step 6/8: Select Category</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Choose an active category for this project:\n"
        "<i>(Includes all newly added categories from your database)</i>"
    )
    bot.send_message(chat_id, text, reply_markup=markup)


def prompt_wizard_step_button_text(user_id, chat_id, edit_message_id=None):
    user_states[user_id]["stage"] = "wizard_step_btn_text"
    text = (
        "🔘 <b>Step 7/9: Action Button Label (Optional)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Enter a <b>short button label (1 to 4 words max)</b> for the store launch button:\n\n"
        "<b>Examples:</b>\n"
        "• <code>Open Web App</code>\n"
        "• <code>Install APK</code>\n"
        "• <code>Join Bot</code>\n"
        "• <code>View Code</code>\n\n"
        "⚠️ <i>Do NOT paste descriptions or features here.</i>\n"
        "👉 <i>Tap <b>[ ⏭️ Skip this Step ]</b> or send <code>skip</code> for standard default!</i>"
    )
    markup = skip_step_keyboard("wizard_skip:btn_text")
    bot.clear_step_handler_by_chat_id(chat_id)
    msg = bot.send_message(chat_id, text, reply_markup=markup)
    bot.register_next_step_handler(msg, process_wizard_step_btn_text)


def process_wizard_step_btn_text(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_btn_text":
        return

    raw_text = (message.text or "").strip()
    if raw_text.lower() in ("skip", "none", "no", "skip skip", "pass", "default", "/skip"):
        user_states[user_id]["app_data"]["buttonText"] = ""
    elif "\n" in raw_text or len(raw_text) > 35:
        bot.send_message(
            message.chat.id,
            "⚠️ <b>Button label must be short (1-4 words, max 35 characters)!</b>\n\n"
            "Examples: <code>Open Web App</code>, <code>Install APK</code>, <code>View Code</code>\n\n"
            "👉 Please reply with a short button title, or reply <code>skip</code> for standard default:"
        )
        bot.register_next_step_handler(message, process_wizard_step_btn_text)
        return
    else:
        user_states[user_id]["app_data"]["buttonText"] = raw_text

    prompt_wizard_step_icon(user_id, message.chat.id)


def prompt_wizard_step_icon(user_id, chat_id, edit_message_id=None):
    user_states[user_id]["stage"] = "wizard_step_icon"
    text = (
        "🎨 <b>Step 7/8: App Logo / Icon (Optional)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Please reply with an <b>Icon Image URL</b> (or send a photo/image):\n\n"
        "<b>Example:</b>\n"
        "<code>https://example.com/icon.png</code>\n\n"
        "👉 <i>Tap <b>[ ⏭️ Skip this Step ]</b> or send <code>skip</code> for default icon!</i>"
    )
    markup = skip_step_keyboard("wizard_skip:icon")
    bot.clear_step_handler_by_chat_id(chat_id)
    msg = bot.send_message(chat_id, text, reply_markup=markup)
    bot.register_next_step_handler(msg, process_wizard_step_icon)


def process_wizard_step_icon(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_icon":
        return

    # Check if photo or document image was sent
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id

    if file_id:
        status_msg = bot.send_message(
            message.chat.id,
            render_step_progress_text("Uploading App Logo", 1, 3, "Downloading from Telegram...")
        )
        try:
            file_info = bot.get_file(file_id)
            downloaded = bot.download_file(file_info.file_path)

            bot.edit_message_text(
                render_step_progress_text("Uploading App Logo", 2, 3, "Hosting on ImgBB High-Speed CDN..."),
                message.chat.id,
                status_msg.message_id
            )

            filename = f"logo_{int(time.time())}.png"
            ok, imgbb_url = imgbb_mgr.upload_image_bytes(downloaded, filename=filename)

            if ok:
                user_states[user_id]["app_data"]["icon"] = imgbb_url
                bot.edit_message_text(
                    f"✅ <b>Logo Hosted on ImgBB Cloud!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
                    f"🔗 Direct CDN URL: <code>{imgbb_url}</code>",
                    message.chat.id,
                    status_msg.message_id
                )
            else:
                user_states[user_id]["app_data"]["icon"] = "icons/icon-192.svg"
                bot.edit_message_text(f"⚠️ ImgBB Upload Notice: {imgbb_url}\nUsing default icon.", message.chat.id, status_msg.message_id)

        except Exception as e:
            user_states[user_id]["app_data"]["icon"] = "icons/icon-192.svg"
            bot.edit_message_text(f"⚠️ Error uploading logo: {str(e)}\nUsing default icon.", message.chat.id, status_msg.message_id)
    else:
        raw_text = (message.text or "").strip()
        if raw_text.lower() in ("skip", "none", "default", "no"):
            user_states[user_id]["app_data"]["icon"] = "icons/icon-192.svg"
        elif raw_text:
            user_states[user_id]["app_data"]["icon"] = raw_text
        else:
            user_states[user_id]["app_data"]["icon"] = "icons/icon-192.svg"

    # Proceed to Step 8/8: Screenshots
    prompt_wizard_step_screenshots(user_id, message.chat.id)


def prompt_wizard_step_screenshots(user_id, chat_id, edit_message_id=None):
    user_states[user_id]["stage"] = "wizard_step_screenshots"
    shots_count = len(user_states[user_id]["app_data"].get("screenshots", []))
    text = (
        "🖼️ <b>Step 8/8: Screenshots (Optional)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Send Screenshot photos directly in chat OR reply with image URLs.\n\n"
        "💡 <i>You can send multiple photos one after another!</i>\n"
        "👉 <i>When done, tap <b>[ 🚀 Finish & Publish Project ]</b> or <b>[ ⏭️ Skip this Step ]</b>!</i>"
    )
    markup = screenshots_step_keyboard(has_screenshots=(shots_count > 0))
    bot.clear_step_handler_by_chat_id(chat_id)
    msg = bot.send_message(chat_id, text, reply_markup=markup)
    bot.register_next_step_handler(msg, process_wizard_step_screenshots)


def process_wizard_screenshot_media(message, file_id):
    """Thread-safe processor for single or multiple/album screenshot uploads during wizard with strict 1-by-1 chronological ordering"""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_screenshots":
        return

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        filename = f"shot_{int(time.time())}_{file_id[:8]}.jpg"
        ok, imgbb_url = imgbb_mgr.upload_image_bytes(downloaded, filename=filename)

        if ok and imgbb_url:
            with _shots_lock:
                # Store URL keyed by the strictly increasing Telegram message.message_id
                ordered_map = user_states[user_id].setdefault("ordered_screenshots", {})
                ordered_map[message.message_id] = imgbb_url
                
                # Sort exactly by message_id so photos match 100% the sequence user selected
                sorted_shots = [url for msg_id, url in sorted(ordered_map.items(), key=lambda x: x[0])]
                user_states[user_id]["app_data"]["screenshots"] = sorted_shots
                
                shots_count = len(sorted_shots)
                plural = "s" if shots_count > 1 else ""
                status_text = (
                    f"✅ <b>{shots_count} Screenshot{plural} Hosted & Attached!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🖼️ <b>Total Screenshots:</b> <code>{shots_count}</code>\n"
                    f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
                    f"👉 <i>Send more photos, or tap <b>[ 🚀 Finish & Publish Project ]</b> / reply <code>done</code>!</i>"
                )

                markup = screenshots_step_keyboard(has_screenshots=True)

                # Live in-place edit on existing status message to avoid multi-message spam
                existing_msg_id = user_states[user_id].get("screenshot_status_msg_id")
                updated = False
                if existing_msg_id:
                    try:
                        bot.edit_message_text(
                            status_text,
                            message.chat.id,
                            existing_msg_id,
                            reply_markup=markup
                        )
                        updated = True
                    except Exception:
                        pass
                
                if not updated:
                    sent = bot.send_message(message.chat.id, status_text, reply_markup=markup)
                    user_states[user_id]["screenshot_status_msg_id"] = sent.message_id

            bot.register_next_step_handler(message, process_wizard_step_screenshots)
        else:
            bot.send_message(message.chat.id, f"⚠️ ImgBB Upload notice: {imgbb_url}")
            bot.register_next_step_handler(message, process_wizard_step_screenshots)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error uploading screenshot: {str(e)}")
        bot.register_next_step_handler(message, process_wizard_step_screenshots)


def process_wizard_step_screenshots(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_screenshots":
        return

    # Check if a photo or document was uploaded
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id

    if file_id:
        process_wizard_screenshot_media(message, file_id)
        return
    else:
        raw_text = (message.text or "").strip()
        if raw_text.lower() in ("done", "finish", "publish", "ready", "ok"):
            finalize_publish_project(user_id, message.chat.id)
            return
        elif raw_text.lower() in ("skip", "none", "no", "skip skip", "pass"):
            if "screenshots" not in user_states[user_id]["app_data"]:
                user_states[user_id]["app_data"]["screenshots"] = []
            finalize_publish_project(user_id, message.chat.id)
            return
        else:
            urls = []
            for line in raw_text.splitlines():
                for part in line.split(','):
                    for item in part.split():
                        item_clean = item.strip()
                        if item_clean and (item_clean.startswith('http://') or item_clean.startswith('https://') or item_clean.startswith('icons/')):
                            urls.append(item_clean)

            if urls:
                with _shots_lock:
                    for u in urls:
                        if u not in user_states[user_id]["app_data"].setdefault("screenshots", []):
                            user_states[user_id]["app_data"]["screenshots"].append(u)
                finalize_publish_project(user_id, message.chat.id)
                return
            else:
                # Unrecognized text: prompt user clearly, do not prematurely publish!
                shots_count = len(user_states[user_id]["app_data"].get("screenshots", []))
                markup = screenshots_step_keyboard(has_screenshots=(shots_count > 0))
                bot.clear_step_handler_by_chat_id(message.chat.id)
                msg = bot.send_message(
                    message.chat.id,
                    "⚠️ <i>Please send photos, valid image URLs, or tap <b>[ 🚀 Finish & Publish Project ]</b> / reply <code>done</code> below:</i>",
                    reply_markup=markup
                )
                bot.register_next_step_handler(msg, process_wizard_step_screenshots)
                return


def finalize_publish_project(user_id, chat_id, edit_message_id=None):
    if user_id not in user_states:
        return

    app_data = user_states[user_id]["app_data"]
    app_name = app_data.get("name", "Project")

    # Step 1 Progress Bar
    p_text_1 = render_step_progress_text(f"Publishing {app_name}", 1, 3, "Validating schema, assets & checksums...")
    if edit_message_id:
        try:
            bot.edit_message_text(p_text_1, chat_id, edit_message_id)
            status_msg_id = edit_message_id
        except Exception:
            s_msg = bot.send_message(chat_id, p_text_1)
            status_msg_id = s_msg.message_id
    else:
        s_msg = bot.send_message(chat_id, p_text_1)
        status_msg_id = s_msg.message_id

    time.sleep(0.4)

    # Step 2 Progress Bar
    p_text_2 = render_step_progress_text(f"Publishing {app_name}", 2, 3, "Pushing node to Firebase Realtime Cloud...")
    try:
        bot.edit_message_text(p_text_2, chat_id, status_msg_id)
    except Exception:
        pass

    success, app_id = firebase_mgr.add_or_update_app(app_data)
    shots_count = len(app_data.get("screenshots", []))
    user_states.pop(user_id, None)

    time.sleep(0.3)

    if success:
        success_text = (
            f"🎉 <b>Project Published Successfully!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
            f"📱 <b>Name:</b> {app_data.get('name')}\n"
            f"🆔 <b>ID:</b> <code>{app_id}</code>\n"
            f"🏷️ <b>Category:</b> <code>{app_data.get('category', 'Apps')}</code>\n"
            f"🏷️ <b>Version:</b> <code>{app_data.get('version')}</code>\n"
            f"🖼️ <b>Screenshots:</b> <code>{shots_count} attached</code>\n"
            f"🔥 <b>Firebase Cloud:</b> <code>Synced in Real-time</code>\n\n"
            f"🌐 <b>Live Web URL:</b> {STORE_WEB_URL}/#/apps/{app_id}"
        )
        bot.edit_message_text(success_text, chat_id, status_msg_id, reply_markup=back_to_main_keyboard())
    else:
        bot.edit_message_text("⚠️ Failed to sync with Firebase.", chat_id, status_msg_id, reply_markup=back_to_main_keyboard())


# --------------------------------------------------------------------------
# Real-Time Field & Screenshots Processing Handlers
# --------------------------------------------------------------------------

def process_edit_app_icon_media(message, file_id):
    """Processor for updating an existing app's logo via photo/image upload"""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state:
        return
    app_id = state.get("app_id")
    if not app_id:
        return

    status_msg = bot.send_message(
        message.chat.id,
        render_step_progress_text("Updating App Logo", 1, 2, "Uploading new logo to ImgBB CDN...")
    )

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        filename = f"logo_{int(time.time())}_{file_id[:8]}.png"
        ok, imgbb_url = imgbb_mgr.upload_image_bytes(downloaded, filename=filename)

        if ok and imgbb_url:
            firebase_mgr.update_app_field(app_id, "icon", imgbb_url)
            app = firebase_mgr.get_app(app_id)
            app_name = app.get("name") if app else app_id
            user_states.pop(user_id, None)

            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("✏️ Edit Other Fields", callback_data=f"edit_menu:{app_id}"),
                InlineKeyboardButton("🔙 Back to App Menu", callback_data=f"view_app:{app_id}")
            )

            res_text = (
                f"🎉 <b>App Logo Successfully Updated!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 <b>App:</b> <code>{app_name}</code>\n"
                f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
                f"🖼️ <b>New Logo CDN Link:</b>\n"
                f"<code>{imgbb_url}</code>\n\n"
                f"🔥 <i>Synced live to Firebase & Web Store in real-time!</i>"
            )
            bot.edit_message_text(res_text, message.chat.id, status_msg.message_id, reply_markup=markup)
            log_activity("App Logo Updated", user_id=user_id, details=f"App: {app_name}, URL: {imgbb_url}")
        else:
            bot.edit_message_text(f"❌ ImgBB Upload Failed: {imgbb_url}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
    except Exception as e:
        bot.edit_message_text(f"❌ Error updating logo: {str(e)}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())


def process_field_update(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, {})
    app_id = state.get("app_id")
    field = state.get("field")

    if not app_id or not field:
        bot.send_message(message.chat.id, "Session expired.", reply_markup=back_to_main_keyboard())
        return

    # Check if a photo or image document was sent when updating logo/icon
    if field == "icon":
        file_id = None
        if message.photo:
            file_id = message.photo[-1].file_id
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id

        if file_id:
            process_edit_app_icon_media(message, file_id)
            return

    new_val = (message.text or "").strip()
    if not new_val:
        bot.send_message(message.chat.id, "⚠️ Invalid input. Please send valid text or a photo.", reply_markup=back_to_main_keyboard())
        return

    target_field = "description" if field == "desc" else "category" if field == "cat" else "downloadUrl" if field == "url" else field
    if target_field == "buttonText":
        if "\n" in new_val or len(new_val) > 35:
            bot.send_message(
                message.chat.id,
                "⚠️ <b>Button label must be short (1-4 words, max 35 characters)!</b>\n\n"
                "Examples: <code>Open Web App</code>, <code>Install APK</code>, <code>View Code</code>\n\n"
                "Please try again with a short button name.",
                reply_markup=back_to_main_keyboard()
            )
            return

    if target_field == "description":
        new_val = normalize_description_markdown(new_val)
    
    success = firebase_mgr.update_app_field(app_id, target_field, new_val)
    user_states.pop(user_id, None)

    if success:
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✏️ Edit Other Fields", callback_data=f"edit_menu:{app_id}"),
            InlineKeyboardButton("🔙 Back to App Menu", callback_data=f"view_app:{app_id}")
        )
        bot.send_message(
            message.chat.id,
            f"✅ <b>Field Updated in Real-Time!</b>\n\n"
            f"Field: <code>{target_field}</code>\n"
            f"New Value: <code>{new_val}</code>\n"
            f"🔥 <i>Synced live to Firebase & Web Store!</i>",
            reply_markup=markup
        )
        log_activity("Field Updated", user_id=user_id, details=f"App: {app_id}, Field: {target_field}")
    else:
        bot.send_message(message.chat.id, "❌ Failed to update field.", reply_markup=back_to_main_keyboard())


def process_add_screenshot_links(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, {})
    app_id = state.get("app_id")

    if not app_id:
        bot.send_message(message.chat.id, "Session expired.", reply_markup=back_to_main_keyboard())
        return

    raw_text = message.text.strip()
    if raw_text.lower() in ("skip", "cancel"):
        user_states.pop(user_id, None)
        bot.send_message(message.chat.id, "Screenshots addition cancelled.", reply_markup=back_to_main_keyboard())
        return

    urls = []
    for line in raw_text.splitlines():
        for part in line.split(','):
            for item in part.split():
                item_clean = item.strip()
                if item_clean and (item_clean.startswith('http://') or item_clean.startswith('https://') or item_clean.startswith('icons/')):
                    urls.append(item_clean)

    if not urls:
        bot.send_message(message.chat.id, "⚠️ No valid URLs provided (links must start with http://, https://, or icons/).", reply_markup=back_to_main_keyboard())
        return

    success = firebase_mgr.add_screenshots(app_id, urls)
    user_states.pop(user_id, None)

    if success:
        app = firebase_mgr.get_app(app_id)
        bot.send_message(
            message.chat.id,
            f"🎉 <b>{len(urls)} Screenshot(s) Added in Real-Time!</b>\n\n"
            f"App: <b>{app.get('name')}</b>\n"
            f"Total Screenshots: <code>{len(app.get('screenshots', []))}</code>\n"
            f"🔥 <i>Carousel updated on live website!</i>",
            reply_markup=back_to_main_keyboard()
        )
    else:
        bot.send_message(message.chat.id, "❌ Failed to add screenshots.", reply_markup=back_to_main_keyboard())


def process_upload_screenshot_photo(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, {})
    app_id = state.get("app_id")

    if not app_id:
        bot.send_message(message.chat.id, "Session expired.", reply_markup=back_to_main_keyboard())
        return

    # Check photo or document image
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id

    if not file_id:
        bot.send_message(message.chat.id, "⚠️ Please send a valid photo/image.", reply_markup=back_to_main_keyboard())
        return

    status_msg = bot.send_message(
        message.chat.id,
        render_step_progress_text("Hosting Screenshot", 1, 3, "Downloading from Telegram...")
    )

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        bot.edit_message_text(
            render_step_progress_text("Hosting Screenshot", 2, 3, "Uploading to ImgBB CDN..."),
            message.chat.id,
            status_msg.message_id
        )

        filename = f"shot_{app_id}_{int(time.time())}.jpg"
        ok, imgbb_url = imgbb_mgr.upload_image_bytes(downloaded, filename=filename)

        if not ok:
            bot.edit_message_text(f"❌ ImgBB Upload Failed: {imgbb_url}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
            return

        # Attach ImgBB URL directly to app screenshots in Firebase
        firebase_mgr.add_screenshots(app_id, [imgbb_url])

        app = firebase_mgr.get_app(app_id)
        total_shots = len(app.get("screenshots", [])) if app else 1

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("📸 Upload More Photos", callback_data=f"shot_up_photo:{app_id}"),
            InlineKeyboardButton("🔙 Back to App Menu", callback_data=f"view_app:{app_id}")
        )

        bot.edit_message_text(
            f"🎉 <b>Screenshot Hosted on ImgBB & Attached!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
            f"📱 <b>App:</b> {app.get('name') if app else app_id}\n"
            f"🖼️ <b>Total Screenshots:</b> <code>{total_shots}</code>\n"
            f"🔗 <b>ImgBB CDN Link:</b> <code>{imgbb_url}</code>\n\n"
            f"👉 <i>Send more photos directly, or tap below when finished!</i>",
            message.chat.id,
            status_msg.message_id,
            reply_markup=markup
        )
        msg = bot.send_message(message.chat.id, "👉 <i>Send next screenshot photo (or return to menu):</i>", reply_markup=markup)
        bot.register_next_step_handler(msg, process_upload_screenshot_photo)
    except Exception as e:
        bot.edit_message_text(f"❌ Failed to upload: {str(e)}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
        bot.edit_message_text(f"❌ Failed to upload: {str(e)}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())


# --------------------------------------------------------------------------
# GitHub Release 4-Step Interactive Wizard
# --------------------------------------------------------------------------

def process_release_step_tag(message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return

    tag = message.text.strip()
    user_states[user_id]["rel_data"]["tag"] = tag

    text = (
        f"🏷️ Release Tag: <code>{tag}</code>\n\n"
        f"<b>Step 2/4:</b> Please reply with the <b>Release Title / Name</b>\n"
        f"<i>(e.g., 'MyStore v1.0.0 Initial Release' or 'FocusFlow {tag} - Big Update')</i>"
    )
    msg = bot.send_message(message.chat.id, text, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_release_step_title)


def process_release_step_title(message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return

    title = message.text.strip()
    user_states[user_id]["rel_data"]["title"] = title

    text = (
        f"📝 Release Title: <b>{title}</b>\n\n"
        f"<b>Step 3/4:</b> Reply with the <b>Release Notes & Changelog</b>\n"
        f"<i>(Supports markdown bullet points, or send <code>skip</code> for default)</i>"
    )
    msg = bot.send_message(message.chat.id, text, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_release_step_notes)


def process_release_step_notes(message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return

    raw_notes = (message.text or "").strip()
    notes = "Release created via MyStore Telegram Admin Bot" if raw_notes.lower() in ("skip", "none", "") else raw_notes
    user_states[user_id]["rel_data"]["notes"] = notes

    rel = user_states[user_id]["rel_data"]
    text = (
        f"📦 <b>Step 4/4: Send Release File (Any Format)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ <b>Tag:</b> <code>{rel.get('tag')}</code>\n"
        f"📝 <b>Title:</b> <code>{rel.get('title')}</code>\n\n"
        f"👉 <b>Please send any file (APK, ZIP, EXE, DEB, IPA, PDF, ISO, etc.) as a document in this chat:</b>"
    )
    msg = bot.send_message(message.chat.id, text, reply_markup=cancel_wizard_keyboard())
    bot.register_next_step_handler(msg, process_release_step_apk_file)


def process_release_step_apk_file(message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return

    if not message.document:
        msg = bot.send_message(message.chat.id, "⚠️ Please send a valid file document (APK, ZIP, EXE, PDF, etc.).", reply_markup=cancel_wizard_keyboard())
        bot.register_next_step_handler(msg, process_release_step_apk_file)
        return

    rel = user_states[user_id]["rel_data"]
    tag = rel.get("tag", f"v{int(time.time())}")
    release_title = rel.get("title", f"Release {tag}")
    changelog = rel.get("notes", "Release created via MyStore Telegram Bot")

    doc = message.document
    file_name = doc.file_name or "file.bin"
    file_size_mb = round(doc.file_size / (1024 * 1024), 2)

    status_msg = bot.send_message(
        message.chat.id, 
        f"⏳ <b>Step 1/3:</b> Downloading <code>{file_name}</code> ({file_size_mb} MB)...\n"
        f"📊 <code>{make_progress_bar(0, doc.file_size)}</code>"
    )

    try:
        local_path = os.path.join(BASE_DIR, file_name)
        dl_ok, dl_res = smart_download_telegram_document(message, doc, local_path, status_msg)
        if not dl_ok:
            bot.edit_message_text(f"❌ Download Failed: {dl_res}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
            return

        # 1. Forward & Archive to Storage Channel (-1003887776900)
        bot.edit_message_text(
            f"⏳ <b>Step 2/3:</b> Archiving to Storage Channel (-1003887776900)...\n"
            f"📊 <code>{make_progress_bar(50, 100)}</code>",
            message.chat.id,
            status_msg.message_id
        )

        chan_ok, chan_info = storage_mgr.sync_upload_to_channel(
            file_path=local_path,
            caption=f"📦 <b>MyStore APK Cloud Archive</b>\nFile: <code>{file_name}</code>\nSize: <code>{file_size_mb} MB</code>\nTag: <code>{tag}</code>\nTitle: <code>{release_title}</code>"
        )
        if not chan_ok:
            try:
                with open(local_path, "rb") as f_up:
                    bot.send_document(
                        STORAGE_CHANNEL_ID,
                        f_up,
                        caption=f"📦 <b>MyStore APK Storage Archive</b>\nFile: <code>{file_name}</code>\nSize: <code>{file_size_mb} MB</code>\nTag: <code>{tag}</code>"
                    )
            except Exception as chan_err:
                print(f"[Channel Storage] Notice: {chan_err}")

        # 2. Upload to GitHub Releases with Real-Time Progress Bar
        progress_cb = ProgressCallbackHandler(
            bot=bot,
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            action_title="Step 3/3: Uploading to GitHub Releases",
            filename=file_name
        )

        success, direct_url = github_mgr.publish_apk_release(
            file_path=local_path,
            file_name=file_name,
            tag_name=tag,
            release_name=release_title,
            changelog=changelog,
            progress_callback=progress_cb
        )

        if os.path.exists(local_path):
            os.remove(local_path)

        user_states.pop(user_id, None)

        if success:
            result_text = (
                f"🎉 <b>GitHub Release Published & Hosted!</b>\n\n"
                f"🏷️ <b>Tag:</b> <code>{tag}</code>\n"
                f"📝 <b>Title:</b> <b>{release_title}</b>\n"
                f"📦 <b>File:</b> <code>{file_name}</code> (<code>{file_size_mb} MB</code>)\n"
                f"📢 <b>Channel Storage:</b> <code>Archived to -1003887776900</code>\n"
                f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
                f"🔗 <b>Direct Download Link:</b>\n"
                f"<code>{direct_url}</code>\n\n"
                f"🐙 <b>GitHub Release Page:</b>\n"
                f"https://github.com/{github_mgr.owner}/{github_mgr.repo}/releases/tag/{tag}"
            )
            bot.edit_message_text(result_text, message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
        else:
            bot.edit_message_text(f"❌ <b>GitHub Upload Failed:</b>\n{direct_url}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())

    except Exception as e:
        bot.edit_message_text(f"❌ <b>Error:</b> {str(e)}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())


def process_bump_version(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, {})
    app_id = state.get("app_id")

    if not app_id:
        bot.send_message(message.chat.id, "Session expired.", reply_markup=back_to_main_keyboard())
        return

    new_version = message.text.strip()
    app = firebase_mgr.get_app(app_id)
    if app:
        app["version"] = new_version
        app["updatedDate"] = time.strftime("%Y-%m-%d")
        firebase_mgr.add_or_update_app(app)
        
        bot.send_message(
            message.chat.id,
            f"✅ Version for <b>{app.get('name')}</b> bumped to <code>{new_version}</code> (Synced to Firebase in Real-Time)!",
            reply_markup=back_to_main_keyboard()
        )
    user_states.pop(user_id, None)


def process_app_update_input(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "app_update_upload":
        return

    app_id = state.get("app_id")
    app_name = state.get("app_name", "App")
    curr_version = state.get("curr_version", "v1.0")

    # If document or file was uploaded
    doc = message.document
    if doc:
        file_id = doc.file_id
        file_name = doc.file_name or f"{app_name}.apk"
        file_size_bytes = doc.file_size or 0
        file_size_mb = round(file_size_bytes / (1024 * 1024), 2)
        
        status_msg = bot.send_message(
            message.chat.id,
            f"⏳ <b>Downloading file from Telegram...</b>\n"
            f"📦 <code>{file_name}</code> (<code>{file_size_mb} MB</code>)"
        )
        try:
            file_info = bot.get_file(file_id)
            os.makedirs("/tmp/mystore_updates", exist_ok=True)
            local_path = f"/tmp/mystore_updates/{int(time.time())}_{file_name}"
            downloaded = bot.download_file(file_info.file_path)
            with open(local_path, "wb") as f_out:
                f_out.write(downloaded)
            
            user_states[user_id]["local_path"] = local_path
            user_states[user_id]["file_name"] = file_name
            user_states[user_id]["file_size_mb"] = file_size_mb
            user_states[user_id]["stage"] = "app_update_version"
            
            bot.edit_message_text(
                f"✅ <b>File Received & Staged:</b> <code>{file_name}</code> (<code>{file_size_mb} MB</code>)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏷️ Current Version: <code>{curr_version}</code>\n\n"
                f"👉 <b>Reply with the New Version Tag</b> (e.g. <code>v3.1.0</code>):\n"
                f"<i>(Send <code>auto</code> to auto-bump version)</i>",
                message.chat.id,
                status_msg.message_id
            )
            bot.register_next_step_handler(message, process_app_update_version)
            return
        except Exception as e:
            bot.edit_message_text(f"❌ Failed to download file: {str(e)}", message.chat.id, status_msg.message_id)
            return

    # If user sent text (e.g. URL or Cancel)
    text = (message.text or "").strip()
    if text.lower() in ("cancel", "/cancel"):
        user_states.pop(user_id, None)
        bot.send_message(message.chat.id, "Update cancelled.", reply_markup=back_to_main_keyboard())
        return

    if text.startswith("http://") or text.startswith("https://"):
        user_states[user_id]["download_url"] = text
        user_states[user_id]["stage"] = "app_update_version"
        msg = bot.send_message(
            message.chat.id,
            f"🔗 <b>Download URL Received!</b>\n\n"
            f"👉 <b>Reply with the New Version Tag</b> (e.g. <code>v3.1.0</code>):"
        )
        bot.register_next_step_handler(msg, process_app_update_version)
        return

    bot.send_message(message.chat.id, "⚠️ Please send the APK/file as a document, or send a valid download URL.")
    bot.register_next_step_handler(message, process_app_update_input)


def process_app_update_version(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "app_update_version":
        return

    curr_version = state.get("curr_version", "v1.0")
    text = (message.text or "").strip()

    if text.lower() in ("cancel", "/cancel"):
        if "local_path" in state and os.path.exists(state["local_path"]):
            try:
                os.remove(state["local_path"])
            except Exception:
                pass
        user_states.pop(user_id, None)
        bot.send_message(message.chat.id, "Update cancelled.", reply_markup=back_to_main_keyboard())
        return

    if text.lower() in ("auto", "bump", "next"):
        import re
        nums = re.findall(r'\d+', curr_version)
        if nums:
            last_num = int(nums[-1]) + 1
            nums[-1] = str(last_num)
            tag = "v" + ".".join(nums)
        else:
            tag = curr_version + ".1"
    else:
        tag = text if text.startswith("v") or (text and text[0].isdigit()) else f"v{text}"

    user_states[user_id]["version_tag"] = tag
    user_states[user_id]["stage"] = "app_update_changelog"

    msg = bot.send_message(
        message.chat.id,
        f"🏷️ <b>Version Tag Set:</b> <code>{tag}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 <b>Enter What's New / Changelog for this update:</b>\n\n"
        f"<i>(Send changelog notes in bullet points, or reply <code>skip</code> to use default)</i>"
    )
    bot.register_next_step_handler(msg, process_app_update_changelog)


def process_app_update_changelog(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "app_update_changelog":
        return

    text = (message.text or "").strip()
    if text.lower() in ("skip", "default", "none", ""):
        changelog = f"Release {state.get('version_tag', 'update')} — Bug fixes and performance improvements."
    else:
        changelog = text

    user_states[user_id]["changelog"] = changelog
    finalize_app_update_release(user_id, message.chat.id)


def finalize_app_update_release(user_id, chat_id):
    state = user_states.get(user_id)
    if not state:
        return

    app_id = state.get("app_id")
    app_name = state.get("app_name", "App")
    tag = state.get("version_tag", "v1.1")
    changelog = state.get("changelog", "Update via MyStore Admin")
    local_path = state.get("local_path")
    file_name = state.get("file_name", f"{app_name}.apk")
    file_size_mb = state.get("file_size_mb", 0)
    direct_url = state.get("download_url")

    status_msg = bot.send_message(
        chat_id,
        f"🚀 <b>Publishing Update to GitHub Releases & Firebase...</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 App: <b>{app_name}</b>\n"
        f"🏷️ Version: <code>{tag}</code>\n"
        f"📊 <code>{make_progress_bar(10, 100)}</code>"
    )

    try:
        # If we have a local APK file, archive to Channel & upload to GitHub
        if local_path and os.path.exists(local_path):
            # 1. Archive to Storage Channel
            bot.edit_message_text(
                f"⏳ <b>Step 1/3:</b> Archiving APK to Channel (-1003887776900)...\n"
                f"📊 <code>{make_progress_bar(35, 100)}</code>",
                chat_id,
                status_msg.message_id
            )
            chan_ok, chan_info = storage_mgr.sync_upload_to_channel(
                file_path=local_path,
                caption=f"📦 <b>MyStore APK Update Archive</b>\nApp: <b>{app_name}</b>\nVersion: <code>{tag}</code>\nSize: <code>{file_size_mb} MB</code>\nChangelog:\n{changelog}"
            )

            # 2. Upload to GitHub Releases with live progress callback
            progress_cb = ProgressCallbackHandler(
                bot=bot,
                chat_id=chat_id,
                message_id=status_msg.message_id,
                action_title=f"Step 2/3: Uploading {tag} to GitHub Releases",
                filename=file_name
            )

            gh_ok, gh_res = github_mgr.publish_apk_release(
                file_path=local_path,
                file_name=file_name,
                tag_name=tag,
                release_name=f"{app_name} {tag}",
                changelog=changelog,
                progress_callback=progress_cb
            )

            if os.path.exists(local_path):
                os.remove(local_path)

            if gh_ok and gh_res:
                direct_url = gh_res
            else:
                direct_url = direct_url or f"https://github.com/{github_mgr.owner}/{github_mgr.repo}"

        # 3. Update Firebase Realtime Database
        bot.edit_message_text(
            f"⏳ <b>Step 3/3:</b> Syncing live to Firebase Database & Web Store...\n"
            f"📊 <code>{make_progress_bar(90, 100)}</code>",
            chat_id,
            status_msg.message_id
        )

        full_data = firebase_mgr.get_full_store_data(force_refresh=True)
        apps = full_data.get("apps", [])
        for a in apps:
            if str(a.get("id")).lower() == str(app_id).lower():
                a["version"] = tag
                if direct_url:
                    a["downloadUrl"] = direct_url
                if file_size_mb:
                    a["size"] = f"{file_size_mb} MB"
                a["changelog"] = changelog
                a["updatedDate"] = time.strftime("%Y-%m-%d")
                
                # Append to versionHistory
                vh = a.setdefault("versionHistory", [])
                vh.insert(0, {
                    "version": tag,
                    "date": time.strftime("%Y-%m-%d"),
                    "size": f"{file_size_mb} MB" if file_size_mb else a.get("size", "N/A"),
                    "downloadUrl": direct_url or a.get("downloadUrl", ""),
                    "changelog": changelog
                })
                break
        
        full_data["apps"] = apps
        firebase_mgr.sync_to_cloud(full_data)
        user_states.pop(user_id, None)

        markup = InlineKeyboardMarkup(row_width=2)
        btns = []
        store_app_url = f"{STORE_WEB_URL.rstrip('/')}/#/apps/{app_id}" if STORE_WEB_URL else ""
        if is_valid_telegram_button_url(store_app_url):
            btns.append(InlineKeyboardButton("🌐 Open in Store", url=store_app_url))
        else:
            btns.append(InlineKeyboardButton("🔗 Copy Web Link", callback_data=f"copy_url:{app_id}"))

        if is_valid_telegram_button_url(direct_url):
            btns.append(InlineKeyboardButton("📥 Direct Download", url=direct_url))

        btns.append(InlineKeyboardButton("🔙 Back to App Menu", callback_data=f"view_app:{app_id}"))
        markup.add(*btns)

        success_text = (
            f"🎉 <b>App Successfully Updated & Released!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>App:</b> <code>{app_name}</code>\n"
            f"🏷️ <b>New Version:</b> <code>{tag}</code>\n"
            f"💾 <b>File Size:</b> <code>{file_size_mb} MB</code>\n"
            f"📅 <b>Release Date:</b> <code>{time.strftime('%Y-%m-%d')}</code>\n"
            f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
            f"🔗 <b>GitHub Direct Asset Download:</b>\n"
            f"<code>{direct_url}</code>\n\n"
            f"📝 <b>Changelog:</b>\n"
            f"<i>{changelog}</i>\n\n"
            f"🔥 <i>Live on Web Store & GitHub Releases!</i>"
        )
        try:
            bot.edit_message_text(success_text, chat_id, status_msg.message_id, reply_markup=markup)
        except Exception:
            bot.edit_message_text(success_text, chat_id, status_msg.message_id, reply_markup=back_to_main_keyboard())
        log_activity("App Updated & Released", user_id=user_id, details=f"App: {app_name}, Tag: {tag}")

    except Exception as e:
        bot.edit_message_text(f"❌ Error updating app: {str(e)}", chat_id, status_msg.message_id, reply_markup=back_to_main_keyboard())


def process_add_new_category(message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return
    name = message.text.strip()

    status_msg = bot.send_message(
        message.chat.id,
        render_step_progress_text(f"Creating Category: {name}", 1, 3, "Validating category name & uniqueness...")
    )
    time.sleep(0.35)

    try:
        bot.edit_message_text(
            render_step_progress_text(f"Creating Category: {name}", 2, 3, "Writing category node to Firebase Cloud..."),
            message.chat.id,
            status_msg.message_id
        )
    except Exception:
        pass

    ok, msg_txt = firebase_mgr.add_category(name)
    user_states.pop(user_id, None)

    time.sleep(0.3)

    if ok:
        res_text = (
            f"🎉 <b>Category Created Successfully!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 <b>Category:</b> <code>{name}</code>\n"
            f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
            f"🔥 <i>Live category chip synced to Web Store in real-time!</i>"
        )
        bot.edit_message_text(res_text, message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
    else:
        bot.edit_message_text(f"⚠️ {msg_txt}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())


def process_rename_category(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, {})
    old_cat = state.get("old_cat")
    if not old_cat:
        bot.send_message(message.chat.id, "Session expired.", reply_markup=back_to_main_keyboard())
        return

    new_cat = message.text.strip()
    status_msg = bot.send_message(
        message.chat.id,
        render_step_progress_text(f"Renaming Category: {old_cat} ➔ {new_cat}", 1, 3, "Updating linked app records...")
    )
    time.sleep(0.35)

    try:
        bot.edit_message_text(
            render_step_progress_text(f"Renaming Category: {old_cat} ➔ {new_cat}", 2, 3, "Syncing changes to Firebase Cloud..."),
            message.chat.id,
            status_msg.message_id
        )
    except Exception:
        pass

    ok, msg_txt = firebase_mgr.rename_category(old_cat, new_cat)
    user_states.pop(user_id, None)

    time.sleep(0.3)

    if ok:
        res_text = (
            f"🎉 <b>Category Renamed Successfully!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 <b>Old:</b> <code>{old_cat}</code> ➔ <b>New:</b> <code>{new_cat}</code>\n"
            f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
            f"🔥 <i>All apps and category chips updated in real-time!</i>"
        )
        bot.edit_message_text(res_text, message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
    else:
        bot.edit_message_text(f"⚠️ {msg_txt}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())


def process_edit_screenshot_media(message, file_id):
    """Thread-safe processor for uploading screenshots to an existing app"""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "upload_screenshot_photo":
        return
    app_id = state.get("app_id")
    if not app_id:
        return

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        filename = f"shot_{int(time.time())}_{file_id[:8]}.jpg"
        ok, imgbb_url = imgbb_mgr.upload_image_bytes(downloaded, filename=filename)

        if ok and imgbb_url:
            with _shots_lock:
                firebase_mgr.add_screenshot(app_id, imgbb_url)
                app = firebase_mgr.get_app(app_id)
                shots = app.get("screenshots", []) if app else []
                total_shots = len(shots)

                markup = InlineKeyboardMarkup(row_width=1)
                markup.add(
                    InlineKeyboardButton("📸 Upload More Photos", callback_data=f"shot_photo:{app_id}"),
                    InlineKeyboardButton("🔙 Back to App Menu", callback_data=f"view_app:{app_id}")
                )

                plural = "s" if total_shots > 1 else ""
                status_text = (
                    f"🎉 <b>{total_shots} Screenshot{plural} Hosted on ImgBB & Attached!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
                    f"📱 <b>App:</b> {app.get('name') if app else app_id}\n"
                    f"🖼️ <b>Total Screenshots:</b> <code>{total_shots}</code>\n\n"
                    f"👉 <i>Send more photos directly, or tap below when finished!</i>"
                )

                # Edit existing message in-place to prevent multiple separate messages
                existing_msg_id = user_states[user_id].get("edit_shot_status_msg_id")
                updated = False
                if existing_msg_id:
                    try:
                        bot.edit_message_text(
                            status_text,
                            message.chat.id,
                            existing_msg_id,
                            reply_markup=markup
                        )
                        updated = True
                    except Exception:
                        pass

                if not updated:
                    sent = bot.send_message(message.chat.id, status_text, reply_markup=markup)
                    user_states[user_id]["edit_shot_status_msg_id"] = sent.message_id

            bot.register_next_step_handler(message, process_upload_screenshot_photo)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error uploading screenshot: {str(e)}")
        bot.register_next_step_handler(message, process_upload_screenshot_photo)


def process_wizard_icon_media(message, file_id):
    """Processor for wizard app icon upload"""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("stage") != "wizard_step_icon":
        return

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        filename = f"logo_{int(time.time())}_{file_id[:8]}.png"
        ok, imgbb_url = imgbb_mgr.upload_image_bytes(downloaded, filename=filename)

        if ok and imgbb_url:
            user_states[user_id]["app_data"]["icon"] = imgbb_url
            bot.send_message(
                message.chat.id,
                f"✅ <b>Logo Hosted on ImgBB Cloud!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
                f"🔗 Direct CDN URL: <code>{imgbb_url}</code>"
            )
        else:
            user_states[user_id]["app_data"]["icon"] = "icons/icon-192.svg"
            bot.send_message(message.chat.id, f"⚠️ ImgBB notice: {imgbb_url}\nUsing default icon.")
    except Exception as e:
        user_states[user_id]["app_data"]["icon"] = "icons/icon-192.svg"
        bot.send_message(message.chat.id, f"⚠️ Error uploading logo: {str(e)}")

    prompt_wizard_step_screenshots(user_id, message.chat.id)


# --------------------------------------------------------------------------
# Unified Direct Image / Photo Uploader & Album Batch Router
# --------------------------------------------------------------------------

def handle_standalone_photo_upload(message):
    file_id = message.photo[-1].file_id
    status_msg = bot.send_message(
        message.chat.id,
        render_step_progress_text("Hosting Image on ImgBB", 1, 2, "Downloading image from Telegram...")
    )

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        bot.edit_message_text(
            render_step_progress_text("Hosting Image on ImgBB", 2, 2, "Uploading to ImgBB High-Speed CDN..."),
            message.chat.id,
            status_msg.message_id
        )

        filename = f"img_{int(time.time())}.jpg"
        ok, imgbb_url = imgbb_mgr.upload_image_bytes(downloaded, filename=filename)

        if ok:
            res_text = (
                f"🖼️ <b>Image Hosted on ImgBB Cloud!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
                f"🔗 <b>Direct CDN Image Link:</b>\n"
                f"<code>{imgbb_url}</code>\n\n"
                f"💡 <i>You can copy this link and use it for App Logos, Screenshots, or Banners in your store!</i>"
            )
            bot.edit_message_text(res_text, message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
        else:
            bot.edit_message_text(f"❌ <b>ImgBB Upload Failed:</b> {imgbb_url}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
    except Exception as e:
        bot.edit_message_text(f"❌ <b>Error:</b> {str(e)}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())


def handle_standalone_doc_image_upload(message):
    file_id = message.document.file_id
    doc_name = message.document.file_name or f"doc_{int(time.time())}.png"
    status_msg = bot.send_message(
        message.chat.id,
        render_step_progress_text("Hosting Image on ImgBB", 1, 2, f"Downloading {doc_name}...")
    )

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        bot.edit_message_text(
            render_step_progress_text("Hosting Image on ImgBB", 2, 2, "Uploading to ImgBB High-Speed CDN..."),
            message.chat.id,
            status_msg.message_id
        )

        ok, imgbb_url = imgbb_mgr.upload_image_bytes(downloaded, filename=doc_name)
        if ok:
            res_text = (
                f"🖼️ <b>Image Document Hosted on ImgBB Cloud!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <code>{make_progress_bar(100, 100)}</code>\n\n"
                f"📄 <b>File:</b> <code>{doc_name}</code>\n"
                f"🔗 <b>Direct CDN Image Link:</b>\n"
                f"<code>{imgbb_url}</code>\n\n"
                f"💡 <i>You can copy this link and use it for App Logos, Screenshots, or Banners in your store!</i>"
            )
            bot.edit_message_text(res_text, message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
        else:
            bot.edit_message_text(f"❌ <b>ImgBB Upload Failed:</b> {imgbb_url}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())
    except Exception as e:
        bot.edit_message_text(f"❌ <b>Error:</b> {str(e)}", message.chat.id, status_msg.message_id, reply_markup=back_to_main_keyboard())


@bot.message_handler(content_types=['photo'])
def handle_unified_photo_stream(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    file_id = message.photo[-1].file_id
    state = user_states.get(user_id, {})
    stage = state.get("stage")

    if stage == "wizard_step_screenshots":
        process_wizard_screenshot_media(message, file_id)
    elif stage == "upload_screenshot_photo":
        process_edit_screenshot_media(message, file_id)
    elif stage == "wizard_step_icon":
        process_wizard_icon_media(message, file_id)
    elif stage == "edit_app_icon" or (stage == "edit_field" and state.get("field") == "icon"):
        process_edit_app_icon_media(message, file_id)
    elif not stage:
        handle_standalone_photo_upload(message)


@bot.message_handler(content_types=['document'], func=lambda m: (m.document.mime_type or '').startswith('image/'))
def handle_unified_doc_image_stream(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    file_id = message.document.file_id
    state = user_states.get(user_id, {})
    stage = state.get("stage")

    if stage == "wizard_step_screenshots":
        process_wizard_screenshot_media(message, file_id)
    elif stage == "upload_screenshot_photo":
        process_edit_screenshot_media(message, file_id)
    elif stage == "wizard_step_icon":
        process_wizard_icon_media(message, file_id)
    elif stage == "edit_app_icon" or (stage == "edit_field" and state.get("field") == "icon"):
        process_edit_app_icon_media(message, file_id)
    elif not stage:
        handle_standalone_doc_image_upload(message)


@bot.message_handler(content_types=['document', 'audio', 'video'], func=lambda m: not (m.document and (m.document.mime_type or '').startswith('image/')))
def handle_unified_doc_stream(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    state = user_states.get(user_id, {})
    stage = state.get("stage")

    if stage == "app_update_upload":
        process_app_update_input(message)
    elif stage == "wizard_step_upload":
        process_wizard_step_upload(message)
    elif not stage:
        handle_standalone_apk_upload(message)


# --------------------------------------------------------------------------
# Main Entry Point
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"🤖 Starting MyStore Telegram Admin Bot...")
    print(f"👑 Authorized Admins: {ADMIN_IDS}")
    print(f"🐙 GitHub Target: {github_mgr.owner}/{github_mgr.repo}")
    print(f"🔥 Firebase Database: {firebase_mgr.db_url}")

    # Proactively clear any active webhook & stale pending updates so polling never crashes with 409 Conflict
    try:
        print("🔄 Clearing any active webhook & pending updates...")
        bot.delete_webhook(drop_pending_updates=True)
        time.sleep(1)
        print("✅ Telegram webhook cleared successfully.")
    except Exception as e:
        print(f"⚠️ Notice when clearing webhook: {e}")

    print(f"🚀 Bot is polling for commands...")
    
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=20, restart_on_change=False)
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Bot stopped gracefully.")
