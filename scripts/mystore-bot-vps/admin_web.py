#!/usr/bin/env python3
"""
MyStore Dedicated Admin Web Command Center & Cloudflare Tunnel Engine
Embedded inside the bot folder. Automatically starts when bot deploys.
Protected by Master Admin Password configured in .env.
"""

import os
import sys
import json
import time
import hmac
import hashlib
import asyncio
import re
import threading
import subprocess
import urllib.request
import platform
import requests
from aiohttp import web
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

load_dotenv(os.path.join(BASE_DIR, ".env"))

from firebase_manager import firebase_mgr
from github_manager import github_mgr
from imgbb_manager import imgbb_mgr
from telegram_storage import storage_mgr

ADMIN_WEB_PASSWORD = os.getenv("ADMIN_WEB_PASSWORD", "x9k-core7373idash").strip()
ADMIN_SECURITY_TOKEN = os.getenv("ADMIN_SECURITY_TOKEN", "sec_mystore_9a8b7c6d5e4f3a2b1c0d").strip()
AUTH_SECRET = ADMIN_SECURITY_TOKEN.encode("utf-8")
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days validity

# Directory containing compiled production static files (no Node.js needed on VPS)
WEB_DIST_DIR = os.path.join(BASE_DIR, "web_dist")
if not os.path.exists(WEB_DIST_DIR):
    alt_dist = os.path.join(BASE_DIR, "web_admin", "dist")
    if os.path.exists(alt_dist):
        WEB_DIST_DIR = alt_dist

BIN_DIR = os.path.join(BASE_DIR, "bin")
os.makedirs(BIN_DIR, exist_ok=True)
CLOUDFLARED_BIN = os.path.join(BIN_DIR, "cloudflared")
TUNNEL_URL_FILE = os.path.join(BASE_DIR, "tunnel_url.txt")

# Global state
CURRENT_TUNNEL_URL = None
_web_thread = None
_tunnel_thread = None
_server_runner = None

# Active upload progress tracking: upload_id -> { "phase", "percent", "loaded", "total", "speed", "eta", "status", "updated_at" }
UPLOAD_PROGRESS = {}


