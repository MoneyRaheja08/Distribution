from motor.motor_asyncio import AsyncIOMotorClient

from .config import settings

# Client is created lazily; no connection is made until the first operation.
client = AsyncIOMotorClient(settings.mongo_uri)
db = client[settings.db_name]
