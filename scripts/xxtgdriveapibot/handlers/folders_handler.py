import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import get_user_api_key, set_user_state, get_user_state, clear_user_state
from api_client import list_folders, create_folder, delete_folder
from keyboards import folders_list_kb, folder_view_kb, back_to_main_kb, cancel_kb
from helpers import clean_html

logger = logging.getLogger(__name__)

async def list_folders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List virtual folders."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    if not api_key:
        await query.edit_message_text("❌ Kripya pehle /start bhej kar apni API key connect karein.")
        return
        
    parts = query.data.split(":")
    parent_id = parts[1] if len(parts) > 1 else "root"

    try:
        res = await list_folders(api_key, parent_id=parent_id)
        if res.get("status") == "success":
            folders = res.get("folders", [])
            total = len(folders)
            
            text = (
                f"📂 <b>VIRTUAL FOLDERS</b>\n\n"
                f"Total Folders: <b>{total}</b>\n\n"
                f"<i>Naya folder banane ke liye '➕ Create New Folder' par click karein:</i>"
            )
            kb = folders_list_kb(folders, current_parent=parent_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await query.edit_message_text(
                f"❌ Error fetching folders: {clean_html(res.get('message', 'Error'))}",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Folders error: {e}")
        await query.edit_message_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")

async def folder_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View options for a specific folder."""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split(":")
    folder_id = parts[1]

    text = (
        f"📁 <b>FOLDER OPTIONS</b>\n\n"
        f"Folder ID: <code>{clean_html(folder_id)}</code>\n\n"
        f"Aap is folder ki files dekh sakte hain ya is folder ko delete kar sakte hain."
    )
    await query.edit_message_text(text, reply_markup=folder_view_kb(folder_id, folder_id), parse_mode="HTML")

async def folder_create_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user to type folder name."""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split(":")
    parent_id = parts[1] if len(parts) > 1 else "root"
    
    user_id = update.effective_user.id
    set_user_state(user_id, "AWAITING_FOLDER_NAME", {"parent_id": parent_id})
    
    text = (
        "➕ <b>Create New Folder</b>\n\n"
        "Kripya naye folder ka <b>naam (Name)</b> yahan type karke send karein (e.g. <code>Movies</code>, <code>Documents</code>):"
    )
    await query.edit_message_text(text, reply_markup=cancel_kb(f"menu_folders:{parent_id}"), parse_mode="HTML")

async def handle_folder_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle folder name text submission."""
    user_id = update.effective_user.id
    state, state_data = get_user_state(user_id)
    
    if state != "AWAITING_FOLDER_NAME":
        return False
        
    api_key = get_user_api_key(user_id)
    if not api_key:
        return False
        
    folder_name = (update.message.text or "").strip()
    if not folder_name:
        await update.message.reply_html("❌ Folder name khali nahi ho sakta. Kripya naam type karein:")
        return True
        
    parent_id = state_data.get("parent_id", "root") if state_data else "root"
    clear_user_state(user_id)
    
    status_msg = await update.message.reply_html(f"⏳ <i>Folder '{clean_html(folder_name)}' banaya ja raha hai...</i>")
    
    try:
        res = await create_folder(api_key, folder_name, parent_id=parent_id)
        if res.get("status") == "success":
            await status_msg.edit_text(
                f"✅ <b>Folder '{clean_html(folder_name)}' successfully created!</b>",
                parse_mode="HTML"
            )
            # Show updated folders list
            folders_res = await list_folders(api_key, parent_id=parent_id)
            folders = folders_res.get("folders", [])
            kb = folders_list_kb(folders, current_parent=parent_id)
            await update.message.reply_html("📂 <b>Virtual Folders:</b>", reply_markup=kb)
        else:
            await status_msg.edit_text(
                f"❌ Folder create nahi ho paya: {clean_html(res.get('message', 'Error'))}",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Create folder error: {e}")
        await status_msg.edit_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")
        
    return True

async def folder_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm folder deletion."""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split(":")
    folder_id = parts[1]
    
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete Folder", callback_data=f"folder_del_do:{folder_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"folder_view:{folder_id}")
        ]
    ])
    text = f"⚠️ <b>Delete Folder Confirmation</b>\n\nKya aap folder <code>{clean_html(folder_id)}</code> ko delete karna chahte hain?"
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

async def folder_delete_do_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perform folder deletion."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    parts = query.data.split(":")
    folder_id = parts[1]

    try:
        res = await delete_folder(api_key, folder_id)
        if res.get("status") == "success":
            await query.edit_message_text(
                f"✅ <b>Folder successfully deleted!</b>",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
        else:
            await query.edit_message_text(
                f"❌ Delete failed: {clean_html(res.get('message', 'Error'))}",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Delete folder error: {e}")
        await query.edit_message_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")