# -----------------------------------------------------------------------------
# Security & Token Helpers
# -----------------------------------------------------------------------------
def generate_auth_token():
    ts = str(int(time.time()))
    sig = hmac.new(AUTH_SECRET, ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_auth_token(token_str):
    if not token_str:
        return False
    if token_str == ADMIN_SECURITY_TOKEN or token_str == ADMIN_WEB_PASSWORD:
        return True
    try:
        parts = token_str.split(".", 1)
        if len(parts) != 2:
            return False
        ts_str, sig = parts
        ts = int(ts_str)
        if time.time() - ts > SESSION_MAX_AGE:
            return False
        expected_sig = hmac.new(AUTH_SECRET, ts_str.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected_sig)
    except Exception:
        return False


def get_bearer_token(request):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return request.headers.get("X-Admin-Token", "").strip()


@web.middleware
async def auth_middleware(request, handler):
    if request.method == "OPTIONS":
        return await handler(request)

    path = request.path

    # Public endpoints
    if (
        path == "/api/auth/login"
        or path == "/api/auth/check"
        or path.startswith("/api/upload/progress")
        or not path.startswith("/api")
    ):
        return await handler(request)

    # Protected API endpoints
    token = get_bearer_token(request)
    if not verify_auth_token(token):
        return web.json_response(
            {"error": "Unauthorized: Invalid or expired admin password token."},
            status=401,
            headers={"Access-Control-Allow-Origin": "*"}
        )

    return await handler(request)


# -----------------------------------------------------------------------------
# API Route Handlers
# -----------------------------------------------------------------------------
async def handle_login(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    password = (data.get("password") or "").strip()

    if password == ADMIN_WEB_PASSWORD or password == ADMIN_SECURITY_TOKEN:
        token = generate_auth_token()
        return web.json_response({
            "success": True,
            "token": token,
            "message": "Admin Command Center Unlocked!"
        })

    return web.json_response(
        {"success": False, "error": "Incorrect admin password. Access denied."},
        status=401
    )


async def handle_auth_check(request):
    token = get_bearer_token(request)
    valid = verify_auth_token(token)
    return web.json_response({"authenticated": valid})


async def handle_get_apps(request):
    apps = await asyncio.to_thread(firebase_mgr.get_all_apps)
    return web.json_response({"apps": apps})


async def handle_create_app(request):
    try:
        data = await request.json()
        name = (data.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "Project name is required"}, status=400)

        data["updatedDate"] = data.get("updatedDate") or time.strftime("%Y-%m-%d")

        # Mirror icon and logo fields for compatibility
        if data.get("icon") and not data.get("logo"):
            data["logo"] = data["icon"]
        elif data.get("logo") and not data.get("icon"):
            data["icon"] = data["logo"]

        res = await asyncio.to_thread(firebase_mgr.add_or_update_app, data)
        if isinstance(res, tuple):
            ok, app_id = res
            data["id"] = app_id
        else:
            ok = res
        if ok:
            return web.json_response({"success": True, "app": data})
        
        err_detail = getattr(firebase_mgr, "last_sync_error", None) or "Database write failed"
        return web.json_response({"error": f"Failed to save app in database: {err_detail}"}, status=500)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return web.json_response({"error": f"Create app error: {str(e)}"}, status=500)


async def handle_update_app(request):
    try:
        app_id = request.match_info["app_id"]
        data = await request.json()
        data["id"] = app_id
        data["updatedDate"] = time.strftime("%Y-%m-%d")

        # Mirror icon and logo fields for compatibility
        if data.get("icon") and not data.get("logo"):
            data["logo"] = data["icon"]
        elif data.get("logo") and not data.get("icon"):
            data["icon"] = data["logo"]

        res = await asyncio.to_thread(firebase_mgr.add_or_update_app, data)
        if isinstance(res, tuple):
            ok, target_id = res
        else:
            ok = res
            target_id = app_id

        if ok:
            return web.json_response({"success": True, "app": data, "id": target_id})
        
        err_detail = getattr(firebase_mgr, "last_sync_error", None) or "Database write failed"
        return web.json_response({"error": f"Failed to update app in database: {err_detail}"}, status=500)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return web.json_response({"error": f"Update app error: {str(e)}"}, status=500)


async def handle_delete_app(request):
    app_id = request.match_info["app_id"]
    ok = await asyncio.to_thread(firebase_mgr.delete_app, app_id)
    if ok:
        return web.json_response({"success": True, "deleted_id": app_id})
    return web.json_response({"error": "Failed to delete app from database"}, status=500)


async def handle_get_pinned(request):
    pinned = await asyncio.to_thread(firebase_mgr.get_pinned_apps)
    return web.json_response({"pinned": pinned})


async def handle_move_pinned(request):
    data = await request.json()
    app_id = data.get("id")
    direction = data.get("direction", "up")
    ok = await asyncio.to_thread(firebase_mgr.move_pinned_app, app_id, direction)
    pinned = await asyncio.to_thread(firebase_mgr.get_pinned_apps)
    return web.json_response({"success": ok, "pinned": pinned})


async def handle_toggle_pin(request):
    data = await request.json()
    app_id = data.get("id")
    pin = data.get("pinned", True)
    if pin:
        ok = await asyncio.to_thread(firebase_mgr.pin_app_to_top, app_id)
    else:
        ok = await asyncio.to_thread(firebase_mgr.unpin_app, app_id)
    pinned = await asyncio.to_thread(firebase_mgr.get_pinned_apps)
    return web.json_response({"success": ok, "pinned": pinned})


async def handle_get_categories(request):
    cats = await asyncio.to_thread(firebase_mgr.get_categories, force_refresh=True)
    return web.json_response({"categories": cats})


async def handle_add_category(request):
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "Category name required"}, status=400)
    ok = await asyncio.to_thread(firebase_mgr.add_category, name)
    cats = await asyncio.to_thread(firebase_mgr.get_categories, force_refresh=True)
    return web.json_response({"success": ok, "categories": cats})


