import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # -100...
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/+XbVUN9inkQ4yZDAy").strip()

if not BOT_TOKEN:
    raise RuntimeError("Задай BOT_TOKEN")
if ADMIN_ID == 0:
    raise RuntimeError("Задай ADMIN_ID")
if not CHANNEL_ID:
    raise RuntimeError("Задай CHANNEL_ID канала (начинается с -100)")
