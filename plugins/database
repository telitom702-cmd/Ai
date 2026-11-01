from motor.motor_asyncio import AsyncIOMotorClient

from info import DATABASE_NAME, DATABASE_URI

if not DATABASE_URI:
    raise RuntimeError("Missing required environment variable: DATABASE_URI")

client = AsyncIOMotorClient(DATABASE_URI)
db = client[DATABASE_NAME]