async def handle_delete_category(request):
    name = request.match_info["name"]
    ok = await asyncio.to_thread(firebase_mgr.delete_category, name)
    cats = await asyncio.to_thread(firebase_mgr.get_categories, force_refresh=True)
    return web.json_response({"success": ok, "categories": cats})


async def handle_rename_category(request):
    data = await request.json()
    old_name = data.get("old_name")
    new_name = data.get("new_name")
    ok = await asyncio.to_thread(firebase_mgr.rename_category, old_name, new_name)
    cats = await asyncio.to_thread(firebase_mgr.get_categories, force_refresh=True)
    return web.json_response({"success": ok, "categories": cats})


async def handle_get_releases(request):
    ok, rels = await asyncio.to_thread(github_mgr.list_all_releases)
    if ok and isinstance(rels, list):
        return web.json_response({
            "releases": rels,
            "owner": github_mgr.owner,
            "repo": github_mgr.repo
        })
    return web.json_response({"error": str(rels)}, status=500)


async def handle_delete_release(request):
    rel_id = request.match_info["rel_id"]
    tag = request.query.get("tag")
    ok, msg = await asyncio.to_thread(github_mgr.delete_release, rel_id, tag)
    return web.json_response({"success": ok, "message": msg})


async def handle_create_release(request):
    data = await request.json()
    tag = (data.get("tag") or "").strip()
    name = (data.get("name") or tag).strip()
    body = (data.get("body") or f"Release {tag} published via MyStore Admin Web Center").strip()
    if not tag:
        return web.json_response({"error": "Release tag is required"}, status=400)

    ok, rel = await asyncio.to_thread(github_mgr.get_or_create_release, tag, name, body)
    if ok and isinstance(rel, dict):
        return web.json_response({"success": True, "release": rel})
    return web.json_response({"error": str(rel)}, status=500)


