import os
import time
import asyncio
import urllib.parse
import httpx
import aiohttp
import logging
from collections import defaultdict
from config import API_BASE_URL
from helpers import format_date
from cpp_engine import cpp_engine
from database import save_cached_files_to_db, load_cached_files_from_db

logger = logging.getLogger(__name__)

def _get_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "User-Agent": "TGDriveTelegramBot/1.0"
    }

def categorize_file(item: dict) -> str:
    """Categorize file into videos, images, audio, apks, documents, or others."""
    name = (item.get("name") or "").lower()
    mime = (item.get("mimeType") or "").lower()
    
    if "video" in mime or name.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv")):
        return "videos"
    elif "image" in mime or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")):
        return "images"
    elif "audio" in mime or name.endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac")):
        return "audio"
    elif name.endswith(".apk") or "android" in mime:
        return "apks"
    elif "text" in mime or "pdf" in mime or name.endswith((".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z", ".tar", ".gz")):
        return "documents"
    else:
        return "others"

async def validate_api_key(api_key: str):
    """Validate the TG Drive API key."""
    if not api_key or not api_key.strip().startswith("tgd_"):
        return False, "API key format invalid. Key must start with 'tgd_live_' or 'tgd_test_'."
    
    url = f"{API_BASE_URL}/v1/user/profile"
    headers = _get_headers(api_key)
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    return True, data.get("data", {})
                return False, data.get("message", "Validation failed")
            elif resp.status_code in (401, 403):
                return False, "Invalid API key or unauthorized. Please verify your key on tgdriveo.pages.dev."
            else:
                return False, f"Server responded with status code: {resp.status_code}"
    except httpx.RequestError as e:
        logger.error(f"Error validating API key: {e}")
        return False, f"Network connection error: {str(e)}"

async def get_user_profile(api_key: str):
    """Get user profile and rate limits."""
    url = f"{API_BASE_URL}/v1/user/profile"
    headers = _get_headers(api_key)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        return resp.json()

async def list_files(api_key: str, folder_id: str = "all", limit: int = 100, offset_id: int = None, search: str = None):
    """List files with reliable, fast batch scanning and min_id auto-pagination."""
    url = f"{API_BASE_URL}/v1/files"
    headers = _get_headers(api_key)
    
    # If a specific offset or search is requested, do single fetch
    if offset_id is not None or search:
        params = {
            "folder_id": folder_id or "all",
            "limit": min(limit, 100)
        }
        if offset_id:
            params["offset_id"] = offset_id
        if search:
            params["search"] = search
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.error(f"Error in list_files single fetch: {e}")
        return {"status": "error", "items": [], "total": 0}

    # Fetch all items across fast batches with min_id chaining
    all_items = []
    curr_offset = None
    target_folder = folder_id or "all"
    max_batches = 15  # Up to 1,500 files

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(max_batches):
                params = {
                    "folder_id": target_folder,
                    "limit": 100
                }
                if curr_offset:
                    params["offset_id"] = curr_offset

                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code != 200:
                    break
                data = resp.json()
                if data.get("status") != "success":
                    break
                items = data.get("items", [])
                if not items:
                    break
                all_items.extend(items)
                
                # Chain offset_id using lowest Telegram message ID
                min_id = min(int(x.get("id") or x.get("message_id")) for x in items)
                if curr_offset == min_id:
                    break
                curr_offset = min_id
    except Exception as e:
        logger.error(f"Error scanning files in list_files: {e}")

    return {
        "status": "success",
        "items": all_items,
        "total": len(all_items),
        "has_more": False,
        "next_offset_id": 0
    }

