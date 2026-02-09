from pymongo import MongoClient
from datetime import datetime, timedelta
import os

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

# client = MongoClient(MONGO_URI)
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)

db = client["figma_to_code"]

# ---------- MAIN FIGMA CACHE ----------
collection = db["figma_files"]

collection.create_index(
    [("figma_url", 1)],
    unique=True
)

def save_figma_json(figma_url: str, figma_json: dict):
    collection.update_one(
        {"figma_url": figma_url},
        {
            "$set": {
                "figma_url": figma_url,
                "figma_json": figma_json,
                "updated_at": datetime.utcnow()
            }
        },
        upsert=True
    )

def update_parsed_layout(figma_url: str, layout: dict):
    collection.update_one(
        {"figma_url": figma_url},
        {
            "$set": {
                "parsed_layout": layout,
                "parsed_at": datetime.utcnow()
            }
        }
    )

def get_cached_figma(figma_url: str):
    return collection.find_one(
        {"figma_url": figma_url},
        {"_id": 0}
    )

# ---------- IMAGE CACHE ----------
figma_images = db["figma_images"]

figma_images.create_index(
    [("figma_file_key", 1), ("imageRef", 1)],
    unique=True
)

def get_cached_images(figma_file_key: str) -> dict:
    docs = figma_images.find(
        {"figma_file_key": figma_file_key},
        {"_id": 0}
    )
    return {d["imageRef"]: d for d in docs}


def save_images_binary(figma_file_key: str, images: dict[str, bytes]):
    for image_ref, data in images.items():
        figma_images.update_one(
            {
                "figma_file_key": figma_file_key,
                "imageRef": image_ref
            },
            {
                "$set": {
                    "filename": image_ref + ".png",
                    "data": data,
                    "updated_at": datetime.utcnow()
                }
            },
            upsert=True
        )