async def handle_upload_release_asset(request):
    """
    Handle binary file uploads (APK, ZIP, EXE, etc.) to GitHub Releases.
    Creates or retrieves the GitHub release, uploads the asset with progress & deduplication,
    and returns direct download links.
    """
    reader = await request.multipart()
    file_bytes = None
    filename = None
    tag = None
    title = None
    notes = None
    release_id = None
    upload_id = request.headers.get("X-Upload-ID") or ""

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            filename = part.filename or "app-release.apk"
            file_bytes = await part.read()
        elif part.name == "tag":
            tag = (await part.text()).strip()
        elif part.name == "name":
            title = (await part.text()).strip()
        elif part.name in ("notes", "changelog", "body"):
            notes = (await part.text()).strip()
        elif part.name == "release_id":
            release_id = (await part.text()).strip()
        elif part.name == "upload_id":
            upload_id = (await part.text()).strip()

    if not file_bytes:
        return web.json_response({"error": "No file was selected or uploaded."}, status=400)
    if not tag and not release_id:
        return web.json_response({"error": "Release tag or release_id is required."}, status=400)

    if not upload_id:
        upload_id = f"up_{int(time.time()*1000)}"

    total_file_size = len(file_bytes)
    UPLOAD_PROGRESS[upload_id] = {
        "phase": "server_received",
        "percent": 0,
        "loaded": 0,
        "total": total_file_size,
        "speed": 0,
        "eta": 0,
        "status": "Server received file, preparing GitHub Release...",
        "updated_at": time.time()
    }

    def _process_github_upload():
        import tempfile
        # 1. Determine release target
        if release_id:
            res = requests.get(
                f"{github_mgr.api_base}/repos/{github_mgr.owner}/{github_mgr.repo}/releases/{release_id}",
                headers=github_mgr.headers,
                timeout=12
            )
            if res.status_code == 200:
                rel = res.json()
            else:
                ok, rel = github_mgr.get_or_create_release(tag or f"v{int(time.time())}", title, notes)
                if not ok:
                    return False, f"Target release error: {rel}"
        else:
            ok, rel = github_mgr.get_or_create_release(tag, title or tag, notes or f"Release {tag}")
            if not ok:
                return False, f"Failed to get/create release: {rel}"

        # 2. Write temp file and upload asset with live progress callback
        clean_filename = os.path.basename(filename)
        temp_dir = tempfile.mkdtemp(prefix="mystore_bin_")
        temp_file = os.path.join(temp_dir, clean_filename)
        try:
            with open(temp_file, "wb") as f:
                f.write(file_bytes)

            # 2a. MTProto Pure Telegram Channel Archive (using API_ID & API_HASH)
            telegram_channel_link = ""
            telegram_message_id = None
            if storage_mgr.is_configured():
                start_tg = time.time()
                last_tg_t = start_tg
                last_tg_b = 0

                def _on_tg_progress(sent_bytes, total_bytes):
                    nonlocal last_tg_t, last_tg_b
                    now = time.time()
                    dt = now - last_tg_t
                    speed = 0
                    if dt >= 0.15 or sent_bytes >= total_bytes:
                        speed = (sent_bytes - last_tg_b) / max(dt, 0.001)
                        last_tg_t = now
                        last_tg_b = sent_bytes

                    pct = min(100, int((sent_bytes / max(total_bytes, 1)) * 100))
                    eta = int((total_bytes - sent_bytes) / max(speed, 1)) if speed > 0 else 0

                    UPLOAD_PROGRESS[upload_id] = {
                        "phase": "telegram_uploading",
                        "percent": pct,
                        "loaded": sent_bytes,
                        "total": total_bytes,
                        "speed": round(speed, 1),
                        "eta": eta,
                        "status": f"Archiving to Telegram Storage Channel (MTProto API): {pct}%",
                        "updated_at": time.time()
                    }

                try:
                    tg_ok, tg_res = storage_mgr.sync_upload_to_channel(
                        file_path=temp_file,
                        caption=(
                            f"📦 <b>MyStore Cloud Storage Asset</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📄 <b>File:</b> <code>{clean_filename}</code>\n"
                            f"💾 <b>Size:</b> <code>{round(total_file_size / (1024*1024), 2)} MB</code>\n"
                            f"🏷️ <b>Tag:</b> <code>{tag}</code>\n"
                            f"⚡ <b>Engine:</b> Web Panel (Pure MTProto API_ID: {storage_mgr.api_id})"
                        ),
                        progress_callback=_on_tg_progress
                    )
                    if tg_ok and isinstance(tg_res, dict):
                        telegram_channel_link = tg_res.get("channel_link", "")
                        telegram_message_id = tg_res.get("message_id")
                except Exception as tg_err:
                    print(f"[Admin Web MTProto Upload] Notice: {tg_err}")

            # 2b. GitHub Releases CDN Upload
            start_gh = time.time()
            last_t = start_gh
            last_b = 0

            def _on_gh_progress(sent_bytes, total_bytes):
                nonlocal last_t, last_b
                now = time.time()
                dt = now - last_t
                speed = 0
                if dt >= 0.15 or sent_bytes >= total_bytes:
                    speed = (sent_bytes - last_b) / max(dt, 0.001)
                    last_t = now
                    last_b = sent_bytes

                pct = min(100, int((sent_bytes / max(total_bytes, 1)) * 100))
                eta = int((total_bytes - sent_bytes) / max(speed, 1)) if speed > 0 else 0

                UPLOAD_PROGRESS[upload_id] = {
                    "phase": "github_uploading",
                    "percent": pct,
                    "loaded": sent_bytes,
                    "total": total_bytes,
                    "speed": round(speed, 1),
                    "eta": eta,
                    "status": f"Publishing to GitHub Releases CDN: {pct}%",
                    "updated_at": time.time()
                }

            ok, asset_res = github_mgr.upload_asset(rel, temp_file, clean_filename, progress_callback=_on_gh_progress)
            if ok:
                UPLOAD_PROGRESS[upload_id] = {
                    "phase": "completed",
                    "percent": 100,
                    "loaded": total_file_size,
                    "total": total_file_size,
                    "speed": 0,
                    "eta": 0,
                    "status": "Binary asset published to GitHub Release & Telegram Storage!",
                    "updated_at": time.time()
                }

                if isinstance(asset_res, dict):
                    dl_url = asset_res.get("browser_download_url") or ""
                    asset_obj = asset_res
                else:
                    dl_url = str(asset_res)
                    asset_obj = {"browser_download_url": dl_url, "name": clean_filename}

                return True, {
                    "release": rel,
                    "asset": asset_obj,
                    "download_url": dl_url,
                    "telegram_channel_link": telegram_channel_link,
                    "telegram_message_id": telegram_message_id,
                    "filename": clean_filename,
                    "size_mb": round(len(file_bytes) / (1024 * 1024), 2)
                }

            UPLOAD_PROGRESS[upload_id] = {
                "phase": "error",
                "percent": 0,
                "status": f"GitHub upload error: {asset_res}",
                "updated_at": time.time()
            }
            return False, f"GitHub asset upload failed: {asset_res}"
        finally:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                if os.path.exists(temp_dir):
                    os.rmdir(temp_dir)
            except Exception:
                pass

    ok, result = await asyncio.to_thread(_process_github_upload)
    if ok:
        return web.json_response({"success": True, **result})
    return web.json_response({"error": str(result)}, status=500)


