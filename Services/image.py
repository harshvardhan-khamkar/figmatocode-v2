import os
import requests
import re
from dotenv import load_dotenv
from fastapi import HTTPException
import time
from storedb import save_images_binary


load_dotenv()

FIGMA_TOKEN = os.getenv("FIGMA_TOKEN")

HEADERS = {
    "X-Figma-Token": FIGMA_TOKEN
}

# ------------------------------------------------------------------
# BASIC FIGMA HELPERS
# ------------------------------------------------------------------

def extract_file_key(figma_url: str) -> str:
    figma_url = str(figma_url)

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

# ------------------------------------------------------------------
# NODE MAP (node_id → node)
# ------------------------------------------------------------------

def build_node_map(node, node_map=None):
    if node_map is None:
        node_map = {}

    node_map[node["id"]] = node

    for child in node.get("children", []):
        build_node_map(child, node_map)

    return node_map

# ------------------------------------------------------------------
# IMAGE NODE EXTRACTION
# ------------------------------------------------------------------

RENDERABLE_IMAGE_TYPES = {
    "RECTANGLE",
    "FRAME",
    "VECTOR",
    "ELLIPSE",
    "INSTANCE",
    "COMPONENT",
    "GROUP",
    "BOOLEAN_OPERATION",
    "STAR",
    "LINE",
    "POLYGON"
}

def extract_image_node_ids(node, image_nodes=None):
    if image_nodes is None:
        image_nodes = []

    fills = node.get("fills", [])
    if (
        fills
        and node.get("type") in RENDERABLE_IMAGE_TYPES
        and any(f.get("type") == "IMAGE" for f in fills)
    ):
        image_nodes.append(node["id"])

    for child in node.get("children", []):
        extract_image_node_ids(child, image_nodes)

    return image_nodes



def extract_instance_image_refs(node, instance_nodes=None):
    if instance_nodes is None:
        instance_nodes = []

    if node.get("type") in {"INSTANCE", "COMPONENT"}:
        if any(f.get("type") == "IMAGE" for f in node.get("fills", [])):
            instance_nodes.append(node["id"])

    for child in node.get("children", []):
        extract_instance_image_refs(child, instance_nodes)

    return instance_nodes


def extract_logo_like_nodes(node, logo_nodes=None):
    if logo_nodes is None:
        logo_nodes = []

    name = (node.get("name") or "").lower()
    node_type = node.get("type")

    if "logo" in name:
        logo_nodes.append(node["id"])

    # Prefer rendering vector-like nodes that look like logos even if not named logo
    if node_type in {"VECTOR", "BOOLEAN_OPERATION"} and "logo" in name:
        logo_nodes.append(node["id"])

    for child in node.get("children", []):
        extract_logo_like_nodes(child, logo_nodes)

    return logo_nodes

# ------------------------------------------------------------------
# FIGMA IMAGE API
# ------------------------------------------------------------------

def get_figma_images(file_key: str, node_ids: list) -> dict:
    if not node_ids:
        return {}

    url = f"https://api.figma.com/v1/images/{file_key}"
    params = {
        "ids": ",".join(node_ids[:100]),
        "format": "png"
    }

    response = requests.get(url, headers=HEADERS, params=params)
    response.raise_for_status()

    return response.json().get("images", {})

# ------------------------------------------------------------------
# DOWNLOAD IMAGES (imageRef → local path)
# ------------------------------------------------------------------

def download_figma_images(images: dict, node_map: dict, output_dir="figma_images"):
    os.makedirs(output_dir, exist_ok=True)
    local_map = {}

    for node_id, img_url in images.items():
        if not img_url:
            continue

        node = node_map.get(node_id)
        if not node:
            continue

        image_ref = None

        for fill in node.get("fills", []):
            if fill.get("type") == "IMAGE":
                image_ref = fill.get("imageRef")
                break

        if not image_ref:
            for bg in node.get("background", []):
                if bg.get("type") == "IMAGE":
                    image_ref = bg.get("imageRef")
                    break

        if not image_ref:
            continue

        filename = f"{image_ref}.png"
        path = os.path.abspath(os.path.join(output_dir, filename))

        r = requests.get(img_url)
        with open(path, "wb") as f:
            f.write(r.content)

        # IMPORTANT: map imageRef → local file
        local_map[image_ref] = path

    return local_map

