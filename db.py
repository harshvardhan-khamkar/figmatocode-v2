from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

client = MongoClient(MONGO_URI)

db = client["figma_to_code"]

figma_collection = db["figma_cache"]

# OPTIONAL but recommended
figma_collection.create_index("figma_url", unique=True)
