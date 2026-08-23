import os
import sys
import io
import re
import json
import time
import uuid
import base64
import struct
import shutil
import asyncio
import subprocess

# Auto-Install Missing Dependencies on First Launch
REQUIRED_PACKAGES = [
    "wheel",
    "setuptools",
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.34.0",
    "telethon>=1.38.0",
    "tgcrypto>=1.2.5",
    "python-multipart>=0.0.20",
    "httpx>=0.28.0",
    "python-dotenv>=1.0.0"
]

def ensure_dependencies():
    missing = []
    checks = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "telethon": "telethon",
        "multipart": "python-multipart",
        "httpx": "httpx"
    }
    for mod, pkg in checks.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"📦 [Auto-Installer] Missing packages detected: {missing}. Installing dependencies automatically...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", *REQUIRED_PACKAGES])
            print("✅ [Auto-Installer] Dependencies installed successfully!")
        except Exception as e:
            print(f"⚠️ [Auto-Installer Warning] Automatic pip install failed: {e}")

ensure_dependencies()

from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo, DocumentAttributeAudio

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FOLDER_PREFIX = "#TG_DRIVE_FOLDER#"
FILE_PREFIX = "#TG_DRIVE_FILE#"

DEFAULT_NGROK_TOKEN = "3IBGFjZrUBgDgqY1Hn3EIU20BXL_7DSo3P6LkVk1CrgNkg95Q"
CLOUDFLARE_BIN = "/tmp/cloudflared"
TUNNEL_LOG = "/tmp/cloudflared.log"
FIREBASE_DB_URL = "https://studio-2641678334-61796-default-rtdb.firebaseio.com"
SYNC_SECRET = os.environ.get("SYNC_SECRET", "tgdrive_live_auto_sync_secret_2026")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "https://tgdriveapi.youganksaini1.workers.dev")

def normalize_telethon_session(session_str: str) -> str:
    """
    Normalizes a GramJS or Telethon string session into a standard Telethon StringSession.
    """
    if not session_str:
        return session_str

    try:
        data = base64.urlsafe_b64decode(session_str + '=' * (-len(session_str) % 4))
        
        # Format 1: Telethon format (352 or 353 bytes starting with DC byte)
        if len(data) in (352, 353, 263, 264):
            return session_str

        # Format 2: GramJS Browser Session format
        if session_str.startswith('1'):
            decoded = base64.b64decode(session_str[1:] + '=' * (-len(session_str[1:]) % 4))
            dc_id = decoded[0]
            
            # Map official Telegram DC IP addresses
            DC_IPS = {
                1: "149.154.175.53",
                2: "149.154.167.51",
                3: "149.154.175.100",
                4: "149.154.167.91",
                5: "91.108.56.130"
            }
            
            ip_str = DC_IPS.get(dc_id, "91.108.56.130")
            ip_parts = [int(p) for p in ip_str.split('.')]
            ip_bin = bytes(ip_parts)
            port = 443
            auth_key = decoded[-256:]
            
            telethon_data = struct.pack('>B4sH256s', dc_id, ip_bin, port, auth_key)
            return '1' + base64.urlsafe_b64encode(telethon_data).decode('ascii')
            
    except Exception as e:
        print(f"[Session Parse Warning] {e}")

    return session_str