async def handle_get_upload_progress(request):
    upload_id = request.match_info.get("upload_id") or request.query.get("id") or ""
    # Clean up entries older than 10 minutes
    now = time.time()
    for k in list(UPLOAD_PROGRESS.keys()):
        if now - UPLOAD_PROGRESS[k].get("updated_at", now) > 600:
            UPLOAD_PROGRESS.pop(k, None)

    info = UPLOAD_PROGRESS.get(upload_id)
    if not info:
        return web.json_response({
            "phase": "idle",
            "percent": 0,
            "loaded": 0,
            "total": 0,
            "speed": 0,
            "eta": 0,
            "status": "Waiting for server..."
        })
    return web.json_response({"success": True, **info})


async def handle_upload_screenshot(request):
    reader = await request.multipart()
    field = await reader.next()
    if not field:
        return web.json_response({"error": "No image file provided"}, status=400)

    filename = field.filename or "screenshot.png"
    image_bytes = await field.read()
    if not image_bytes:
        return web.json_response({"error": "Empty file"}, status=400)

    # 1. Try ImgBB
    ok, img_url = await asyncio.to_thread(imgbb_mgr.upload_image_bytes, image_bytes, filename)
    if ok and img_url:
        return web.json_response({"success": True, "url": img_url})

    # 2. Reliable Fallback: Upload to GitHub Assets release
    def _upload_to_github():
        import tempfile
        clean_name = f"img_{int(time.time())}_{os.path.basename(filename)}"
        ok_rel, rel = github_mgr.get_or_create_release("assets", "Store Media Assets", "Static media assets uploaded via Admin Center")
        if not ok_rel:
            return False, f"GitHub assets release error: {rel}"
        with tempfile.NamedTemporaryFile(suffix=f"_{filename}", delete=False) as tf:
            tf.write(image_bytes)
            tf_path = tf.name
        try:
            ok_up, asset_res = github_mgr.upload_asset(rel, tf_path, clean_name)
            if ok_up:
                dl_url = asset_res.get("browser_download_url") if isinstance(asset_res, dict) else str(asset_res)
                return True, dl_url
            return False, str(asset_res)
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    ok_gh, gh_res = await asyncio.to_thread(_upload_to_github)
    if ok_gh and gh_res:
        return web.json_response({"success": True, "url": gh_res})

    return web.json_response({"error": f"Image upload failed: {img_url} | GitHub fallback: {gh_res}"}, status=500)


