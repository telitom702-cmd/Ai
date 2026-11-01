from os import environ

SESSION = environ.get("SESSION", "DreamZBotz")
API_ID = int(environ.get("API_ID", ""))
API_HASH = environ.get("API_HASH", "")
BOT_TOKEN = environ.get("BOT_TOKEN", "")
ADMINS = [int(x) for x in environ.get("ADMINS", "").replace(",", " ").split() if x.isdigit()]
DATABASE_URI = environ.get("DATABASE_URI", "")
DATABASE_NAME = environ.get("DATABASE_NAME", "Cluster0")
PORT = int(environ.get("PORT", "8080"))
