import logging
from telegram import Update
from telegram.ext import ContextTypes
from database import get_user_api_key, set_user_api_key, set_user_state, get_user_state, clear_user_state
from api_client import validate_api_key, get_user_profile
from keyboards import api_key_request_kb, main_menu_kb, cancel_kb
from helpers import clean_html

logger = logging.getLogger(__name__)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name or "User"
    username = user.username or ""

    # Check if user already has an API key in database
    api_key = get_user_api_key(user_id)

    if not api_key:
        # Prompt user to enter their API key
        set_user_state(user_id, "AWAITING_API_KEY")
        text = (
            f"👋 <b>Namaste {clean_html(first_name)}! Welcome to TG Drive Cloud Bot.</b>\n\n"
            f"Is bot ko use karne ke liye aapko apni <b>TG Drive API Key</b> link karni hogi.\n\n"
            f"🔑 <b>API Key kaise banayein:</b>\n"
            f"1️⃣ Niche diye gaye button <b>'🌐 Generate API Key'</b> par click karein.\n"
            f"2️⃣ Website se apni personal API Key copy karein.\n"
            f"3️⃣ Yahan bot ko message me send karein (e.g. <code>tgd_live_...</code>).\n\n"
            f"👇 <i>Kripya apni API Key yahan paste karke send karein:</i>"
        )
        if update.message:
            await update.message.reply_html(text, reply_markup=api_key_request_kb(), disable_web_page_preview=True)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=api_key_request_kb(), parse_mode="HTML", disable_web_page_preview=True)
        return

    # User already has an API key, show Main Menu
    await show_main_menu(update, context, user_id, first_name)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, first_name: str):
    """Render the Main Menu dashboard."""
    api_key = get_user_api_key(user_id)
    profile_info = "Connected"
    
    try:
        profile_res = await get_user_profile(api_key)
        if profile_res.get("status") == "success":
            data = profile_res.get("data", {})
            tg_uid = data.get("user_id", "N/A")
            quota = data.get("quota", "Unlimited")
            profile_info = f"UID: <code>{clean_html(tg_uid)}</code> | Quota: <b>{clean_html(quota)}</b>"
    except Exception as e:
        logger.warning(f"Failed to fetch profile: {e}")

    text = (
        f"🚀 <b>TG DRIVE CLOUD MANAGER</b>\n\n"
        f"👤 <b>User:</b> {clean_html(first_name)}\n"
        f"⚡ <b>Status:</b> {profile_info}\n"
        f"☁️ <b>Storage Engine:</b> Telegram Cloud (Saved Messages)\n\n"
        f"<i>Niche diye gaye buttons se files browse, search, upload ya manage karein:</i>"
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
        except Exception:
            await update.callback_query.message.reply_html(text, reply_markup=main_menu_kb())
    elif update.message:
        await update.message.reply_html(text, reply_markup=main_menu_kb())

async def handle_api_key_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input when entering an API key."""
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name or "User"
    username = user.username or ""
    text = (update.message.text or "").strip()

    state, _ = get_user_state(user_id)
    
    # If text looks like a TG Drive API key or user is in AWAITING_API_KEY state
    if state == "AWAITING_API_KEY" or text.startswith("tgd_"):
        status_msg = await update.message.reply_html("⏳ <b>Verifying API Key with TG Drive Server...</b>")
        
        is_valid, result = await validate_api_key(text)
        
        if is_valid:
            # Key is valid! Save in DB
            set_user_api_key(user_id, text, username=username, first_name=first_name)
            clear_user_state(user_id)
            
            tg_uid = result.get("user_id", "Connected")
            quota = result.get("quota", "Unlimited")
            
            await status_msg.edit_text(
                f"✅ <b>API Key Successfully Verified & Connected!</b>\n\n"
                f"👤 <b>Telegram UID:</b> <code>{clean_html(tg_uid)}</code>\n"
                f"📦 <b>Cloud Quota:</b> {clean_html(quota)}\n\n"
                f"Aapka account successfully connect ho chuka hai.",
                parse_mode="HTML"
            )
            
            # Immediately show Main Menu
            await show_main_menu(update, context, user_id, first_name)
            return True
        else:
            error_text = result if isinstance(result, str) else "Invalid API key"
            await status_msg.edit_text(
                f"❌ <b>API Key Verification Failed!</b>\n\n"
                f"<b>Reason:</b> {clean_html(error_text)}\n\n"
                f"Kripya sahi API Key check karke dobara bhejein.",
                reply_markup=api_key_request_kb(),
                parse_mode="HTML"
            )
            return True
            
    return False

async def help_api_guide_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show guide on how to get API key."""
    query = update.callback_query
    await query.answer()
    
    text = (
        "📖 <b>TG Drive API Key Kaise Nikalein:</b>\n\n"
        "1. Apne browser me <a href=\"https://tgdriveo.pages.dev/#/developer\">tgdriveo.pages.dev/#/developer</a> open karein.\n"
        "2. Apne Telegram account se login / connect karein.\n"
        "3. Developer section me <b>Generate API Key</b> par click karein.\n"
        "4. Jo key mile (e.g. <code>tgd_live_...</code>), use copy karein.\n"
        "5. Is bot ko chat me paste karke send kar dein.\n\n"
        "<i>Bas itna karte hi aapka Cloud Drive active ho jayega!</i>"
    )
    await query.edit_message_text(text, reply_markup=cancel_kb("menu_main"), parse_mode="HTML", disable_web_page_preview=True)
