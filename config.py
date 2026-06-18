import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DISCORD_GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
DISCORD_ROLE_ID = int(os.environ.get("DISCORD_ROLE_ID", "1517165526781137077"))
DISCORD_CLIENT_ID = int(os.environ.get("DISCORD_CLIENT_ID", "0"))
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", f"http://127.0.0.1:5000/callback")

FLASK_API_KEY = os.environ.get("FLASK_API_KEY", "nyapost-dev-key")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", FLASK_API_KEY)
FLASK_HOST = os.environ.get("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
FLASK_BASE_URL = os.environ.get("FLASK_BASE_URL", f"http://{FLASK_HOST}:{FLASK_PORT}")
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

MEMES_DIR = os.path.join(BASE_DIR, "memes")
DB_PATH = os.path.join(BASE_DIR, "nyapost.db")

MAX_FILE_SIZE = 500 * 1024 * 1024
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov"}
ALLOWED_MIME_PREFIXES = {"image/", "video/"}