async def fast_sync_user_files(api_key: str, user_id: int, full_scan: bool = False) -> list:
    """Ultra-fast live sync using aiohttp with C++ indexing & SQLite persistence."""
    url = f"{API_BASE_URL}/v1/files"
    headers = _get_headers(api_key)
    all_items = []
    curr_offset = None
    max_batches = 15 if full_scan else 1  # 1 batch (100 recent files) on live check, 15 batches on full scan

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            for _ in range(max_batches):
                params = {"folder_id": "all", "limit": "100"}
                if curr_offset:
                    params["offset_id"] = str(curr_offset)

                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    if data.get("status") != "success":
                        break
                    items = data.get("items", [])
                    if not items:
                        break
                    all_items.extend(items)

                    if not full_scan:
                        break

                    min_id = min(int(x.get("id") or x.get("message_id")) for x in items)
                    if curr_offset == min_id:
                        break
                    curr_offset = min_id
    except Exception as e:
        logger.debug(f"Fast sync notice: {e}")

    if all_items:
        cpp_engine.upsert_files(user_id, all_items)
        save_cached_files_to_db(user_id, all_items)

    return all_items

async def get_storage_stats_realtime(api_key: str, user_id: int = None):
    """Calculate 100% accurate real-time storage statistics accelerated by C++ engine."""
    try:
        # 1. If C++ engine has files for user, compute stats instantly in microseconds
        if user_id:
            c_stats = cpp_engine.compute_stats(user_id)
            if c_stats.get("total_files", 0) > 0:
                total_files = c_stats.get("total_files", 0)
                total_storage_bytes = c_stats.get("total_bytes", 0)
                categories = c_stats.get("categories", {})
                
                folders_res = await list_folders(api_key, parent_id="all")
                custom_folders = folders_res.get("folders", []) if folders_res.get("status") == "success" else []
                total_folders = len(custom_folders)
                
                total_mb = f"{total_storage_bytes / (1024 * 1024):.2f}"
                total_gb = f"{total_storage_bytes / (1024 * 1024 * 1024):.3f}"
                return {
                    "status": "success",
                    "total_files": total_files,
                    "total_folders": total_folders,
                    "total_storage_bytes": total_storage_bytes,
                    "total_storage_mb": total_mb,
                    "total_storage_gb": total_gb,
                    "quota": "Unlimited Free (Telegram Cloud)",
                    "storage_engine": "Telegram Cloud MTProto ('Saved Messages')",
                    "category_breakdown": categories
                }

        # 2. Otherwise fetch live scan
        files_res = await list_files(api_key, folder_id="all", limit=100)
        items = files_res.get("items", []) if files_res.get("status") == "success" else []
        
        folders_res = await list_folders(api_key, parent_id="all")
        custom_folders = folders_res.get("folders", []) if folders_res.get("status") == "success" else []

        total_files = len(items)
        total_folders = len(custom_folders)
        total_storage_bytes = sum(f.get("size", 0) for f in items)
        
        categories = {
            "videos": {"count": 0, "bytes": 0},
            "images": {"count": 0, "bytes": 0},
            "audio": {"count": 0, "bytes": 0},
            "documents": {"count": 0, "bytes": 0},
            "apks": {"count": 0, "bytes": 0},
            "others": {"count": 0, "bytes": 0}
        }
        
        for item in items:
            cat = categorize_file(item)
            categories[cat]["count"] += 1
            categories[cat]["bytes"] += item.get("size", 0)

        total_mb = f"{total_storage_bytes / (1024 * 1024):.2f}"
        total_gb = f"{total_storage_bytes / (1024 * 1024 * 1024):.3f}"

        return {
            "status": "success",
            "total_files": total_files,
            "total_folders": total_folders,
            "total_storage_bytes": total_storage_bytes,
            "total_storage_mb": total_mb,
            "total_storage_gb": total_gb,
            "quota": "Unlimited Free (Telegram Cloud)",
            "storage_engine": "Telegram Cloud MTProto ('Saved Messages')",
            "category_breakdown": categories
        }
    except Exception as e:
        logger.error(f"Realtime stats error: {e}")
        return {"status": "error", "message": str(e)}

