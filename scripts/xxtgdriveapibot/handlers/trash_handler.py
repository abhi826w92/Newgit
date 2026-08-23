import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import get_user_api_key
from api_client import list_trash, restore_trash, empty_trash
from keyboards import trash_kb, back_to_main_kb
from helpers import clean_html

logger = logging.getLogger(__name__)

async def menu_trash_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View items in trash bin."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    if not api_key:
        await query.edit_message_text("❌ Kripya pehle /start bhej kar apni API key connect karein.")
        return

    try:
        res = await list_trash(api_key)
        items = res.get("items", [])
        total = res.get("total_in_trash", len(items))
        
        if not items:
            text = (
                "🗑️ <b>RECYCLE BIN / TRASH</b>\n\n"
                "<i>Aapka trash bin bilkul khali hai!</i>"
            )
            await query.edit_message_text(text, reply_markup=back_to_main_kb(), parse_mode="HTML")
            return

        text = (
            f"🗑️ <b>RECYCLE BIN / TRASH ({total})</b>\n\n"
            f"<i>Niche diye gaye files ko restore ya permanent delete kar sakte hain:</i>"
        )
        await query.edit_message_text(text, reply_markup=trash_kb(items), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Trash view error: {e}")
        await query.edit_message_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")

async def trash_restore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restore a deleted file."""
    query = update.callback_query
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    file_id = query.data.split(":")[1]

    try:
        res = await restore_trash(api_key, file_id)
        if res.get("status") == "success":
            await query.answer(f"✅ File #{file_id} Restored!", show_alert=True)
            # Reload trash list
            await menu_trash_callback(update, context)
        else:
            await query.answer(f"❌ Restore failed: {res.get('message', 'Error')}", show_alert=True)
    except Exception as e:
        logger.error(f"Trash restore error: {e}")
        await query.answer(f"❌ Error: {str(e)}", show_alert=True)

async def trash_empty_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask confirmation to empty trash."""
    query = update.callback_query
    await query.answer()
    
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💥 Yes, Empty Trash", callback_data="trash_empty_do"),
            InlineKeyboardButton("❌ Cancel", callback_data="menu_trash")
        ]
    ])
    text = "⚠️ <b>Empty Trash Confirmation</b>\n\nKya aap Trash ki saari files permanently delete karna chahte hain? Yeh undo nahi ho sakta."
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

async def trash_empty_do_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permanently empty trash."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)

    try:
        res = await empty_trash(api_key)
        if res.get("status") == "success":
            await query.edit_message_text(
                "✅ <b>Trash successfully emptied!</b>",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
        else:
            await query.edit_message_text(
                f"❌ Error emptying trash: {clean_html(res.get('message', 'Error'))}",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Empty trash error: {e}")
        await query.edit_message_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")
