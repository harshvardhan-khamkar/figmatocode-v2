from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import time
import os
import zipfile
import shutil
import traceback

from models import ConvertRequest
from Services.figma_service import get_figma_file
from Services.layout_parser import parse_figma_layout
from Services.ai_services import generate_code
from storedb import save_figma_json, update_parsed_layout, get_cached_figma

from Services.image import build_image_ref_map, extract_file_key
from Services.image import inject_images_into_layout
from storedb import get_cached_images

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def copy_templates(out_dir):
    shutil.copytree("templates", out_dir, dirs_exist_ok=True)


def export_images_to_assets(file_key, out_dir):
    assets_dir = os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    docs = get_cached_images(file_key)

    for d in docs.values():
        path = os.path.join(assets_dir, d["filename"])
        with open(path, "wb") as f:
            f.write(d["data"])

@app.get("/")
def root():
    return {"message": "Kriti backend running"}

@app.post("/convert")
def convert_design(req: ConvertRequest):
    try:
        figma_url = str(req.figma_url)
        framework = req.framework   # "html-tailwind" or "react"

        # -------- 1. Figma cache --------
        cached = get_cached_figma(figma_url)

        if cached and cached.get("figma_json"):
            figma_json = cached["figma_json"]
            print("[CACHE] Using stored Figma JSON")
        else:
            print("[FIGMA API] Fetching fresh JSON")
            figma_json = get_figma_file(figma_url)
            save_figma_json(figma_url, figma_json)

        # ------- Build image ref map ----------
        file_key = extract_file_key(figma_url)
        image_map = build_image_ref_map(figma_json, file_key)

        #----------------------------------------

        # -------- 2. Parse layout --------
        layout = parse_figma_layout(figma_json)
        update_parsed_layout(figma_url, layout)

        # -------- Inject images --------
        inject_images_into_layout(layout, image_map)

        # -------- 3. Output folder --------
        out_dir = "generated_site"
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)
        os.makedirs(out_dir)

        pages = layout.get("pages", [])
        if not pages:
            raise Exception("No pages found")

        # -------- HTML MODE --------
        if framework == "html-tailwind":
            for page in pages:
                for screen in page.get("screens", []):
                    screen_name = screen["screen"].lower().replace(" ", "_")
                    filename = f"{screen_name}.html"

                    screen_layout = {
                        "page": page["page"],
                        "screen": screen["screen"],
                        "box": screen["box"],
                        "tree": screen["tree"]
                    }

                    time.sleep(4)
                    code = generate_code(screen_layout, framework)

                    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
                        f.write(code)

        # -------- REACT MODE (PRODUCTION) --------
        elif framework == "react":
            time.sleep(4)

            # 1. Copy deterministic base project
            copy_templates(out_dir)

            # 2. AI generates ONLY UI files
            ui_files = generate_code(layout, framework)

            # 3. Inject AI files
            for path, content in ui_files.items():
                path = path.replace("FILE:", "").strip()
                full = os.path.join(out_dir, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                     f.write(content)

        else:
            raise Exception("Invalid framework")



        # ------- Export cached images to assets----------
        export_images_to_assets(file_key, out_dir)


        # -------- 4. Zip --------
        zip_path = "figma_site.zip"
        if os.path.exists(zip_path):
            os.remove(zip_path)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(out_dir):
                for file in files:
                    full = os.path.join(root, file)
                    zipf.write(full, arcname=os.path.relpath(full, out_dir))

        return {"downloadUrl": "http://127.0.0.1:8000/download"}

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download")
def download_zip():
    return FileResponse(
        "figma_site.zip",
        media_type="application/zip",
        filename="figma_site.zip"
    )


"""
figma → Kriti backend
FastAPI server for converting Figma designs into code.
backend last change is done on 03-02-2026 2:29

"""