async def handle_get_developer(request):
    profile = await asyncio.to_thread(firebase_mgr.get_developer_profile)
    return web.json_response({"developer": profile})


async def handle_save_developer(request):
    data = await request.json()
    ok = await asyncio.to_thread(firebase_mgr.update_developer_profile, data)
    return web.json_response({"success": ok, "developer": data})


async def handle_get_analytics(request):
    apps = await asyncio.to_thread(firebase_mgr.get_all_apps)
    cats = await asyncio.to_thread(firebase_mgr.get_categories, force_refresh=True)
    pinned = await asyncio.to_thread(firebase_mgr.get_pinned_apps)
    stats = await asyncio.to_thread(firebase_mgr.get_analytics_stats)

    total_downloads = sum(int(v) for v in stats.values() if isinstance(v, (int, float))) if stats else 0

    breakdown = []
    for a in apps:
        app_id = str(a.get("id", "")).strip()
        count = int(stats.get(app_id, 0)) if stats else 0
        pct = round((count / max(total_downloads, 1)) * 100, 1) if total_downloads > 0 else 0
        breakdown.append({
            "id": app_id,
            "name": a.get("name", "Unnamed App"),
            "category": a.get("category", "Apps"),
            "icon": a.get("icon") or a.get("logo") or "",
            "version": a.get("version", "v1.0"),
            "size": a.get("size", "N/A"),
            "downloads": count,
            "percentage": pct,
            "downloadUrl": a.get("downloadUrl", "")
        })

    breakdown.sort(key=lambda x: x["downloads"], reverse=True)
    top_app = breakdown[0] if (breakdown and breakdown[0]["downloads"] > 0) else (breakdown[0] if breakdown else None)

    return web.json_response({
        "success": True,
        "total_apps": len(apps),
        "total_categories": len(cats),
        "total_pinned": len(pinned),
        "total_downloads": total_downloads,
        "top_app": top_app,
        "apps_breakdown": breakdown,
        "raw_stats": stats,
        "sync_time": time.strftime("%H:%M:%S UTC")
    })


async def handle_get_health(request):
    # 1. Firebase Health
    start = time.time()
    fb_ok = False
    try:
        r = await asyncio.to_thread(firebase_mgr.session.get, firebase_mgr._get_url("categories"), timeout=5)
        fb_ok = (r.status_code == 200)
    except Exception:
        pass
    fb_lat = round((time.time() - start) * 1000, 1)

    # 2. GitHub API Health
    start_gh = time.time()
    gh_ok, gh_msg = await asyncio.to_thread(github_mgr.test_connection)
    gh_lat = round((time.time() - start_gh) * 1000, 1)
    repo_name = f"{github_mgr.owner}/{github_mgr.repo}"

    return web.json_response({
        "status": "healthy" if (fb_ok and gh_ok) else "warning",
        "firebase": {"online": fb_ok, "latency_ms": fb_lat, "url": firebase_mgr.db_url},
        "github": {"online": gh_ok, "latency_ms": gh_lat, "repo": repo_name, "message": gh_msg},
        "tunnel_url": CURRENT_TUNNEL_URL or "Connecting...",
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    })


async def handle_get_backup(request):
    try:
        res = await asyncio.to_thread(firebase_mgr.session.get, f"{firebase_mgr.db_url}/.json", timeout=15)
        if res.status_code == 200:
            return web.json_response(res.json(), headers={
                "Content-Disposition": f'attachment; filename="mystore-backup-{int(time.time())}.json"'
            })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"error": "Failed to fetch database dump"}, status=500)


async def handle_post_restore(request):
    data = await request.json()
    if not isinstance(data, dict):
        return web.json_response({"error": "Invalid backup JSON format"}, status=400)
    try:
        res = await asyncio.to_thread(firebase_mgr.session.put, f"{firebase_mgr.db_url}/.json", json=data, timeout=30)
        if res.status_code == 200:
            firebase_mgr._invalidate_cache()
            return web.json_response({"success": True, "message": "Database successfully restored!"})
        return web.json_response({"error": res.text}, status=res.status_code)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_spa_fallback(request):
    index_file = os.path.join(WEB_DIST_DIR, "index.html")
    if os.path.exists(index_file):
        return web.FileResponse(index_file)
    return web.Response(text="Admin Web Panel is initializing or web_dist not found.", content_type="text/html")