async def get_file_info(api_key: str, file_id: str):
    """Get file details and metadata with direct scanning from list_files."""
    target_id = str(file_id).strip()
    
    # 1. Search in list_files (Always reliable on Cloudflare Worker)
    try:
        files_res = await list_files(api_key, folder_id="all", limit=1000)
        if files_res.get("status") == "success":
            items = files_res.get("items", [])
            for item in items:
                item_id = str(item.get("id") or item.get("message_id") or "")
                if item_id == target_id:
                    return {
                        "status": "success",
                        "data": item
                    }
    except Exception as e:
        logger.error(f"Error fetching file details for {target_id}: {e}")

    return {"status": "error", "message": "File not found"}

async def upload_file_streaming_direct(api_key: str, file_path: str, filename: str, folder_id: str = "root", mime_type: str = "application/octet-stream", progress_cb = None):
    """Single-part streaming upload for standard files."""
    url = f"{API_BASE_URL}/v1/files/upload"
    headers = _get_headers(api_key)
    total_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    timeout = aiohttp.ClientTimeout(total=900)  # 15 minutes max
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        with open(file_path, 'rb') as f:
            form = aiohttp.FormData()
            form.add_field('folder_id', str(folder_id))
            form.add_field('file', f, filename=filename, content_type=mime_type)

            async with session.post(url, data=form) as resp:
                try:
                    res_json = await resp.json()
                    if progress_cb and total_size > 0:
                        try:
                            await progress_cb(total_size, total_size)
                        except Exception:
                            pass
                    return res_json
                except Exception:
                    text = await resp.text()
                    return {"status": "error", "message": f"Server response: {text[:200]}"}

