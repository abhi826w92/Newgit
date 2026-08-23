import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import get_user_api_key, delete_user_api_key, set_user_state, clear_user_state
from api_client import get_storage_stats, get_user_profile
from keyboards import main_menu_kb, account_kb, back_to_main_kb, cancel_kb
from helpers import clean_html, format_bytes

logger = logging.getLogger(__name__)

async def menu_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return to main menu."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    clear_user_state(user.id)
    
    api_key = get_user_api_key(user.id)
    if not api_key:
        from handlers.start_handler import start_command
        await start_command(update, context)
        return
        
    first_name = user.first_name or "User"
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
    await query.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")

async def menu_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display cloud storage stats."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    
    if not api_key:
        await query.edit_message_text("❌ Kripya pehle apni API key connect karein (/start).")
        return
        
    await query.edit_message_text("⏳ <b>Fetching live storage statistics...</b>", parse_mode="HTML")
    
    try:
        stats = await get_storage_stats(api_key)
        if stats.get("status") == "success":
            total_files = stats.get("total_files", 0)
            total_folders = stats.get("total_folders", 0)
            total_bytes = stats.get("total_storage_bytes", 0)
            total_mb = stats.get("total_storage_mb", "0.00")
            total_gb = stats.get("total_storage_gb", "0.000")
            engine = stats.get("storage_engine", "Telegram Cloud MTProto")
            quota = stats.get("quota", "Unlimited Free Cloud Storage")
            
            categories = stats.get("category_breakdown", {})
            videos = categories.get("videos", {})
            images = categories.get("images", {})
            audio = categories.get("audio", {})
            docs = categories.get("documents", {})
            apks = categories.get("apks", {})
            others = categories.get("others", {})
            
            text = (
                f"📊 <b>TG DRIVE STORAGE STATISTICS</b>\n\n"
                f"📁 <b>Total Files:</b> <code>{total_files}</code>\n"
                f"📂 <b>Total Folders:</b> <code>{total_folders}</code>\n"
                f"💾 <b>Total Storage Used:</b> <code>{format_bytes(total_bytes)}</code> ({total_mb} MB)\n"
                f"♾️ <b>Cloud Quota:</b> {clean_html(quota)}\n"
                f"⚙️ <b>Storage Engine:</b> {clean_html(engine)}\n\n"
                f"<b>Media Breakdown:</b>\n"
                f"• 🎬 <b>Videos:</b> {videos.get('count', 0)} files ({format_bytes(videos.get('bytes', 0))})\n"
                f"• 🖼️ <b>Images:</b> {images.get('count', 0)} files ({format_bytes(images.get('bytes', 0))})\n"
                f"• 🎵 <b>Audios:</b> {audio.get('count', 0)} files ({format_bytes(audio.get('bytes', 0))})\n"
                f"• 📄 <b>Documents:</b> {docs.get('count', 0)} files ({format_bytes(docs.get('bytes', 0))})\n"
                f"• 📱 <b>APKs:</b> {apks.get('count', 0)} files ({format_bytes(apks.get('bytes', 0))})\n"
                f"• 📎 <b>Others:</b> {others.get('count', 0)} files ({format_bytes(others.get('bytes', 0))})\n"
            )
            await query.edit_message_text(text, reply_markup=back_to_main_kb(), parse_mode="HTML")
        else:
            await query.edit_message_text(
                f"❌ Error fetching stats: {clean_html(stats.get('message', 'Unknown error'))}",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await query.edit_message_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")

async def menu_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show account details and options."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    api_key = get_user_api_key(user.id)
    
    if not api_key:
        await query.edit_message_text("❌ Kripya pehle apni API key connect karein (/start).")
        return
        
    masked_key = f"{api_key[:12]}...{api_key[-6:]}" if len(api_key) > 20 else api_key
    
    profile_text = ""
    try:
        res = await get_user_profile(api_key)
        if res.get("status") == "success":
            d = res.get("data", {})
            rate = d.get("rate_limits", {})
            m_rate = rate.get("per_minute", {})
            h_rate = rate.get("per_hour", {})
            d_rate = rate.get("per_day", {})
            
            profile_text = (
                f"👤 <b>Telegram User ID:</b> <code>{d.get('user_id', 'N/A')}</code>\n"
                f"⚡ <b>API Status:</b> <code>{d.get('status', 'Active').upper()}</code>\n"
                f"🚀 <b>Rate Limits:</b>\n"
                f"  • Min: {m_rate.get('remaining', 60)}/{m_rate.get('limit', 60)} req/min\n"
                f"  • Hour: {h_rate.get('remaining', 1500)}/{h_rate.get('limit', 1500)} req/hr\n"
                f"  • Day: {d_rate.get('remaining', 10000)}/{d_rate.get('limit', 10000)} req/day\n\n"
            )
    except Exception as e:
        logger.warning(f"Error getting account info: {e}")
        
    text = (
        f"⚙️ <b>ACCOUNT & API SETTINGS</b>\n\n"
        f"🔑 <b>Connected API Key:</b>\n<code>{masked_key}</code>\n\n"
        f"{profile_text}"
        f"Aap chahein toh nayi API key connect kar sakte hain ya account logout kar sakte hain."
    )
    await query.edit_message_text(text, reply_markup=account_kb(), parse_mode="HTML")

async def menu_setkey_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user to send a new API key."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    set_user_state(user_id, "AWAITING_API_KEY")
    
    text = (
        "🔑 <b>Enter New TG Drive API Key:</b>\n\n"
        "Kripya apni nayi API key yahan message me send karein (e.g. <code>tgd_live_...</code>).\n\n"
        "Agar key nahi hai toh <a href=\"https://tgdriveo.pages.dev/#/developer\">yahan click karke generate karein</a>."
    )
    await query.edit_message_text(text, reply_markup=cancel_kb("menu_account"), parse_mode="HTML", disable_web_page_preview=True)

async def menu_logout_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirmation before disconnecting."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [
            InlineKeyboardButton("🚪 Yes, Logout / Disconnect", callback_data="menu_logout_do"),
            InlineKeyboardButton("❌ Cancel", callback_data="menu_account")
        ]
    ]
    text = "⚠️ <b>Kya aap sach me apna account disconnect karna chahte hain?</b>\n\nIsse aapki saved API key is bot se remove ho jayegi."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def menu_logout_do_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perform logout."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    delete_user_api_key(user_id)
    
    from keyboards import api_key_request_kb
    text = "✅ <b>Aapka account successfully logout ho gaya hai!</b>\n\nBot ko dobara use karne ke liye /start bhej kar API key link karein."
    await query.edit_message_text(text, reply_markup=api_key_request_kb(), parse_mode="HTML")

async def menu_upload_guide_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upload instructions."""
    query = update.callback_query
    await query.answer()
    
    text = (
        "📤 <b>File Upload Kaise Karein:</b>\n\n"
        "1️⃣ Aapko bas koi bhi <b>Photo, Video, Audio, Document ya APK</b> is chat me direct send/forward karna hai.\n"
        "2️⃣ Bot use automatically aapke <b>TG Drive (Saved Messages)</b> me upload karke direct download link generate kar dega.\n\n"
        "💡 <i>Tip: Aap ek sath multiple files bhi send kar sakte hain!</i>"
    )
    await query.edit_message_text(text, reply_markup=back_to_main_kb(), parse_mode="HTML")
