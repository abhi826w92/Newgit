import html
import time
from datetime import datetime

def format_bytes(size_bytes: int) -> str:
    """Format bytes to human-readable string (KB, MB, GB)."""
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    size = float(size_bytes)
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"

def format_date(timestamp_ms: int) -> str:
    """Format millisecond timestamp to readable date string."""
    if not timestamp_ms:
        return "N/A"
    try:
        if timestamp_ms > 1e11:
            dt = datetime.fromtimestamp(timestamp_ms / 1000.0)
        else:
            dt = datetime.fromtimestamp(timestamp_ms)
        return dt.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return str(timestamp_ms)

def get_mime_icon(mime_type: str = "", filename: str = "") -> str:
    """Return appropriate emoji icon based on mime type or file extension."""
    mime = (mime_type or "").lower()
    fn = (filename or "").lower()

    if "video" in mime or fn.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv")):
        return "🎬"
    elif "image" in mime or fn.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")):
        return "🖼️"
    elif "audio" in mime or fn.endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac")):
        return "🎵"
    elif "pdf" in mime or fn.endswith(".pdf"):
        return "📕"
    elif "zip" in mime or "tar" in mime or "rar" in mime or fn.endswith((".zip", ".tar", ".gz", ".7z", ".rar")):
        return "📦"
    elif "android" in mime or fn.endswith(".apk"):
        return "📱"
    elif "text" in mime or fn.endswith((".txt", ".py", ".js", ".json", ".html", ".css", ".md")):
        return "📄"
    else:
        return "📁" if mime == "folder" else "📎"

def clean_html(text: str) -> str:
    """Escape text for HTML parse mode."""
    if not text:
        return ""
    return html.escape(str(text))

def make_progress_bar(current: int, total: int, length: int = 15) -> str:
    """
    Generate visual unicode progress bar.
    Example: ██████░░░░░░░░░ 40.00%
    """
    if total <= 0:
        return "░" * length + " 0.00%"
    ratio = current / total
    ratio = min(1.0, max(0.0, ratio))
    filled = int(round(length * ratio))
    empty = length - filled
    bar = "█" * filled + "░" * empty
    percent = ratio * 100.0
    return f"{bar} {percent:.2f}%"

def format_time_remaining(seconds: float) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    if not seconds or seconds < 0 or seconds > 86400:
        return "Calculating..."
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def build_progress_card(action: str, filename: str, current: int, total: int, start_time: float, icon: str = "📦") -> str:
    """Build a rich, beautiful progress card."""
    elapsed = max(0.001, time.time() - start_time)
    speed = current / elapsed
    eta = (total - current) / speed if speed > 0 and total > current else 0
    bar_str = make_progress_bar(current, total, length=15)

    return (
        f"<b>{action} {icon} <code>{clean_html(filename)}</code></b>\n\n"
        f"<code>{bar_str}</code>\n\n"
        f"📊 <b>Progress:</b> <code>{format_bytes(current)} / {format_bytes(total)}</code>\n"
        f"⚡ <b>Speed:</b> <code>{format_bytes(int(speed))}/s</code>\n"
        f"⏳ <b>ETA:</b> <code>{format_time_remaining(eta)}</code> | ⏱️ <b>Elapsed:</b> <code>{format_time_remaining(elapsed)}</code>"
    )

def build_loading_card(title: str, percent: float, status_text: str = "Loading...") -> str:
    """Build a visual loading progress screen with unicode bar."""
    total = 100
    current = int(percent)
    bar_str = make_progress_bar(current, total, length=16)
    return (
        f"<b>{title}</b>\n\n"
        f"<code>{bar_str}</code>\n\n"
        f"⏳ <i>{clean_html(status_text)}</i>"
    )

class ProgressTracker:
    """Throttled progress tracker to avoid Telegram FloodWait with real-time Unicode bar."""
    def __init__(self, message, action: str, filename: str, icon: str, total_size: int, update_interval: float = 1.5):
        self.message = message
        self.action = action
        self.filename = filename
        self.icon = icon
        self.total_size = total_size
        self.start_time = time.time()
        self.last_update_time = 0
        self.update_interval = update_interval

    async def callback(self, current: int, total: int):
        now = time.time()
        if not total or total <= 0:
            total = self.total_size

        # Update if interval elapsed, or on first call (0%), or on completion (100%)
        if (self.last_update_time == 0) or (now - self.last_update_time >= self.update_interval) or (current >= total and current > 0):
            self.last_update_time = now
            card_text = build_progress_card(
                action=self.action,
                filename=self.filename,
                current=current,
                total=total,
                start_time=self.start_time,
                icon=self.icon
            )
            try:
                await self.message.edit(card_text, parse_mode="html")
            except Exception:
                pass