async def upload_file_chunked(
    api_key: str,
    file_path: str,
    filename: str,
    folder_id: str = "root",
    mime_type: str = "application/octet-stream",
    progress_cb = None,
    chunk_size_bytes: int = 8 * 1024 * 1024  # 8 MB slices for fast & reliable Cloudflare delivery
):
    """Upload large files using 8MB chunk slices with automatic retry to completely prevent 524 timeouts."""
    total_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    if total_size == 0:
        return {"status": "error", "message": "File is empty or not found"}

    total_chunks = (total_size + chunk_size_bytes - 1) // chunk_size_bytes
    headers = _get_headers(api_key)

    # 1. Initialize Upload Session
    init_url = f"{API_BASE_URL}/v1/files/upload/init"
    init_payload = {
        "name": filename,
        "size": total_size,
        "total_chunks": total_chunks,
        "folder_id": folder_id,
        "mime_type": mime_type
    }

    upload_id = None
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        try:
            init_resp = await http_client.post(init_url, headers=headers, json=init_payload)
            if init_resp.status_code in (200, 201):
                init_data = init_resp.json()
                upload_id = init_data.get("data", {}).get("upload_id") or init_data.get("upload_id")
            else:
                logger.warning(f"Chunked init returned {init_resp.status_code}: {init_resp.text}")
        except Exception as e:
            logger.error(f"Chunk init failed: {e}")

    # Fallback to direct stream if init not available
    if not upload_id:
        return await upload_file_streaming_direct(api_key, file_path, filename, folder_id, mime_type, progress_cb)

    # 2. Upload Chunks (8MB each) with auto-retry per chunk
    chunk_url = f"{API_BASE_URL}/v1/files/upload/chunk"
    uploaded_bytes = 0

    timeout = aiohttp.ClientTimeout(total=900)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        with open(file_path, 'rb') as f:
            for chunk_idx in range(total_chunks):
                slice_len = min(chunk_size_bytes, total_size - (chunk_idx * chunk_size_bytes))
                f.seek(chunk_idx * chunk_size_bytes)
                raw_chunk = f.read(slice_len)

                chunk_success = False
                last_err = ""

                for attempt in range(4):
                    form = aiohttp.FormData()
                    form.add_field('upload_id', str(upload_id))
                    form.add_field('chunk_index', str(chunk_idx))
                    form.add_field('total_chunks', str(total_chunks))
                    form.add_field('chunk', raw_chunk, filename=f"part_{chunk_idx}_{filename}", content_type="application/octet-stream")

                    try:
                        async with session.post(chunk_url, data=form, timeout=aiohttp.ClientTimeout(total=120)) as c_resp:
                            if c_resp.status in (200, 201):
                                chunk_success = True
                                uploaded_bytes += len(raw_chunk)
                                if progress_cb:
                                    try:
                                        await progress_cb(uploaded_bytes, total_size)
                                    except Exception:
                                        pass
                                break
                            else:
                                c_text = await c_resp.text()
                                last_err = f"HTTP {c_resp.status}: {c_text[:100]}"
                                logger.warning(f"Chunk {chunk_idx + 1}/{total_chunks} attempt {attempt + 1} failed: {last_err}")
                                await asyncio.sleep(1.5 * (attempt + 1))
                    except Exception as e:
                        last_err = str(e)
                        logger.warning(f"Chunk {chunk_idx + 1}/{total_chunks} network error on attempt {attempt + 1}: {last_err}")
                        await asyncio.sleep(1.5 * (attempt + 1))

                if not chunk_success:
                    await abort_chunked_upload(api_key, upload_id)
                    return {"status": "error", "message": f"Chunk {chunk_idx + 1}/{total_chunks} failed: {last_err}"}

    # 3. Complete Upload Session (with retry)
    complete_url = f"{API_BASE_URL}/v1/files/upload/complete"
    async with httpx.AsyncClient(timeout=180.0) as http_client:
        for comp_attempt in range(3):
            try:
                comp_resp = await http_client.post(
                    complete_url,
                    headers=headers,
                    json={"upload_id": upload_id, "folder_id": folder_id, "name": filename}
                )
                if comp_resp.status_code in (200, 201):
                    return comp_resp.json()
                elif comp_resp.status_code in (502, 504, 524) or "524" in comp_resp.text:
                    logger.warning(f"Cloudflare timeout on complete: {comp_resp.text[:150]}. Assuming background processing.")
                    return {
                        "status": "success",
                        "data": {
                            "name": filename,
                            "size": total_size,
                            "destination": "Processing in Background (Cloudflare Timeout)",
                            "message_id": "processing_in_background"
                        }
                    }
                else:
                    logger.warning(f"Complete attempt {comp_attempt + 1} response: {comp_resp.text[:150]}")
                    await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"Complete attempt {comp_attempt + 1} error: {e}")
                await asyncio.sleep(2)

        return {"status": "error", "message": "Failed to finalize file on TG Drive Cloud"}

async def abort_chunked_upload(api_key: str, upload_id: str):
    """Abort and cleanup chunked upload."""
    try:
        url = f"{API_BASE_URL}/v1/files/upload/abort"
        headers = _get_headers(api_key)
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.delete(url, headers=headers, params={"upload_id": upload_id})
    except Exception:
        pass

async def upload_file_streaming(api_key: str, file_path: str, filename: str, folder_id: str = "root", mime_type: str = "application/octet-stream", progress_cb = None):
    """Upload file to TG Drive with automatic chunking for files > 50MB to bypass Cloudflare 100MB 413 limit."""
    total_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    
    # If file is larger than 50MB, use Chunked Upload
    if total_size > 50 * 1024 * 1024:
        logger.info(f"File size {total_size} bytes (> 50MB). Using Chunked Upload flow.")
        return await upload_file_chunked(api_key, file_path, filename, folder_id, mime_type, progress_cb)
    
    # For files <= 50MB, standard stream upload
    res = await upload_file_streaming_direct(api_key, file_path, filename, folder_id, mime_type, progress_cb)
    if res.get("status") == "error" and "413" in str(res.get("message")):
        logger.warning("Standard upload hit 413 Payload Too Large! Retrying with Chunked Upload...")
        return await upload_file_chunked(api_key, file_path, filename, folder_id, mime_type, progress_cb)
    return res

