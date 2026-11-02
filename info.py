from os import environ

SESSION = environ.get("SESSION", "DreamZBotz")
API_ID = int(environ.get("API_ID", "24776633"))
API_HASH = environ.get("API_HASH", "57b1f632044b4e718f5dce004a988d69")
BOT_TOKEN = environ.get("BOT_TOKEN", "")
ADMINS = [int(x) for x in environ.get("ADMINS", "8248792819").replace(",", " ").split() if x.isdigit()]
DATABASE_URI = environ.get("DATABASE_URI", "mongodb+srv://rendamd1_db_user:M7vb8ZD9rx0AfHnP@cluster0.uzqvib6.mongodb.net/?appName=Cluster0")
DATABASE_NAME = environ.get("DATABASE_NAME", "Cluster")
PORT = int(environ.get("PORT", "8080"))
