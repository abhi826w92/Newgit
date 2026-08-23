import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import get_user_api_key, set_user_state, get_user_state, clear_user_state
from api_client import search_items
from keyboards import back_to_main_kb, cancel_kb
from helpers import clean_html, format_bytes, get_mime_icon

logger = logging.getLogger(__name__)

async def search_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user to type search keyword."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    set_user_state(user_id, "AWAITING_SEARCH_QUERY")
    
    text = (
        "🔍 <b>SEARCH FILES & FOLDERS</b>\n\n"
        "Aap jo bhi file ya folder search karna chahte hain, uska naam yahan type karke send karein.\n\n"
        "💡 <i>Example: <code>avatar</code> ya <code>movie.mp4</code> ya <code>invoice</code></i>"
    )
    await query.edit_message_text(text, reply_markup=cancel_kb("menu_main"), parse_mode="HTML")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /search <query> command."""
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    
    if not api_key:
        await update.message.reply_html("❌ Kripya pehle /start bhej kar apni API key connect karein.")
        return
        
    query_text = " ".join(context.args).strip() if context.args else ""
    if not query_text:
        set_user_state(user_id, "AWAITING_SEARCH_QUERY")
        await update.message.reply_html(
            "🔍 Kripya search query provide karein: <code>/search &lt;name&gt;</code>\n"
            "Ya sidha file ka naam yahan type karein:"
        )
        return
        
    await execute_search(update, context, api_key, query_text)

async def handle_search_query_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search text input."""
    user_id = update.effective_user.id
    state, _ = get_user_state(user_id)
    
    if state != "AWAITING_SEARCH_QUERY":
        return False
        
    api_key = get_user_api_key(user_id)
    if not api_key:
        return False
        
    query_text = (update.message.text or "").strip()
    if not query_text:
        return False
        
    clear_user_state(user_id)
    await execute_search(update, context, api_key, query_text)
    return True

async def execute_search(update: Update, context: ContextTypes.DEFAULT_TYPE, api_key: str, query_text: str):
    """Execute search query against API."""
    status_msg = await update.message.reply_html(f"🔍 <i>Searching for '<b>{clean_html(query_text)}</b>'...</i>")
    
    try:
        res = await search_items(api_key, query_text)
        
        # Support different response formats from /v1/search
        items = []
        if isinstance(res, dict):
            items = res.get("items") or res.get("results") or res.get("data", [])
        elif isinstance(res, list):
            items = res
            
        if not items:
            await status_msg.edit_text(
                f"🔍 <b>No files found matching:</b> '<code>{clean_html(query_text)}</code>'\n\n"
                f"Kripya dusra keyword try karein.",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
            return

        keyboard = []
        for item in items[:12]:
            file_id = item.get("id") or item.get("message_id")
            name = item.get("name", "Untitled")
            size_str = format_bytes(item.get("size", 0))
            icon = get_mime_icon(item.get("mimeType", ""), name)
            display_name = name if len(name) <= 22 else f"{name[:19]}..."
            keyboard.append([InlineKeyboardButton(f"{icon} {display_name} ({size_str})", callback_data=f"file_view:{file_id}:root:1")])
            
        keyboard.append([
            InlineKeyboardButton("🔍 Search Again", callback_data="menu_search_prompt"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")
        ])
        
        text = (
            f"🔍 <b>Search Results for:</b> '<code>{clean_html(query_text)}</code>'\n"
            f"Found: <b>{len(items)}</b> matches\n\n"
            f"<i>Details dekhne ke liye file par click karein:</i>"
        )
        await status_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Search execution error: {e}")
        await status_msg.edit_text(f"❌ Error during search: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")