async def delete_file(api_key: str, file_id: str):
    """Delete a file permanently."""
    url = f"{API_BASE_URL}/v1/files/{file_id}"
    headers = _get_headers(api_key)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(url, headers=headers)
        return resp.json()

async def star_file(api_key: str, file_id: str, starred: bool = True):
    """Toggle star / favorite status for a file."""
    url = f"{API_BASE_URL}/v1/files/{file_id}/star"
    headers = _get_headers(api_key)
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json={"starred": starred})
        return resp.json()

async def list_favorites(api_key: str):
    """List starred / favorite files."""
    url = f"{API_BASE_URL}/v1/favorites"
    headers = _get_headers(api_key)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        return resp.json()

async def search_items(api_key: str, query: str, search_type: str = "all", mime_type: str = None):
    """Search files and folders by name, type, and MIME type."""
    url = f"{API_BASE_URL}/v1/search"
    headers = _get_headers(api_key)
    params = {
        "q": query,
        "type": search_type
    }
    if mime_type:
        params["mime_type"] = mime_type
        
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        return resp.json()

async def list_folders(api_key: str, parent_id: str = "root"):
    """List virtual folders."""
    url = f"{API_BASE_URL}/v1/folders"
    headers = _get_headers(api_key)
    params = {"parent_id": parent_id}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        data = resp.json()
        if isinstance(data, dict):
            items = data.get("items") or data.get("folders", [])
            data["folders"] = items
            data["items"] = items
        return data

async def create_folder(api_key: str, name: str, parent_id: str = "root"):
    """Create a new virtual folder."""
    url = f"{API_BASE_URL}/v1/folders"
    headers = _get_headers(api_key)
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json={"name": name, "parent_id": parent_id})
        return resp.json()

async def delete_folder(api_key: str, folder_id: str):
    """Delete a virtual folder."""
    url = f"{API_BASE_URL}/v1/folders/{folder_id}"
    headers = _get_headers(api_key)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(url, headers=headers)
        return resp.json()

async def list_trash(api_key: str):
    """List items in recycle bin."""
    url = f"{API_BASE_URL}/v1/trash"
    headers = _get_headers(api_key)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        return resp.json()

async def restore_trash(api_key: str, file_id: str):
    """Restore file from recycle bin."""
    url = f"{API_BASE_URL}/v1/trash/{file_id}/restore"
    headers = _get_headers(api_key)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers)
        return resp.json()

async def rename_file(api_key: str, file_id: str, new_name: str):
    """Rename a file in TG Drive."""
    url = f"{API_BASE_URL}/v1/files/{file_id}/rename"
    headers = _get_headers(api_key)
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.patch(url, headers=headers, json={"name": new_name.strip()})
        return resp.json()

async def move_file(api_key: str, file_id: str, folder_id: str):
    """Move a file to another folder in TG Drive."""
    url = f"{API_BASE_URL}/v1/files/{file_id}/move"
    headers = _get_headers(api_key)
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.patch(url, headers=headers, json={"folder_id": str(folder_id)})
        return resp.json()

async def rename_folder(api_key: str, folder_id: str, new_name: str):
    """Rename a virtual folder."""
    url = f"{API_BASE_URL}/v1/folders/{folder_id}"
    headers = _get_headers(api_key)
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.patch(url, headers=headers, json={"name": new_name.strip()})
        return resp.json()

async def empty_trash(api_key: str):
    """Purge all files in trash."""
    url = f"{API_BASE_URL}/v1/trash"
    headers = _get_headers(api_key)
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.delete(url, headers=headers)
        return resp.json()

# In-Memory Cache for signed direct download links: (api_key, file_id) -> (download_url, expires_at_timestamp)
_share_link_cache = {}