async def sync_url_to_gateway(url: str):
    """Auto-syncs live tunnel URL directly to Global Database & Cloudflare Gateway"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            # 1. Direct Firebase RTDB sync with secret authentication lock
            try:
                await client.put(
                    f"{FIREBASE_DB_URL}/system/live_backend.json",
                    json={
                        "backend_url": url,
                        "updated_at": asyncio.get_event_loop().time(),
                        "sync_secret": SYNC_SECRET
                    }
                )
                print("✅ [Auto-Sync] Live backend successfully synchronized to Global Database with secret lock!")
            except Exception as fb_err:
                print(f"[Firebase Sync Notice] {fb_err}")

            # 2. Cloudflare Gateway ping
            try:
                res = await client.post(
                    f"{GATEWAY_URL}/internal/update-tunnel",
                    headers={"X-Sync-Secret": SYNC_SECRET},
                    json={"backend_url": url}
                )
                if res.status_code == 200:
                    print("✅ [Auto-Sync] Live backend successfully synchronized to Cloudflare Gateway!")
            except Exception:
                pass
    except Exception as e:
        print(f"[Auto-Sync Error] {e}")

async def start_auto_tunnel():
    """
    Continuous Self-Healing Supervisor:
    Launches and monitors Cloudflare Unlimited Tunnel.
    If the tunnel drops or process restarts, it immediately relaunches
    and synchronizes the new URL to the Cloudflare Gateway.
    """
    await asyncio.sleep(2)
    port = int(os.environ.get("PORT", "8000"))

    # Download Cloudflare binary if not present
    try:
        if not os.path.exists(CLOUDFLARE_BIN):
            print("[Tunnel] Downloading standalone Cloudflare binary...")
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
            import httpx
            async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as http_client:
                r = await http_client.get(url)
                if r.status_code == 200:
                    with open(CLOUDFLARE_BIN, "wb") as f:
                        f.write(r.content)
                    os.chmod(CLOUDFLARE_BIN, 0o777)
                    print("[Tunnel] Cloudflare binary installed successfully!")
    except Exception as e:
        print(f"[Tunnel Binary Error] {e}")

    while True:
        try:
            if os.path.exists(CLOUDFLARE_BIN):
                try:
                    subprocess.run(["pkill", "-9", "-f", "cloudflared"], capture_output=True)
                except Exception:
                    pass
                await asyncio.sleep(1)

                if os.path.exists(TUNNEL_LOG):
                    try:
                        os.remove(TUNNEL_LOG)
                    except Exception:
                        pass

                log_file = open(TUNNEL_LOG, "w")
                proc = subprocess.Popen(
                    [CLOUDFLARE_BIN, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
                    stdout=log_file,
                    stderr=subprocess.STDOUT
                )

                # Monitor log for tunnel URL
                tunnel_found = False
                for _ in range(60):
                    await asyncio.sleep(1)
                    if os.path.exists(TUNNEL_LOG):
                        with open(TUNNEL_LOG, "r") as f:
                            content = f.read()
                            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", content)
                            if match:
                                tunnel_url = match.group(0)
                                print(f"🚀 [Tunnel Active] Cloudflare URL: {tunnel_url}")
                                await sync_url_to_gateway(tunnel_url)
                                tunnel_found = True
                                break

                if tunnel_found:
                    while proc.poll() is None:
                        await asyncio.sleep(10)

        except Exception as err:
            print(f"[Tunnel Supervisor Warning] {err}")

        print("[Tunnel] Re-launching tunnel in 5 seconds...")
        await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Launch auto-tunnel on startup in background
    asyncio.create_task(start_auto_tunnel())
    yield

app = FastAPI(
    title="TG Drive MTProto Microservice",
    version="3.1.0",
    description="High-performance, Zero-RAM Streaming Telegram MTProto storage backend with full Folders & Trash suite",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "service": "TG Drive Python MTProto Engine",
        "version": "3.1.0",
        "storage": "Telegram 'Saved Messages' ('me')",
        "features": [
            "Real #TG_DRIVE_FOLDER# Folders",
            "Folder-Linked Uploads (parentId)",
            "HTTP 206 Partial Range Video/Audio Streaming",
            "Soft Delete & Restore (Trash / Recycle Bin)",
            "Server-Side Fast Search",
            "Live Accurate Storage Analytics",
            "Cursor Pagination (1000+ files)"
        ],
        "status": "healthy"
    }

async def get_tg_client(session_string: str, api_id: int, api_hash: str) -> TelegramClient:
    """Helper to initialize and connect a Telethon MTProto client"""
    formatted_session = normalize_telethon_session(session_string)
    client = TelegramClient(
        StringSession(formatted_session),
        int(api_id),
        str(api_hash),
        connection_retries=5,
        timeout=30
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(status_code=401, detail="Invalid or expired Telegram MTProto session")
    return client

# ─────────────────────────────────────────────────────────────
# 1. CORE FOLDERS MANAGEMENT (#TG_DRIVE_FOLDER#)
# ─────────────────────────────────────────────────────────────

@app.get("/api/folders")
async def list_folders(
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...),
    parent_id: Optional[str] = Query(None),
    limit: int = Query(500)
):
    """
    Scans Telegram 'Saved Messages' ('me') for #TG_DRIVE_FOLDER# messages.
    Returns folders matching parent_id (or all non-trash folders).
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        folders = []
        async for msg in client.iter_messages('me', limit=limit):
            text = (msg.message or "").strip()
            if text.startswith(FOLDER_PREFIX):
                try:
                    meta = json.loads(text[len(FOLDER_PREFIX):])
                    f_parent = str(meta.get("parentId") or meta.get("folderId") or "root")
                    
                    # Filter by parent_id if specified (ignore 'trash' unless specifically asked)
                    if parent_id is not None:
                        if parent_id == "all" or f_parent == str(parent_id):
                            folders.append({
                                "id": str(msg.id),
                                "message_id": msg.id,
                                "name": meta.get("name", f"Folder_{msg.id}"),
                                "parentId": f_parent,
                                "parent_id": f_parent,
                                "created_at": int(msg.date.timestamp() * 1000) if msg.date else 0
                            })
                    else:
                        if f_parent != "trash":
                            folders.append({
                                "id": str(msg.id),
                                "message_id": msg.id,
                                "name": meta.get("name", f"Folder_{msg.id}"),
                                "parentId": f_parent,
                                "parent_id": f_parent,
                                "created_at": int(msg.date.timestamp() * 1000) if msg.date else 0
                            })
                except Exception:
                    pass

        return {
            "status": "success",
            "total": len(folders),
            "storage": "Telegram 'Saved Messages' ('me')",
            "items": folders
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list folders: {str(e)}")
    finally:
        await client.disconnect()

@app.post("/api/folders")
async def create_folder(
    name: str = Form(...),
    parent_id: str = Form("root"),
    session_string: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...)
):
    """
    Creates a folder in Telegram 'Saved Messages' ('me') by sending #TG_DRIVE_FOLDER#{"name":"...","parentId":"..."}
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        clean_name = name.strip() or "New Folder"
        clean_parent = parent_id.strip() or "root"
        folder_meta = {
            "name": clean_name,
            "parentId": clean_parent,
            "folderId": clean_parent,
            "created_at": int(asyncio.get_event_loop().time() * 1000)
        }
        text = f"{FOLDER_PREFIX}{json.dumps(folder_meta)}"
        msg = await client.send_message('me', text)

        return {
            "status": "success",
            "message": f"Folder '{clean_name}' created successfully in Telegram Saved Messages",
            "data": {
                "id": str(msg.id),
                "message_id": msg.id,
                "name": clean_name,
                "parentId": clean_parent,
                "parent_id": clean_parent,
                "created_at": int(msg.date.timestamp() * 1000) if msg.date else 0
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create folder: {str(e)}")
    finally:
        await client.disconnect()

@app.patch("/api/folders/{folder_id}")
@app.post("/api/folders/{folder_id}")
async def update_folder(
    folder_id: int,
    name: Optional[str] = Form(None),
    parent_id: Optional[str] = Form(None),
    session_string: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...)
):
    """
    Renames or moves a folder by editing its Telegram #TG_DRIVE_FOLDER# text message.
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        msg = await client.get_messages('me', ids=folder_id)
        if not msg:
            raise HTTPException(status_code=404, detail="Folder message not found in Saved Messages")

        current_meta = {}
        if msg.message and msg.message.startswith(FOLDER_PREFIX):
            try:
                current_meta = json.loads(msg.message[len(FOLDER_PREFIX):])
            except Exception:
                pass

        if name:
            current_meta["name"] = name.strip()
        if parent_id is not None:
            current_meta["parentId"] = parent_id.strip()
            current_meta["folderId"] = parent_id.strip()

        new_text = f"{FOLDER_PREFIX}{json.dumps(current_meta)}"
        await client.edit_message('me', folder_id, text=new_text)

        return {
            "status": "success",
            "message": f"Folder #{folder_id} updated successfully",
            "data": {
                "id": str(folder_id),
                "message_id": folder_id,
                "name": current_meta.get("name", f"Folder_{folder_id}"),
                "parentId": current_meta.get("parentId", "root"),
                "parent_id": current_meta.get("parentId", "root")
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update folder: {str(e)}")
    finally:
        await client.disconnect()

@app.delete("/api/folders/{folder_id}")
async def delete_folder(
    folder_id: int,
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...)
):
    """
    Deletes the folder message from Telegram 'Saved Messages' ('me').
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        await client.delete_messages('me', [folder_id], revoke=True)
        return {
            "status": "success",
            "message": f"Folder #{folder_id} permanently deleted from Saved Messages ('me')"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete folder failed: {str(e)}")
    finally:
        await client.disconnect()

# ─────────────────────────────────────────────────────────────
# 2. FOLDER-LINKED FILE UPLOAD & LISTING (parentId Binding)
# ─────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_file_to_saved_messages(
    file: UploadFile = File(...),
    session_string: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    folder_id: str = Form("root"),
    parent_id: Optional[str] = Form(None)
):
    """
    Uploads file directly into Telegram 'Saved Messages' ('me') with #TG_DRIVE_FILE#{"name":"...","parentId":"..."}
    """
    file_name = file.filename or "file.bin"
    file_size = file.size if hasattr(file, 'size') and file.size else 0
    target_folder = (parent_id or folder_id or "root").strip()

    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        caption_meta = {
            "name": file_name,
            "customName": file_name,
            "parentId": target_folder,
            "folderId": target_folder,
            "totalSize": file_size,
            "mimeType": file.content_type or "application/octet-stream"
        }
        caption = f"{FILE_PREFIX}{json.dumps(caption_meta)}"

        attributes = [DocumentAttributeFilename(file_name=file_name)]
        if file.content_type and file.content_type.startswith("video/"):
            attributes.append(DocumentAttributeVideo(duration=0, w=1280, h=720, supports_streaming=True))
        elif file.content_type and file.content_type.startswith("audio/"):
            attributes.append(DocumentAttributeAudio(duration=0, title=file_name))

        await file.seek(0)
        try:
            file.file.name = file_name
        except Exception:
            pass

        uploaded_file = await client.upload_file(file.file, file_name=file_name)

        msg = await client.send_file(
            'me',
            uploaded_file,
            caption=caption,
            force_document=True,
            attributes=attributes
        )

        real_size = msg.file.size if msg.file and msg.file.size else file_size

        return {
            "status": "success",
            "data": {
                "id": str(msg.id),
                "message_id": msg.id,
                "name": file_name,
                "size": real_size,
                "folder_id": target_folder,
                "parentId": target_folder,
                "mimeType": file.content_type or "application/octet-stream",
                "destination": "Saved Messages ('me')",
                "created_at": int(msg.date.timestamp() * 1000)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MTProto Upload Failed: {str(e)}")
    finally:
        await file.close()
        await client.disconnect()

# ─────────────────────────────────────────────────────────────
# 2.1 CHUNKED RESUMABLE UPLOAD SUITE (Bypasses Cloudflare 100MB Limit)
# ─────────────────────────────────────────────────────────────

CHUNK_STAGING_DIR = "/tmp/tgdrive_chunks"
os.makedirs(CHUNK_STAGING_DIR, exist_ok=True)

class ChunkStreamReader(io.RawIOBase):
    def __init__(self, chunk_paths, file_name="file.bin"):
        self.chunk_paths = chunk_paths
        self.current_idx = 0
        self.current_file = open(self.chunk_paths[0], 'rb') if self.chunk_paths else None
        self.name = file_name

    def read(self, size=-1):
        if not self.current_file:
            return b""
        data = self.current_file.read(size)
        if not data and self.current_idx + 1 < len(self.chunk_paths):
            self.current_file.close()
            self.current_idx += 1
            self.current_file = open(self.chunk_paths[self.current_idx], 'rb')
            return self.read(size)
        return data

    def readable(self):
        return True

    def seekable(self):
        return False

    def close(self):
        if self.current_file:
            try:
                self.current_file.close()
            except Exception:
                pass
            self.current_file = None

@app.post("/api/upload/init")
async def init_chunked_upload(
    file_name: str = Form(...),
    file_size: int = Form(...),
    total_chunks: int = Form(...),
    chunk_size: int = Form(26214400), # default 25MB
    folder_id: str = Form("root"),
    parent_id: Optional[str] = Form(None),
    mime_type: str = Form("application/octet-stream")
):
    """
    Initializes a chunked upload session for large files (> 100MB).
    """
    upload_id = f"up_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
    session_dir = os.path.join(CHUNK_STAGING_DIR, upload_id)
    os.makedirs(session_dir, exist_ok=True)

    meta = {
        "upload_id": upload_id,
        "file_name": file_name,
        "file_size": file_size,
        "total_chunks": total_chunks,
        "chunk_size": chunk_size,
        "folder_id": parent_id or folder_id or "root",
        "mime_type": mime_type,
        "created_at": time.time(),
        "expires_at": time.time() + 10800 # 3 hours TTL
    }

    with open(os.path.join(session_dir, "meta.json"), "w") as f:
        json.dump(meta, f)

    return {
        "status": "success",
        "upload_id": upload_id,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "expires_at": int(meta["expires_at"] * 1000)
    }

@app.post("/api/upload/chunk")
async def upload_chunk_part(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    file: UploadFile = File(...)
):
    """
    Receives a single chunk (25MB-50MB) and stores it in temporary staging directory.
    """
    session_dir = os.path.join(CHUNK_STAGING_DIR, upload_id)
    if not os.path.exists(session_dir) or not os.path.exists(os.path.join(session_dir, "meta.json")):
        raise HTTPException(status_code=404, detail="Upload session not found or expired")

    chunk_path = os.path.join(session_dir, f"chunk_{chunk_index:05d}.part")
    with open(chunk_path, "wb") as f:
        while True:
            chunk_data = await file.read(1024 * 1024)
            if not chunk_data:
                break
            f.write(chunk_data)

    await file.close()

    with open(os.path.join(session_dir, "meta.json"), "r") as f:
        meta = json.load(f)

    uploaded = [
        int(re.search(r"chunk_(\d+)\.part", fn).group(1))
        for fn in os.listdir(session_dir)
        if fn.startswith("chunk_") and fn.endswith(".part")
    ]
    uploaded.sort()

    return {
        "status": "success",
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "uploaded_chunks_count": len(uploaded),
        "total_chunks": meta["total_chunks"],
        "is_complete": len(uploaded) == meta["total_chunks"]
    }

@app.post("/api/upload/complete")
async def complete_chunked_upload(
    upload_id: str = Form(...),
    session_string: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...)
):
    """
    Assembles and streams all received chunks directly to Telegram Saved Messages.
    Cleans up temporary disk staging immediately upon completion.
    """
    session_dir = os.path.join(CHUNK_STAGING_DIR, upload_id)
    meta_path = os.path.join(session_dir, "meta.json")
    if not os.path.exists(session_dir) or not os.path.exists(meta_path):
        raise HTTPException(status_code=404, detail="Upload session not found or expired")

    with open(meta_path, "r") as f:
        meta = json.load(f)

    total_chunks = meta["total_chunks"]
    file_name = meta["file_name"]
    target_folder = meta.get("folder_id", "root")
    mime_type = meta.get("mime_type", "application/octet-stream")
    file_size = meta.get("file_size", 0)

    chunk_paths = []
    for i in range(total_chunks):
        p = os.path.join(session_dir, f"chunk_{i:05d}.part")
        if not os.path.exists(p):
            raise HTTPException(status_code=400, detail=f"Missing chunk index {i}. Upload not complete.")
        chunk_paths.append(p)

    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        caption_meta = {
            "name": file_name,
            "customName": file_name,
            "parentId": target_folder,
            "folderId": target_folder,
            "totalSize": file_size,
            "mimeType": mime_type
        }
        caption = f"{FILE_PREFIX}{json.dumps(caption_meta)}"

        attributes = [DocumentAttributeFilename(file_name=file_name)]
        if mime_type.startswith("video/"):
            attributes.append(DocumentAttributeVideo(duration=0, w=1280, h=720, supports_streaming=True))
        elif mime_type.startswith("audio/"):
            attributes.append(DocumentAttributeAudio(duration=0, title=file_name))

        reader = ChunkStreamReader(chunk_paths, file_name=file_name)
        try:
            uploaded_file = await client.upload_file(reader, file_name=file_name, file_size=file_size)
        finally:
            reader.close()

        msg = await client.send_file(
            'me',
            uploaded_file,
            caption=caption,
            force_document=True,
            attributes=attributes
        )

        real_size = msg.file.size if msg.file and msg.file.size else file_size

        # Cleanup chunk session files immediately
        try:
            shutil.rmtree(session_dir)
        except Exception:
            pass

        return {
            "status": "success",
            "message": "Chunked upload assembled and uploaded to Saved Messages successfully",
            "data": {
                "id": str(msg.id),
                "message_id": msg.id,
                "name": file_name,
                "size": real_size,
                "folder_id": target_folder,
                "parentId": target_folder,
                "mimeType": mime_type,
                "destination": "Saved Messages ('me')",
                "created_at": int(msg.date.timestamp() * 1000)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assembly & MTProto Upload Failed: {str(e)}")
    finally:
        await client.disconnect()

@app.get("/api/upload/status")
async def get_chunk_upload_status(upload_id: str = Query(...)):
    """
    Returns progress of chunked upload session for resuming.
    """
    session_dir = os.path.join(CHUNK_STAGING_DIR, upload_id)
    meta_path = os.path.join(session_dir, "meta.json")
    if not os.path.exists(session_dir) or not os.path.exists(meta_path):
        raise HTTPException(status_code=404, detail="Upload session not found or expired")

    with open(meta_path, "r") as f:
        meta = json.load(f)

    uploaded = [
        int(re.search(r"chunk_(\d+)\.part", fn).group(1))
        for fn in os.listdir(session_dir)
        if fn.startswith("chunk_") and fn.endswith(".part")
    ]
    uploaded.sort()

    missing = [i for i in range(meta["total_chunks"]) if i not in uploaded]

    return {
        "status": "success",
        "upload_id": upload_id,
        "file_name": meta["file_name"],
        "file_size": meta["file_size"],
        "total_chunks": meta["total_chunks"],
        "uploaded_chunks": uploaded,
        "missing_chunks": missing,
        "is_complete": len(missing) == 0
    }

@app.delete("/api/upload/abort")
async def abort_chunk_upload(upload_id: str = Query(...)):
    """
    Aborts a chunked upload session and cleans up disk.
    """
    session_dir = os.path.join(CHUNK_STAGING_DIR, upload_id)
    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
        except Exception:
            pass
    return {"status": "success", "message": f"Upload session {upload_id} aborted and cleaned up"}

@app.get("/api/files")
async def list_files(
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...),
    folder_id: Optional[str] = Query(None),
    parent_id: Optional[str] = Query(None),
    limit: int = Query(50),
    offset_id: int = Query(0),
    search: Optional[str] = Query(None)
):
    """
    Directly scans and streams files from Telegram 'Saved Messages' ('me')
    Supports folder filtering, search query, and cursor pagination (offset_id).
    """
    target_folder = parent_id or folder_id
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        items = []
        last_offset = offset_id

        async for msg in client.iter_messages('me', limit=limit, offset_id=offset_id):
            last_offset = msg.id
            if msg.media:
                file_name = None
                file_size = 0
                mime_type = "application/octet-stream"

                if hasattr(msg, 'file') and msg.file:
                    file_name = msg.file.name
                    file_size = msg.file.size or 0
                    mime_type = msg.file.mime_type or mime_type

                if not file_name:
                    if hasattr(msg.media, 'document'):
                        for attr in getattr(msg.media.document, 'attributes', []):
                            if hasattr(attr, 'file_name'):
                                file_name = attr.file_name
                    elif hasattr(msg.media, 'photo'):
                        file_name = f"photo_{msg.id}.jpg"
                        mime_type = "image/jpeg"

                if not file_name:
                    file_name = f"media_{msg.id}"

                item_parent = "root"
                if msg.message and msg.message.startswith(FILE_PREFIX):
                    try:
                        meta = json.loads(msg.message[len(FILE_PREFIX):])
                        item_parent = meta.get("parentId") or meta.get("folderId") or "root"
                        file_name = meta.get("customName") or meta.get("name") or file_name
                    except Exception:
                        pass

                # Filter by folder (exclude 'trash' unless folder_id == 'trash')
                if target_folder is not None:
                    if target_folder != "all" and item_parent != str(target_folder):
                        continue
                else:
                    if item_parent == "trash":
                        continue

                if search and search.lower() not in file_name.lower():
                    continue

                items.append({
                    "id": str(msg.id),
                    "message_id": msg.id,
                    "name": file_name,
                    "size": file_size,
                    "folder_id": item_parent,
                    "parentId": item_parent,
                    "mimeType": mime_type,
                    "destination": "Saved Messages ('me')",
                    "created_at": int(msg.date.timestamp() * 1000) if msg.date else 0
                })

        return {
            "status": "success",
            "total": len(items),
            "next_offset_id": last_offset if len(items) >= limit else 0,
            "has_more": len(items) >= limit,
            "storage": "Telegram 'Saved Messages' ('me')",
            "items": items
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to scan Saved Messages: {str(e)}")
    finally:
        await client.disconnect()

# ─────────────────────────────────────────────────────────────
# 3. ADVANCED FILE OPERATIONS (RENAME, MOVE, STREAMING / RANGE)
# ─────────────────────────────────────────────────────────────

@app.patch("/api/files/{message_id}/rename")
@app.post("/api/files/{message_id}/rename")
@app.patch("/api/rename/{message_id}")
async def rename_file(
    message_id: int,
    name: str = Form(...),
    folder_id: Optional[str] = Form(None),
    session_string: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...)
):
    """
    Renames a file in Telegram 'Saved Messages' ('me') by updating its MTProto metadata caption.
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        msg = await client.get_messages('me', ids=message_id)
        if not msg:
            raise HTTPException(status_code=404, detail="File message not found in Saved Messages")

        current_meta = {}
        if msg.message and msg.message.startswith(FILE_PREFIX):
            try:
                current_meta = json.loads(msg.message[len(FILE_PREFIX):])
            except Exception:
                pass

        new_name = name.strip()
        current_meta["name"] = new_name
        current_meta["customName"] = new_name
        if folder_id is not None:
            current_meta["parentId"] = folder_id.strip()
            current_meta["folderId"] = folder_id.strip()
        elif "parentId" not in current_meta:
            current_meta["parentId"] = "root"

        new_caption = f"{FILE_PREFIX}{json.dumps(current_meta)}"
        await client.edit_message('me', message_id, text=new_caption)

        file_size = msg.file.size if hasattr(msg, 'file') and msg.file else 0
        mime_type = msg.file.mime_type if hasattr(msg, 'file') and msg.file else "application/octet-stream"

        return {
            "status": "success",
            "message": f"File #{message_id} successfully renamed to '{new_name}'",
            "data": {
                "id": str(message_id),
                "message_id": message_id,
                "name": new_name,
                "size": file_size,
                "folder_id": current_meta.get("parentId", "root"),
                "parentId": current_meta.get("parentId", "root"),
                "mimeType": mime_type,
                "updated_at": int(asyncio.get_event_loop().time() * 1000)
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rename Failed: {str(e)}")
    finally:
        await client.disconnect()

@app.patch("/api/files/{message_id}/move")
@app.post("/api/files/{message_id}/move")
async def move_file(
    message_id: int,
    target_folder_id: str = Form(...),
    session_string: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...)
):
    """
    Moves a file between folders by updating parentId in Telegram metadata.
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        msg = await client.get_messages('me', ids=message_id)
        if not msg:
            raise HTTPException(status_code=404, detail="File message not found in Saved Messages")

        current_meta = {}
        if msg.message and msg.message.startswith(FILE_PREFIX):
            try:
                current_meta = json.loads(msg.message[len(FILE_PREFIX):])
            except Exception:
                pass

        target = target_folder_id.strip() or "root"
        current_meta["parentId"] = target
        current_meta["folderId"] = target

        new_caption = f"{FILE_PREFIX}{json.dumps(current_meta)}"
        await client.edit_message('me', message_id, text=new_caption)

        return {
            "status": "success",
            "message": f"File #{message_id} moved to folder '{target}'",
            "data": {
                "id": str(message_id),
                "message_id": message_id,
                "name": current_meta.get("name", f"File_{message_id}"),
                "folder_id": target,
                "parentId": target
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Move Failed: {str(e)}")
    finally:
        await client.disconnect()

@app.get("/api/download/{message_id}")
@app.get("/api/stream/{message_id}")
async def download_or_stream_file(
    message_id: int,
    request: Request,
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...)
):
    """
    Streams file directly from Telegram Saved Messages with full HTTP 206 Partial Content (Range) support.
    Enables seeking, instant playback in video/audio players without downloading the entire file.
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        msg = await client.get_messages('me', ids=message_id)
        if not msg or not msg.media:
            await client.disconnect()
            raise HTTPException(status_code=404, detail="File message not found in Saved Messages")

        file_name = msg.file.name if msg.file and msg.file.name else f"file_{message_id}"
        file_size = msg.file.size if msg.file and msg.file.size else 0
        mime_type = msg.file.mime_type if msg.file and msg.file.mime_type else "application/octet-stream"

        range_header = request.headers.get("range")

        # HTTP 206 Partial Content Byte Range Streaming
        if range_header and file_size > 0:
            range_match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1

                async def range_generator():
                    try:
                        async for chunk in client.iter_download(msg.media, offset=start, limit=length, chunk_size=256*1024):
                            yield chunk
                    finally:
                        await client.disconnect()

                headers = {
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                    "Content-Disposition": f'inline; filename="{file_name}"',
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges"
                }
                return StreamingResponse(
                    range_generator(),
                    status_code=206,
                    media_type=mime_type,
                    headers=headers
                )

        # Full Stream Download (HTTP 200)
        async def full_generator():
            try:
                async for chunk in client.iter_download(msg.media, chunk_size=512*1024):
                    yield chunk
            finally:
                await client.disconnect()

        headers = {
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes"
        }
        if file_size:
            headers["Content-Length"] = str(file_size)

        return StreamingResponse(
            full_generator(),
            media_type=mime_type,
            headers=headers
        )
    except HTTPException:
        raise
    except Exception as e:
        await client.disconnect()
        raise HTTPException(status_code=500, detail=f"MTProto Streaming/Download Failed: {str(e)}")

@app.delete("/api/delete/{message_id}")
async def delete_file_permanently(
    message_id: int,
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...)
):
    """
    Deletes file DIRECTLY and permanently from user's Telegram 'Saved Messages' ('me')
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        await client.delete_messages('me', [message_id], revoke=True)
        return {
            "status": "success",
            "message": f"Message #{message_id} permanently deleted from Saved Messages ('me')"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete Failed: {str(e)}")
    finally:
        await client.disconnect()

# ─────────────────────────────────────────────────────────────
# 4. REAL TRASH / RECYCLE BIN MANAGEMENT
# ─────────────────────────────────────────────────────────────

@app.post("/api/files/{message_id}/trash")
async def move_to_trash(
    message_id: int,
    session_string: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...)
):
    """
    Soft-delete: sets parentId: 'trash' and preserves originalParentId in Telegram caption.
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        msg = await client.get_messages('me', ids=message_id)
        if not msg:
            raise HTTPException(status_code=404, detail="Message not found in Saved Messages")

        text = (msg.message or "").strip()
        is_folder = text.startswith(FOLDER_PREFIX)
        prefix = FOLDER_PREFIX if is_folder else FILE_PREFIX

        current_meta = {}
        if text.startswith(prefix):
            try:
                current_meta = json.loads(text[len(prefix):])
            except Exception:
                pass

        old_parent = current_meta.get("parentId") or current_meta.get("folderId") or "root"
        current_meta["originalParentId"] = old_parent
        current_meta["parentId"] = "trash"
        current_meta["folderId"] = "trash"
        current_meta["trashed_at"] = int(asyncio.get_event_loop().time() * 1000)

        new_text = f"{prefix}{json.dumps(current_meta)}"
        await client.edit_message('me', message_id, text=new_text)

        return {
            "status": "success",
            "message": f"Item #{message_id} moved to Trash",
            "data": {
                "id": str(message_id),
                "message_id": message_id,
                "name": current_meta.get("name", f"Item_{message_id}"),
                "parentId": "trash",
                "originalParentId": old_parent
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Move to Trash Failed: {str(e)}")
    finally:
        await client.disconnect()

@app.get("/api/trash")
async def list_trash(
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...),
    limit: int = Query(500)
):
    """
    Scans Telegram Saved Messages for all items where parentId is 'trash'.
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        trash_items = []
        async for msg in client.iter_messages('me', limit=limit):
            text = (msg.message or "").strip()
            
            # Check folders
            if text.startswith(FOLDER_PREFIX):
                try:
                    meta = json.loads(text[len(FOLDER_PREFIX):])
                    if meta.get("parentId") == "trash":
                        trash_items.append({
                            "id": str(msg.id),
                            "message_id": msg.id,
                            "type": "folder",
                            "name": meta.get("name", f"Folder_{msg.id}"),
                            "originalParentId": meta.get("originalParentId", "root"),
                            "parentId": "trash",
                            "created_at": int(msg.date.timestamp() * 1000) if msg.date else 0
                        })
                except Exception:
                    pass

            # Check files
            elif msg.media:
                if text.startswith(FILE_PREFIX):
                    try:
                        meta = json.loads(text[len(FILE_PREFIX):])
                        if meta.get("parentId") == "trash":
                            file_name = meta.get("customName") or meta.get("name")
                            if not file_name and hasattr(msg, 'file') and msg.file:
                                file_name = msg.file.name
                            trash_items.append({
                                "id": str(msg.id),
                                "message_id": msg.id,
                                "type": "file",
                                "name": file_name or f"file_{msg.id}",
                                "size": msg.file.size if hasattr(msg, 'file') and msg.file else 0,
                                "mimeType": msg.file.mime_type if hasattr(msg, 'file') and msg.file else "application/octet-stream",
                                "originalParentId": meta.get("originalParentId", "root"),
                                "parentId": "trash",
                                "created_at": int(msg.date.timestamp() * 1000) if msg.date else 0
                            })
                    except Exception:
                        pass

        return {
            "status": "success",
            "total": len(trash_items),
            "storage": "Telegram Trash ('Saved Messages')",
            "items": trash_items
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list trash: {str(e)}")
    finally:
        await client.disconnect()

@app.post("/api/trash/{message_id}/restore")
@app.post("/api/files/{message_id}/restore")
async def restore_trash_item(
    message_id: int,
    session_string: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...)
):
    """
    Restores file or folder from Trash back to its originalParentId (or 'root').
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        msg = await client.get_messages('me', ids=message_id)
        if not msg:
            raise HTTPException(status_code=404, detail="Message not found in Saved Messages")

        text = (msg.message or "").strip()
        is_folder = text.startswith(FOLDER_PREFIX)
        prefix = FOLDER_PREFIX if is_folder else FILE_PREFIX

        current_meta = {}
        if text.startswith(prefix):
            try:
                current_meta = json.loads(text[len(prefix):])
            except Exception:
                pass

        target_parent = current_meta.get("originalParentId") or "root"
        current_meta["parentId"] = target_parent
        current_meta["folderId"] = target_parent
        current_meta.pop("originalParentId", None)
        current_meta.pop("trashed_at", None)

        new_text = f"{prefix}{json.dumps(current_meta)}"
        await client.edit_message('me', message_id, text=new_text)

        return {
            "status": "success",
            "message": f"Item #{message_id} restored to folder '{target_parent}'",
            "data": {
                "id": str(message_id),
                "message_id": message_id,
                "name": current_meta.get("name", f"Item_{message_id}"),
                "parentId": target_parent,
                "folder_id": target_parent
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restore Failed: {str(e)}")
    finally:
        await client.disconnect()

@app.delete("/api/trash")
async def empty_trash(
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...)
):
    """
    Permanently deletes all messages currently in trash (parentId == 'trash') from Telegram Saved Messages.
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        msg_ids_to_delete = []
        async for msg in client.iter_messages('me', limit=1000):
            text = (msg.message or "").strip()
            if text.startswith(FOLDER_PREFIX) or text.startswith(FILE_PREFIX):
                try:
                    prefix = FOLDER_PREFIX if text.startswith(FOLDER_PREFIX) else FILE_PREFIX
                    meta = json.loads(text[len(prefix):])
                    if meta.get("parentId") == "trash":
                        msg_ids_to_delete.append(msg.id)
                except Exception:
                    pass

        if msg_ids_to_delete:
            await client.delete_messages('me', msg_ids_to_delete, revoke=True)

        return {
            "status": "success",
            "deleted_count": len(msg_ids_to_delete),
            "message": f"Trash emptied: {len(msg_ids_to_delete)} items permanently deleted"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Empty Trash Failed: {str(e)}")
    finally:
        await client.disconnect()

# ─────────────────────────────────────────────────────────────
# 5. SERVER-SIDE SEARCH & ACCURATE STORAGE ANALYTICS
# ─────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search_items(
    q: str = Query(...),
    type: str = Query("all"),
    mime_type: Optional[str] = Query(None),
    limit: int = Query(200),
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...)
):
    """
    Fast Server-Side Search across Telegram Saved Messages by filename, type, and MIME.
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        results = []
        search_lower = q.strip().lower()

        async for msg in client.iter_messages('me', limit=limit):
            text = (msg.message or "").strip()
            
            # Check folder
            if text.startswith(FOLDER_PREFIX) and type in ("all", "folder"):
                try:
                    meta = json.loads(text[len(FOLDER_PREFIX):])
                    f_name = meta.get("name", f"Folder_{msg.id}")
                    if meta.get("parentId") != "trash" and search_lower in f_name.lower():
                        results.append({
                            "id": str(msg.id),
                            "message_id": msg.id,
                            "type": "folder",
                            "name": f_name,
                            "parentId": meta.get("parentId", "root"),
                            "created_at": int(msg.date.timestamp() * 1000) if msg.date else 0
                        })
                except Exception:
                    pass

            # Check file
            elif msg.media and type in ("all", "file", "photo", "video", "audio", "document"):
                file_name = None
                file_size = 0
                item_mime = "application/octet-stream"

                if hasattr(msg, 'file') and msg.file:
                    file_name = msg.file.name
                    file_size = msg.file.size or 0
                    item_mime = msg.file.mime_type or item_mime

                if not file_name and hasattr(msg.media, 'photo'):
                    file_name = f"photo_{msg.id}.jpg"
                    item_mime = "image/jpeg"

                file_name = file_name or f"media_{msg.id}"
                item_parent = "root"

                if text.startswith(FILE_PREFIX):
                    try:
                        meta = json.loads(text[len(FILE_PREFIX):])
                        file_name = meta.get("customName") or meta.get("name") or file_name
                        item_parent = meta.get("parentId") or meta.get("folderId") or "root"
                    except Exception:
                        pass

                if item_parent == "trash":
                    continue

                if search_lower not in file_name.lower():
                    continue

                if mime_type and mime_type.lower() not in item_mime.lower():
                    continue

                results.append({
                    "id": str(msg.id),
                    "message_id": msg.id,
                    "type": "file",
                    "name": file_name,
                    "size": file_size,
                    "mimeType": item_mime,
                    "parentId": item_parent,
                    "created_at": int(msg.date.timestamp() * 1000) if msg.date else 0
                })

        return {
            "status": "success",
            "query": q,
            "total_matches": len(results),
            "items": results
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search Failed: {str(e)}")
    finally:
        await client.disconnect()

@app.get("/api/stats")
async def get_storage_stats(
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...),
    scan_limit: int = Query(1000)
):
    """
    Calculates live accurate storage metrics and category breakdown directly from Telegram MTProto.
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        total_files = 0
        total_folders = 0
        total_bytes = 0

        categories = {
            "videos": {"count": 0, "bytes": 0},
            "images": {"count": 0, "bytes": 0},
            "audio": {"count": 0, "bytes": 0},
            "documents": {"count": 0, "bytes": 0},
            "apks": {"count": 0, "bytes": 0},
            "archives": {"count": 0, "bytes": 0},
            "others": {"count": 0, "bytes": 0}
        }

        async for msg in client.iter_messages('me', limit=scan_limit):
            text = (msg.message or "").strip()

            if text.startswith(FOLDER_PREFIX):
                try:
                    meta = json.loads(text[len(FOLDER_PREFIX):])
                    if meta.get("parentId") != "trash":
                        total_folders += 1
                except Exception:
                    pass

            elif msg.media:
                file_name = ""
                file_size = 0
                mime = "application/octet-stream"

                if hasattr(msg, 'file') and msg.file:
                    file_name = msg.file.name or ""
                    file_size = msg.file.size or 0
                    mime = msg.file.mime_type or mime

                if text.startswith(FILE_PREFIX):
                    try:
                        meta = json.loads(text[len(FILE_PREFIX):])
                        if meta.get("parentId") == "trash":
                            continue
                        file_name = meta.get("customName") or meta.get("name") or file_name
                    except Exception:
                        pass

                total_files += 1
                total_bytes += file_size

                ext = file_name.split(".")[-1].lower() if "." in file_name else ""
                if mime.startswith("video/") or ext in ("mp4", "mkv", "avi", "mov", "webm", "flv", "3gp", "ts"):
                    categories["videos"]["count"] += 1
                    categories["videos"]["bytes"] += file_size
                elif mime.startswith("image/") or ext in ("jpg", "jpeg", "png", "webp", "gif", "svg", "bmp"):
                    categories["images"]["count"] += 1
                    categories["images"]["bytes"] += file_size
                elif mime.startswith("audio/") or ext in ("mp3", "wav", "m4a", "ogg", "flac", "aac", "opus"):
                    categories["audio"]["count"] += 1
                    categories["audio"]["bytes"] += file_size
                elif ext == "apk":
                    categories["apks"]["count"] += 1
                    categories["apks"]["bytes"] += file_size
                elif ext in ("zip", "rar", "7z", "tar", "gz", "bz2", "xz"):
                    categories["archives"]["count"] += 1
                    categories["archives"]["bytes"] += file_size
                elif ext in ("pdf", "docx", "doc", "txt", "xlsx", "xls", "pptx", "csv", "json", "xml", "html"):
                    categories["documents"]["count"] += 1
                    categories["documents"]["bytes"] += file_size
                else:
                    categories["others"]["count"] += 1
                    categories["others"]["bytes"] += file_size

        return {
            "status": "success",
            "total_files": total_files,
            "total_folders": total_folders,
            "total_bytes": total_bytes,
            "total_storage_mb": round(total_bytes / (1024 * 1024), 2),
            "total_storage_gb": round(total_bytes / (1024 * 1024 * 1024), 3),
            "categories": categories,
            "storage_engine": "Telegram 'Saved Messages' ('me')"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stats calculation failed: {str(e)}")
    finally:
        await client.disconnect()

# ─────────────────────────────────────────────────────────────
# 6. THUMBNAILS & CLONING
# ─────────────────────────────────────────────────────────────

@app.get("/api/thumbnail/{message_id}")
async def get_media_thumbnail(
    message_id: int,
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...)
):
    """
    Downloads and streams the 10KB fast thumbnail preview for photos/videos from Saved Messages
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        msg = await client.get_messages('me', ids=message_id)
        if not msg or not msg.media:
            await client.disconnect()
            raise HTTPException(status_code=404, detail="Media not found")

        thumb_bytes = await client.download_media(msg, thumb=-1, file=bytes)
        await client.disconnect()

        if not thumb_bytes:
            raise HTTPException(status_code=404, detail="Thumbnail not available for this media")

        return Response(
            content=thumb_bytes,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"}
        )
    except HTTPException:
        raise
    except Exception as e:
        await client.disconnect()
        raise HTTPException(status_code=500, detail=f"Thumbnail extraction failed: {str(e)}")

@app.post("/api/copy/{message_id}")
async def copy_saved_message_file(
    message_id: int,
    session_string: str = Query(...),
    api_id: int = Query(...),
    api_hash: str = Query(...)
):
    """
    Instantly copies/forwards message in Saved Messages ('me') without re-uploading bytes
    """
    client = await get_tg_client(session_string, api_id, api_hash)
    try:
        new_msg = await client.forward_messages('me', [message_id])
        if isinstance(new_msg, list):
            new_msg = new_msg[0]
        await client.disconnect()
        return {
            "status": "success",
            "message": "File cloned successfully in Saved Messages",
            "new_message_id": new_msg.id
        }
    except Exception as e:
        await client.disconnect()
        raise HTTPException(status_code=500, detail=f"File copy failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
