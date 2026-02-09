import os
import requests
import re
import time
from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv()

FIGMA_TOKEN = os.getenv("FIGMA_TOKEN")

if not FIGMA_TOKEN:
    raise RuntimeError("FIGMA_TOKEN not set")

HEADERS = {
    "X-Figma-Token": FIGMA_TOKEN
}


def extract_file_key(figma_url: str) -> str:
    match = re.search(r"/(file|design|make)/([a-zA-Z0-9]+)", figma_url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Figma URL")
    return match.group(2)


def get_figma_file(figma_url: str) -> dict:
    file_key = extract_file_key(figma_url)
    time.sleep(1)

    url = f"https://api.figma.com/v1/files/{file_key}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def get_all_image_fills(file_key: str) -> dict:
    """
    Fetch all image fills from a Figma file.
    
    Returns a dict mapping imageRef → image URL
    Example:
    {
        "cb553807f2dc423cec4152a5f7309eeda29ca9fe": "https://s3-alpha.figma.com/img/..."
    }
    """
    res = requests.get(
        f"https://api.figma.com/v1/files/{file_key}/images",
        headers={"X-Figma-Token": FIGMA_TOKEN}
    )

    if res.status_code != 200:
        print("[FIGMA IMAGE ERROR]", res.status_code, res.text)
        return {}

    images = res.json().get("images", {})
    print("[FIGMA] Fetched", len(images), "image fills from Figma")

    return images

import base64

def image_url_to_base64(url: str) -> str:
    r = requests.get(url)
    r.raise_for_status()
    return base64.b64encode(r.content).decode("utf-8")