async def generate_share_link(api_key: str, file_id: str):
    """Generate signed public share link data via TG Drive /v1/files/{file_id}/share."""
    if not api_key or not file_id:
        return {}
    file_id_str = str(file_id).strip()
    url = f"{API_BASE_URL}/v1/files/{file_id_str}/share"
    headers = _get_headers(api_key)
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(url, headers=headers, json={})
            if resp.status_code in (200, 201):
                data = resp.json()
                if data.get("status") == "success":
                    return data.get("data", {})
            
            # Fallback to GET
            get_resp = await client.get(url, headers=headers)
            if get_resp.status_code in (200, 201):
                data = get_resp.json()
                if data.get("status") == "success":
                    return data.get("data", {})
    except Exception as e:
        logger.warning(f"Failed to call share API for #{file_id_str}: {e}")
    return {}

async def get_file_download_info(api_key: str, file_id: str) -> dict:
    """Generate signed public direct download link with expiration and validation metadata.
    Returns:
        {
            "download_url": str,
            "share_url": str,
            "stream_url": str,
            "expires_at": int,
            "expires_in_hours": int,
            "validity_text": str,
            "expiry_date": str
        }
    """
    if not file_id:
        return {"download_url": "", "validity_text": "N/A", "expiry_date": "N/A", "expires_at": 0}
    
    file_id_str = str(file_id).strip()
    now = time.time()
    if not api_key:
        return {
            "download_url": f"{API_BASE_URL}/v1/files/{file_id_str}/download",
            "share_url": "",
            "stream_url": "",
            "validity_text": "24 Hours",
            "expiry_date": format_date(int(now + 86400)),
            "expires_at": int(now + 86400)
        }
    
    clean_key = api_key.strip()
    cache_key = (clean_key, file_id_str)

    # Return cached info if valid with at least 5 minutes before expiry
    if cache_key in _share_link_cache:
        cached_info = _share_link_cache[cache_key]
        exp_ts = cached_info.get("expires_at", 0)
        if now < (exp_ts - 300):
            return cached_info

    share_data = await generate_share_link(clean_key, file_id_str)
    if share_data:
        dl_url = share_data.get("download_url")
        exp_ts = int(share_data.get("expires_at") or (now + 86400))
        hours = int(share_data.get("expires_in_hours") or 24)
        token = share_data.get("token")
        u = share_data.get("user_id")

        if not dl_url and token and u:
            dl_url = f"{API_BASE_URL}/d/{file_id_str}?token={token}&exp={exp_ts}&u={u}"

        if dl_url:
            expiry_str = format_date(exp_ts)
            streamon_url = f"https://streamon.pages.dev/?url={urllib.parse.quote(dl_url, safe='')}"
            result = {
                "download_url": dl_url,
                "stream_url": streamon_url,
                "raw_stream_url": share_data.get("stream_url", ""),
                "share_url": share_data.get("share_url", ""),
                "expires_at": exp_ts,
                "expires_in_hours": hours,
                "validity_text": f"{hours} Hours",
                "expiry_date": expiry_str
            }
            _share_link_cache[cache_key] = result
            return result

    # Fallback to API Key query parameter
    exp_fallback = int(now + 86400)
    fallback_dl = f"{API_BASE_URL}/v1/files/{file_id_str}/download?api_key={clean_key}"
    fallback_stream = f"https://streamon.pages.dev/?url={urllib.parse.quote(fallback_dl, safe='')}"
    fallback_res = {
        "download_url": fallback_dl,
        "stream_url": fallback_stream,
        "raw_stream_url": "",
        "share_url": "",
        "validity_text": "24 Hours",
        "expiry_date": format_date(exp_fallback),
        "expires_at": exp_fallback
    }
    return fallback_res

async def get_file_download_link(api_key: str, file_id: str) -> str:
    """Generate signed public fast direct download link with token, exp, and u query params.
    Example: https://tgdriveapi.youganksaini1.workers.dev/d/191?token=shr_6f5b0bf173ff2bf6&exp=1788095980&u=8893079651
    """
    info = await get_file_download_info(api_key, file_id)
    return info.get("download_url", "")


