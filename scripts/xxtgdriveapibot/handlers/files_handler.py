import logging
from telegram import Update
from telegram.ext import ContextTypes
from database import get_user_api_key
from api_client import list_files, get_file_info, delete_file, star_file, list_favorites
from keyboards import files_list_kb, file_details_kb, delete_confirm_kb, favorites_kb, back_to_main_kb
from helpers import clean_html, format_bytes, format_date, get_mime_icon

logger = logging.getLogger(__name__)

PER_PAGE = 6

async def list_files_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle browsing files with pagination."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    if not api_key:
        await query.edit_message_text("❌ Kripya pehle /start bhej kar apni API key connect karein.")
        return
        
    data_parts = query.data.split(":")
    folder_id = data_parts[1] if len(data_parts) > 1 else "root"
    page = int(data_parts[2]) if len(data_parts) > 2 and data_parts[2].isdigit() else 1

    try:
        # Fetch files from TG Drive API
        res = await list_files(api_key, folder_id=folder_id, limit=50)
        
        if res.get("status") == "success":
            items = res.get("items", [])
            total_items = len(items)
            
            if not items:
                text = (
                    f"📁 <b>My Files (Folder: <code>{clean_html(folder_id)}</code>)</b>\n\n"
                    f"<i>Is folder me abhi koi files nahi hain.</i>\n\n"
                    f"📤 File upload karne ke liye koi bhi file is chat me send karein!"
                )
                await query.edit_message_text(text, reply_markup=back_to_main_kb(), parse_mode="HTML")
                return

            text = (
                f"📁 <b>My Files (Folder: <code>{clean_html(folder_id)}</code>)</b>\n\n"
                f"Total Files: <b>{total_items}</b>\n"
                f"<i>Kissi bhi file par click karke details aur download link dekhein:</i>"
            )
            
            kb = files_list_kb(items, page, total_items, per_page=PER_PAGE, folder_id=folder_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await query.edit_message_text(
                f"❌ Error fetching files: {clean_html(res.get('message', 'Unknown error'))}",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error in list_files: {e}")
        await query.edit_message_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")

async def file_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show details of a specific file."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    if not api_key:
        await query.edit_message_text("❌ Kripya pehle apni API key connect karein.")
        return
        
    parts = query.data.split(":")
    file_id = parts[1]
    folder_id = parts[2] if len(parts) > 2 else "root"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1

    try:
        res = await get_file_info(api_key, file_id)
        if res.get("status") == "success":
            file_data = res.get("data", res)
            name = file_data.get("name", "Untitled")
            size = file_data.get("size", 0)
            mime = file_data.get("mimeType", "N/A")
            created_at = file_data.get("created_at")
            download_url = file_data.get("download_url") or f"https://tgdriveapi.youganksaini1.workers.dev/v1/files/{file_id}/download"
            is_starred = file_data.get("starred", False)
            dest = file_data.get("destination", "Telegram Cloud")
            icon = get_mime_icon(mime, name)
            
            text = (
                f"{icon} <b>FILE DETAILS</b>\n\n"
                f"🏷️ <b>Name:</b> <code>{clean_html(name)}</code>\n"
                f"📦 <b>Size:</b> <code>{format_bytes(size)}</code>\n"
                f"📑 <b>MIME Type:</b> <code>{clean_html(mime)}</code>\n"
                f"🆔 <b>Message ID:</b> <code>#{file_id}</code>\n"
                f"📂 <b>Folder:</b> <code>{clean_html(folder_id)}</code>\n"
                f"📅 <b>Uploaded:</b> {format_date(created_at)}\n"
                f"📍 <b>Storage:</b> {clean_html(dest)}\n"
            )
            
            kb = file_details_kb(file_id, is_starred=is_starred, download_url=download_url, folder_id=folder_id, page=page)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await query.edit_message_text(
                f"❌ File information not found: {clean_html(res.get('message', 'Not found'))}",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error fetching file details: {e}")
        await query.edit_message_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")

async def file_star_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Star or unstar a file."""
    query = update.callback_query
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    parts = query.data.split(":")
    action = parts[0]
    file_id = parts[1]
    folder_id = parts[2] if len(parts) > 2 else "root"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    
    star_val = (action == "file_star")
    
    try:
        await star_file(api_key, file_id, starred=star_val)
        await query.answer("⭐ Starred!" if star_val else "Removed from Starred!")
        # Reload file view
        query.data = f"file_view:{file_id}:{folder_id}:{page}"
        await file_view_callback(update, context)
    except Exception as e:
        logger.error(f"Error starring file: {e}")
        await query.answer(f"Error: {str(e)}", show_alert=True)

async def file_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show delete confirmation prompt."""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split(":")
    file_id = parts[1]
    folder_id = parts[2] if len(parts) > 2 else "root"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    
    text = (
        f"⚠️ <b>Delete Confirmation</b>\n\n"
        f"Kya aap File <code>#{file_id}</code> ko permanently delete karna chahte hain?\n\n"
        f"<i>Yeh file Telegram Cloud aur TG Drive se delete ho jayegi.</i>"
    )
    await query.edit_message_text(text, reply_markup=delete_confirm_kb(file_id, folder_id, page), parse_mode="HTML")

async def file_delete_do_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permanently delete file."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    parts = query.data.split(":")
    file_id = parts[1]
    folder_id = parts[2] if len(parts) > 2 else "root"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1

    try:
        res = await delete_file(api_key, file_id)
        if res.get("status") == "success":
            await query.edit_message_text(
                f"✅ <b>File #{file_id} successfully deleted!</b>",
                reply_markup=files_list_kb([], 1, 0, folder_id=folder_id),
                parse_mode="HTML"
            )
            # Redirect to files list
            query.data = f"menu_files:{folder_id}:{page}"
            await list_files_callback(update, context)
        else:
            await query.edit_message_text(
                f"❌ Delete failed: {clean_html(res.get('message', 'Error'))}",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error deleting file: {e}")
        await query.edit_message_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")

async def menu_favorites_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List starred files."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    if not api_key:
        await query.edit_message_text("❌ Kripya pehle apni API key connect karein.")
        return

    try:
        res = await list_favorites(api_key)
        items = res.get("items", [])
        
        if not items:
            text = (
                "⭐ <b>STARRED FILES</b>\n\n"
                "<i>Aapki koi bhi starred / favorite file nahi hai.</i>\n\n"
                "Kissi bhi file ke details page par jaakar <b>⭐ Star</b> button daba kar favorite mark kar sakte hain."
            )
            await query.edit_message_text(text, reply_markup=back_to_main_kb(), parse_mode="HTML")
            return

        text = f"⭐ <b>STARRED FILES ({len(items)})</b>\n\n<i>Details dekhne ke liye file par click karein:</i>"
        await query.edit_message_text(text, reply_markup=favorites_kb(items), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Favorites error: {e}")
        await query.edit_message_text(f"❌ Error: {clean_html(str(e))}", reply_markup=back_to_main_kb(), parse_mode="HTML")