def setup_routes(app):
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_get("/api/auth/check", handle_auth_check)

    app.router.add_get("/api/apps", handle_get_apps)
    app.router.add_post("/api/apps", handle_create_app)
    app.router.add_put("/api/apps/{app_id}", handle_update_app)
    app.router.add_delete("/api/apps/{app_id}", handle_delete_app)

    app.router.add_get("/api/pinned", handle_get_pinned)
    app.router.add_post("/api/pinned/move", handle_move_pinned)
    app.router.add_post("/api/pinned/toggle", handle_toggle_pin)

    app.router.add_get("/api/categories", handle_get_categories)
    app.router.add_post("/api/categories", handle_add_category)
    app.router.add_delete("/api/categories/{name}", handle_delete_category)
    app.router.add_put("/api/categories/rename", handle_rename_category)

    app.router.add_get("/api/releases", handle_get_releases)
    app.router.add_post("/api/releases", handle_create_release)
    app.router.add_post("/api/releases/upload", handle_upload_release_asset)
    app.router.add_delete("/api/releases/{rel_id}", handle_delete_release)

    app.router.add_get("/api/upload/progress", handle_get_upload_progress)
    app.router.add_get("/api/upload/progress/{upload_id}", handle_get_upload_progress)

    app.router.add_post("/api/screenshots/upload", handle_upload_screenshot)
    app.router.add_post("/api/logo/upload", handle_upload_screenshot)
    app.router.add_post("/api/upload/image", handle_upload_screenshot)

    app.router.add_get("/api/developer", handle_get_developer)
    app.router.add_post("/api/developer", handle_save_developer)

    app.router.add_get("/api/analytics", handle_get_analytics)
    app.router.add_get("/api/health", handle_get_health)

    app.router.add_get("/api/backup", handle_get_backup)
    app.router.add_post("/api/restore", handle_post_restore)

    assets_dir = os.path.join(WEB_DIST_DIR, "assets")
    if os.path.exists(assets_dir):
        app.router.add_static("/assets", assets_dir)
    app.router.add_get("/{tail:.*}", handle_spa_fallback)


def create_app():
    # Allow uploads up to 1GB (for large APKs, ZIP binaries, etc.)
    app = web.Application(middlewares=[auth_middleware], client_max_size=1024 * 1024 * 1024)
    setup_routes(app)
    return app


# -----------------------------------------------------------------------------
# Cloudflare Tunnel Auto-Manager
# -----------------------------------------------------------------------------
def ensure_cloudflared_binary():
    """Ensure cloudflared binary is available or auto-download on any Linux VPS"""
    # 1. Check local bot/bin/cloudflared
    if os.path.exists(CLOUDFLARED_BIN) and os.access(CLOUDFLARED_BIN, os.X_OK):
        return CLOUDFLARED_BIN

    # 2. Check system PATH
    import shutil
    sys_bin = shutil.which("cloudflared")
    if sys_bin:
        return sys_bin

    # Check ~/.local/bin
    local_bin = os.path.expanduser("~/.local/bin/cloudflared")
    if os.path.exists(local_bin) and os.access(local_bin, os.X_OK):
        return local_bin

    # 3. Auto-download according to machine architecture
    arch = platform.machine().lower()
    if "arm" in arch or "aarch64" in arch:
        dl_arch = "cloudflared-linux-arm64"
    else:
        dl_arch = "cloudflared-linux-amd64"

    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{dl_arch}"
    print(f"📥 [Cloudflare Tunnel] Downloading {dl_arch} binary for {arch}...", flush=True)
    try:
        urllib.request.urlretrieve(url, CLOUDFLARED_BIN)
        os.chmod(CLOUDFLARED_BIN, 0o755)
        print(f"✅ [Cloudflare Tunnel] Binary installed at {CLOUDFLARED_BIN}", flush=True)
        return CLOUDFLARED_BIN
    except Exception as e:
        print(f"⚠️ [Cloudflare Tunnel] Auto-download failed: {e}", flush=True)
        return None


