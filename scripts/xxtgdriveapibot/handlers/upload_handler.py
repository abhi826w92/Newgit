import io
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from database import get_user_api_key, get_user_folder
from api_client import upload_file
from keyboards import back_to_main_kb
from helpers import clean_html, format_bytes, get_mime_icon

logger = logging.getLogger(__name__)

# Telegram Bot API standard download limit is 20MB
MAX_BOT_DOWNLOAD_BYTES = 20 * 1024 * 1024

async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle files, photos, videos, audios, and documents sent by user."""
    message = update.message
    if not message:
        return
        
    user_id = update.effective_user.id
    api_key = get_user_api_key(user_id)
    
    if not api_key:
        await message.reply_html(
            "❌ <b>API Key Connected Nahi Hai!</b>\n\n"
            "File upload karne ke liye pehle apni TG Drive API key set karein.\n"
            "Kripya <b>/start</b> send karein.",
            disable_web_page_preview=True
        )
        return

    # Extract media metadata first WITHOUT calling get_file()
    media_obj = None
    filename = "file"
    mime_type = "application/octet-stream"
    file_size = 0

    if message.document:
        doc = message.document
        filename = doc.file_name or "document"
        mime_type = doc.mime_type or "application/octet-stream"
        file_size = doc.file_size or 0
        media_obj = doc
    elif message.video:
        vid = message.video
        filename = vid.file_name or f"video_{vid.file_unique_id}.mp4"
        mime_type = vid.mime_type or "video/mp4"
        file_size = vid.file_size or 0
        media_obj = vid
    elif message.audio:
        aud = message.audio
        filename = aud.file_name or f"audio_{aud.file_unique_id}.mp3"
        mime_type = aud.mime_type or "audio/mpeg"
        file_size = aud.file_size or 0
        media_obj = aud
    elif message.photo:
        photo = message.photo[-1]
        filename = f"photo_{photo.file_unique_id}.jpg"
        mime_type = "image/jpeg"
        file_size = photo.file_size or 0
        media_obj = photo
    elif message.voice:
        v = message.voice
        filename = f"voice_{v.file_unique_id}.ogg"
        mime_type = v.mime_type or "audio/ogg"
        file_size = v.file_size or 0
        media_obj = v
    elif message.animation:
        anim = message.animation
        filename = anim.file_name or f"animation_{anim.file_unique_id}.mp4"
        mime_type = anim.mime_type or "video/mp4"
        file_size = anim.file_size or 0
        media_obj = anim
    else:
        return

    icon = get_mime_icon(mime_type, filename)

    # 1. Check size limit BEFORE attempting get_file()
    if file_size > MAX_BOT_DOWNLOAD_BYTES:
        await message.reply_html(
            f"⚠️ <b>File Size Limit Exceeded (Max 20MB for Telegram Bots)</b>\n\n"
            f"{icon} <b>File:</b> <code>{clean_html(filename)}</code>\n"
            f"📦 <b>Size:</b> <code>{format_bytes(file_size)}</code>\n\n"
            f"ℹ️ <i>Telegram Bot API standard bots ko maximum 20MB tak ki files download karne allow karta hai.</i>\n\n"
            f"💡 <b>20MB se badi files ke liye:</b>\n"
            f"Aap directly <a href=\"https://tgdriveo.pages.dev\">TG Drive Web App</a> par jaakar <b>2GB / 4GB</b> tak ki koi bhi file direct upload kar sakte hain!",
            disable_web_page_preview=True
        )
        return

    status_msg = await message.reply_html(f"⏳ <i>Downloading {icon} <b>{clean_html(filename)}</b> ({format_bytes(file_size)})...</i>")

    try:
        # Retrieve telegram file
        try:
            tg_file = await media_obj.get_file()
        except BadRequest as e:
            if "File is too big" in str(e):
                await status_msg.edit_text(
                    f"⚠️ <b>File is too big!</b>\n\n"
                    f"Aapki file ka size 20MB se jyada hai jise Telegram Bot direct download nahi kar sakta.\n"
                    f"Kripya 20MB se chhoti file bhejein ya <a href=\"https://tgdriveo.pages.dev\">TG Drive Web App</a> use karein.",
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                return
            raise e

        # Download file bytes
        file_byte_array = await tg_file.download_as_bytearray()
        file_bytes = bytes(file_byte_array)
        
        await status_msg.edit_text(f"🚀 <i>Uploading {icon} <b>{clean_html(filename)}</b> to TG Drive Cloud...</i>", parse_mode="HTML")
        
        folder_id = get_user_folder(user_id) or "root"
        
        # Upload via TG Drive API
        upload_res = await upload_file(api_key, file_bytes, filename, folder_id=folder_id, mime_type=mime_type)
        
        if upload_res.get("status") == "success":
            data = upload_res.get("data", {})
            file_id = data.get("id") or data.get("message_id")
            final_name = data.get("name", filename)
            final_size = data.get("size", len(file_bytes))
            dl_url = data.get("download_url") or f"https://tgdriveapi.youganksaini1.workers.dev/v1/files/{file_id}/download"
            dest = data.get("destination", "Saved Messages ('me')")
            
            text = (
                f"🎉 <b>FILE UPLOADED SUCCESSFULLY!</b>\n\n"
                f"{icon} <b>Name:</b> <code>{clean_html(final_name)}</code>\n"
                f"📦 <b>Size:</b> <code>{format_bytes(final_size)}</code>\n"
                f"🆔 <b>File ID:</b> <code>#{file_id}</code>\n"
                f"📂 <b>Folder:</b> <code>{clean_html(folder_id)}</code>\n"
                f"☁️ <b>Storage:</b> {clean_html(dest)}\n\n"
                f"🔗 <b>Direct Link:</b>\n<code>{dl_url}</code>"
            )
            
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬇️ Direct Fast Download Link", url=dl_url)],
                [
                    InlineKeyboardButton("⭐ Star File", callback_data=f"file_star:{file_id}:{folder_id}:1"),
                    InlineKeyboardButton("🗑️ Delete", callback_data=f"file_del_confirm:{file_id}:{folder_id}:1")
                ],
                [InlineKeyboardButton("📁 View in Files", callback_data=f"menu_files:{folder_id}:1")]
            ])
            
            await status_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            err_msg = upload_res.get("message", "Upload failed")
            await status_msg.edit_text(
                f"❌ <b>Upload Failed!</b>\n\n<b>Reason:</b> {clean_html(err_msg)}",
                reply_markup=back_to_main_kb(),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Media upload error: {e}")
        await status_msg.edit_text(
            f"❌ <b>Upload Error:</b> {clean_html(str(e))}",
            reply_markup=back_to_main_kb(),
            parse_mode="HTML"
        )