# ------------------------------------------------------------------
# MASTER FUNCTION (THIS IS WHAT YOU CALL)
# ------------------------------------------------------------------

from storedb import get_cached_images, save_images_binary

def build_image_ref_map(raw_figma_json: dict, file_key: str):
    # 1. CHECK CACHE FIRST
    cached = get_cached_images(file_key)

    # 2. DOCUMENT REQUIRED FOR IMAGE/LOGO DETECTION
    document = raw_figma_json.get("document")
    if not document:
        return {}

    node_map = build_node_map(document)
    image_nodes = extract_image_node_ids(document)
    instance_image_nodes = extract_instance_image_refs(document)
    logo_nodes = extract_logo_like_nodes(document)
    all_image_nodes = list(set(image_nodes + instance_image_nodes + logo_nodes))

    # Collect expected image refs from fills/backgrounds
    expected_refs = set()
    for node in node_map.values():
        for fill in node.get("fills", []):
            if fill.get("type") == "IMAGE" and fill.get("imageRef"):
                expected_refs.add(fill.get("imageRef"))
        for bg in node.get("background", []):
            if bg.get("type") == "IMAGE" and bg.get("imageRef"):
                expected_refs.add(bg.get("imageRef"))

    if cached:
        print("[CACHE] Using stored images")
        missing_refs = [r for r in expected_refs if r not in cached]
        missing_logo_nodes = []
        for node_id in logo_nodes:
            safe_id = node_id.replace(":", "-")
            node_key = f"node-{safe_id}"
            if node_key not in cached:
                missing_logo_nodes.append(node_id)

        if not missing_refs and not missing_logo_nodes:
            return {
                image_ref: f"assets/{image_ref}.png"
                for image_ref in cached.keys()
            }

        print("[CACHE] Missing images detected, fetching from API")

    print("[FIGMA] Fetching images from API")
    images = get_figma_images(file_key, all_image_nodes)

    bin_map = {}

    for node_id, url in images.items():
        if not url:
            continue

        node = node_map.get(node_id)
        if not node:
            continue

        image_ref = None
        for fill in node.get("fills", []):
            if fill.get("type") == "IMAGE":
                image_ref = fill.get("imageRef")
                break

        if not image_ref:
            for bg in node.get("background", []):
                if bg.get("type") == "IMAGE":
                    image_ref = bg.get("imageRef")
                    break

        if not image_ref:
            # Fall back to node id for vector/logo renders
            safe_id = node_id.replace(":", "-")
            image_ref = f"node-{safe_id}"

        r = requests.get(url)
        bin_map[image_ref] = r.content

    # 3. STORE ONCE
    save_images_binary(file_key, bin_map)

    # 4. RETURN MAP (use relative paths for local file usage)
    return {
        image_ref: f"assets/{image_ref}.png"
        for image_ref in bin_map.keys()
    }

# ------------------------------------------------------------------
# IMAGE INJECTION (ADD THIS)
# ------------------------------------------------------------------

def inject_images(node, image_map):
    style = node.get("style", {})
    ref = style.get("imageRef")

    if ref and ref in image_map:
        style["imageUrl"] = image_map[ref]
        style["image"] = True
    else:
        node_id = node.get("id")
        if node_id:
            safe_id = node_id.replace(":", "-")
            node_key = f"node-{safe_id}"
            if node_key in image_map:
                style["imageUrl"] = image_map[node_key]
                style["image"] = True
                name = (node.get("name") or "").lower()
                # Avoid rendering text/children if a logo image is available
                if node.get("type") == "TEXT":
                    node["text"] = None
                if "logo" in name:
                    node["children"] = []

    for child in node.get("children", []):
        inject_images(child, image_map)


def inject_images_into_layout(layout, image_map):
    for page in layout.get("pages", []):
        for screen in page.get("screens", []):
            for node in screen.get("tree", []):
                inject_images(node, image_map)
