"""
Real-time Progress Bar Component for Telegram
Renders stylish progress bars: ▓▓▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ 5.50%
with live speed, size transfer, ETA metrics, and step-by-step state animations.
"""

import time


def make_progress_bar(current, total, length=18):
    """Generate visual progress bar string: ▓▓▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ 5.50%"""
    if total <= 0:
        return "▓" * length + " 100.00%"
    
    ratio = min(max(current / total, 0.0), 1.0)
    percent = ratio * 100
    filled = int(ratio * length)
    unfilled = length - filled
    bar = "▓" * filled + "▁" * unfilled
    return f"{bar} {percent:.2f}%"


def format_size(bytes_count):
    """Format bytes into human readable KB / MB / GB"""
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f} KB"
    elif bytes_count < 1024 * 1024 * 1024:
        return f"{bytes_count / (1024 * 1024):.2f} MB"
    return f"{bytes_count / (1024 * 1024 * 1024):.2f} GB"


def render_step_progress_text(action_title, step_num, total_steps, status_text, detail_info=""):
    """Render a stylish real-time step progress card"""
    bar = make_progress_bar(step_num, total_steps, length=18)
    detail_line = f"\n{detail_info}\n" if detail_info else ""
    return (
        f"⏳ <b>{action_title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <code>{bar}</code> (Step {step_num}/{total_steps})\n"
        f"{detail_line}"
        f"⚡ <i>{status_text}</i>"
    )


class ProgressCallbackHandler:
    def __init__(self, bot, chat_id, message_id, action_title="Uploading", filename="app.apk"):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.action_title = action_title
        self.filename = filename
        self.last_edit_time = 0
        self.start_time = time.time()

    def __call__(self, current, total):
        self.update(current, total)

    def update(self, current, total):
        now = time.time()
        # Throttle edits to max once every 1.5 seconds to prevent Telegram 429 rate limit
        if (now - self.last_edit_time < 1.5) and current < total:
            return

        self.last_edit_time = now
        elapsed = max(now - self.start_time, 0.001)
        speed = current / elapsed  # bytes per sec
        eta = (total - current) / speed if speed > 0 else 0

        bar = make_progress_bar(current, total, length=18)
        curr_str = format_size(current)
        tot_str = format_size(total)
        speed_str = f"{format_size(speed)}/s"
        eta_str = f"{int(eta)}s" if eta < 60 else f"{int(eta // 60)}m {int(eta % 60)}s"

        text = (
            f"⏳ <b>{self.action_title}</b>\n\n"
            f"📦 <b>File:</b> <code>{self.filename}</code>\n"
            f"📊 <code>{bar}</code>\n\n"
            f"💾 <b>Transfer:</b> <code>{curr_str} / {tot_str}</code>\n"
            f"⚡ <b>Speed:</b> <code>{speed_str}</code> | <b>ETA:</b> <code>{eta_str}</code>"
        )

        try:
            self.bot.edit_message_text(text, self.chat_id, self.message_id, parse_mode="HTML")
        except Exception:
            pass