def get_tunnel_url():
    """Return the current active Cloudflare Tunnel URL"""
    global CURRENT_TUNNEL_URL
    if CURRENT_TUNNEL_URL:
        return CURRENT_TUNNEL_URL
    if os.path.exists(TUNNEL_URL_FILE):
        try:
            with open(TUNNEL_URL_FILE, "r", encoding="utf-8") as f:
                url = f.read().strip()
                if url.startswith("https://") and "trycloudflare.com" in url:
                    CURRENT_TUNNEL_URL = url
                    return url
        except Exception:
            pass
    return None


def run_cloudflare_tunnel(port=5000, on_tunnel_ready=None):
    """Background worker to spawn cloudflared tunnel and extract live URL"""
    global CURRENT_TUNNEL_URL

    bin_path = ensure_cloudflared_binary()
    if not bin_path:
        print("⚠️ [Cloudflare Tunnel] cloudflared binary not found; tunnel could not be started.", flush=True)
        return

    cmd = [bin_path, "tunnel", "--url", f"http://localhost:{port}"]
    print(f"🌐 [Cloudflare Tunnel] Starting tunnel for http://localhost:{port}...", flush=True)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        url_regex = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        found_url = False

        for line in iter(proc.stdout.readline, ""):
            if not found_url:
                match = url_regex.search(line)
                if match:
                    tunnel_url = match.group(0)
                    CURRENT_TUNNEL_URL = tunnel_url
                    found_url = True
                    try:
                        with open(TUNNEL_URL_FILE, "w", encoding="utf-8") as f:
                            f.write(tunnel_url)
                    except Exception:
                        pass

                    print("=" * 72, flush=True)
                    print(f"👑 MyStore Admin Command Center Web Panel is LIVE!", flush=True)
                    print(f"🔗 Public URL: {tunnel_url}", flush=True)
                    print(f"🔑 Password:   {ADMIN_WEB_PASSWORD}", flush=True)
                    print("=" * 72, flush=True)

                    if on_tunnel_ready:
                        try:
                            on_tunnel_ready(tunnel_url)
                        except Exception as cb_err:
                            print(f"Tunnel callback error: {cb_err}", flush=True)

        proc.wait()
    except Exception as e:
        print(f"⚠️ [Cloudflare Tunnel] Process error: {e}", flush=True)


# -----------------------------------------------------------------------------
# Background Launcher for Bot Integration
# -----------------------------------------------------------------------------
def start_admin_web_in_background(port=5000, on_tunnel_ready=None):
    """
    Launch both the Admin Web Server and Cloudflare Tunnel concurrently in
    background threads so the Telegram bot polling is never blocked!
    """
    global _web_thread, _tunnel_thread

    def _web_runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = create_app()
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "0.0.0.0", port)
        loop.run_until_complete(site.start())
        print(f"⚡ [Admin Web Engine] Listening locally on http://0.0.0.0:{port}", flush=True)
        loop.run_forever()

    _web_thread = threading.Thread(target=_web_runner, daemon=True, name="AdminWebServer")
    _web_thread.start()

    _tunnel_thread = threading.Thread(
        target=run_cloudflare_tunnel,
        args=(port, on_tunnel_ready),
        daemon=True,
        name="CloudflareTunnel"
    )
    _tunnel_thread.start()


if __name__ == "__main__":
    port = int(os.getenv("ADMIN_PORT", 5000))
    print("🚀 Launching Standalone Admin Web Panel & Cloudflare Tunnel...")
    start_admin_web_in_background(port=port)
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Admin Web Panel stopped.")
