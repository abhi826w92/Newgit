import os
from pathlib import Path

# Load environment variables from .env file if present
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

try:
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH)
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "8850296749:AAFsjQ58Te0NqlQv6mCIw0C2V90AtQKdeU0").strip()
BOT_SESSION_STRING = os.getenv("BOT_SESSION_STRING", "").strip()
API_ID = int(os.getenv("API_ID", "29116029"))
API_HASH = os.getenv("API_HASH", "867fafeeabc20a75163ef2ddbd877f70").strip()

API_BASE_URL = os.getenv("API_BASE_URL", "https://tgdriveapi.youganksaini1.workers.dev").rstrip("/")
DEFAULT_API_KEY = os.getenv("DEFAULT_API_KEY", "").strip()

db_env = os.getenv("DATABASE_PATH", "tgdrive_bot.db").strip()
if os.path.isabs(db_env):
    DATABASE_PATH = db_env
else:
    DATABASE_PATH = str((BASE_DIR / db_env).resolve())

TEMP_DIR = BASE_DIR / "temp_uploads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

GENERATE_KEY_URL = "https://tgdriveo.pages.dev/#/developer"

# Authorized Admin User IDs (Default Admins)
admin_env = os.getenv("ADMIN_IDS", "7249511572,7251749429")
ADMIN_IDS = [int(x.strip()) for x in admin_env.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    ADMIN_IDS = [7249511572, 7251749429]

