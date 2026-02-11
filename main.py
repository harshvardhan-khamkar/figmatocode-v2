from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import time
import os
import zipfile
import shutil
import traceback
import re
import base64
import hashlib
import urllib.parse

from models import ConvertRequest
from Services.figma_service import get_figma_file
from Services.layout_parser import parse_figma_layout
from Services.ai_services import generate_code
from storedb import save_figma_json, update_parsed_layout, get_cached_figma

from Services.image import build_image_ref_map, extract_file_key
from Services.image import inject_images_into_layout
from storedb import get_cached_images
from Services.ir_normalizer import normalize_layout

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

def _safe_component_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "", name)
    if not cleaned:
        return "Page"
    if not cleaned[0].isalpha():
        return "Page" + cleaned
    return cleaned

def _collect_fonts_from_layout(layout: dict) -> dict:
    families = {}

    def _walk(node):
        if not node:
            return
        style = node.get("style") or {}
        family = style.get("family")
        weight = style.get("weight")
        if family:
            if family not in families:
                families[family] = set()
            if isinstance(weight, (int, float)):
                families[family].add(int(weight))
        for child in node.get("children") or []:
            _walk(child)

    for page in layout.get("pages") or []:
        for screen in page.get("screens") or []:
            for node in screen.get("tree") or []:
                _walk(node)

    return families

def _build_google_fonts_import(families: dict) -> str | None:
    if not families:
        return None
    parts = []
    for family, weights in families.items():
        fam = family.replace(" ", "+")
        if weights:
            w = ";".join(str(w) for w in sorted(weights))
            parts.append(f"family={fam}:wght@{w}")
        else:
            parts.append(f"family={fam}")
    if not parts:
        return None
    return f'@import url("https://fonts.googleapis.com/css2?{"&".join(parts)}&display=swap");'

def _build_font_utilities_css(families: dict) -> str | None:
    if not families:
        return None
    lines = []
    default_family = None
    for family in families.keys():
        if not default_family:
            default_family = family
        slug = re.sub(r"[^a-z0-9]", "", family.lower())
        if not slug:
            continue
        lines.append(f".font-{slug} {{ font-family: '{family}', sans-serif; }}")
    if not lines:
        return None
    base = ""
    if default_family:
        base = (
            "@layer base {\n"
            f"  body {{ font-family: '{default_family}', sans-serif; }}\n"
            "}\n"
        )
    utilities = "@layer utilities {\n  " + "\n  ".join(lines) + "\n}\n"
    return base + utilities

def add_font_imports_to_index_css(layout: dict, out_dir: str):
    css_path = os.path.join(out_dir, "src", "index.css")
    if not os.path.exists(css_path):
        return
    families = _collect_fonts_from_layout(layout)
    font_import = _build_google_fonts_import(families)
    if not font_import:
        return
    font_utils = _build_font_utilities_css(families)
    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()
    if "fonts.googleapis.com/css2" in css:
        return
    if font_utils and "font-" not in css:
        css = font_import + "\n" + font_utils + "\n" + css
    else:
        css = font_import + "\n" + css
    with open(css_path, "w", encoding="utf-8") as f:
        f.write(css)

def ensure_react_entry(ui_files: dict) -> dict:
    if any(p.endswith("src/App.jsx") for p in ui_files.keys()):
        return ui_files

    pages = sorted(
        p for p in ui_files.keys()
        if p.startswith("src/pages/") and p.endswith(".jsx")
    )

    if pages:
        imports = []
        routes = []
        first = True
        for p in pages:
            base = os.path.basename(p).replace(".jsx", "")
            comp = _safe_component_name(base)
            rel = "./" + p.replace("src/", "")
            imports.append(f'import {comp} from "{rel}";')
            if first:
                routes.append(f'        <Route path="/" element={{<{comp} />}} />')
                first = False
            slug = base.strip().lower().replace(" ", "-")
            if slug:
                routes.append(f'        <Route path="/{slug}" element={{<{comp} />}} />')

        app = "\n".join([
            'import { BrowserRouter, Routes, Route } from "react-router-dom";',
            *imports,
            "",
            "export default function App() {",
            "  return (",
            "    <BrowserRouter>",
            "      <Routes>",
            *routes,
            "      </Routes>",
            "    </BrowserRouter>",
            "  );",
            "}",
            "",
        ])
        ui_files["src/App.jsx"] = app
        return ui_files

    # Fallback: render the first component file if no pages were generated.
    first_file = next((p for p in ui_files.keys() if p.startswith("src/") and p.endswith(".jsx")), None)
    if first_file and not first_file.endswith("src/App.jsx"):
        base = os.path.basename(first_file).replace(".jsx", "")
        comp = _safe_component_name(base)
        rel = "./" + first_file.replace("src/", "")
        app = "\n".join([
            f'import {comp} from "{rel}";',
            "",
            "export default function App() {",
            f"  return <{comp} />;",
            "}",
            "",
        ])
        ui_files["src/App.jsx"] = app
        return ui_files

    ui_files["src/App.jsx"] = "export default function App() { return <div>App is running</div>; }\n"
    return ui_files


def export_images_to_assets(file_key, out_dir, public_assets=False):
    if public_assets:
        assets_dir = os.path.join(out_dir, "public", "assets")
    else:
        assets_dir = os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    docs = get_cached_images(file_key)

    for d in docs.values():
        path = os.path.join(assets_dir, d["filename"])
        with open(path, "wb") as f:
            f.write(d["data"])

def _extract_data_uris(html: str, out_dir: str, public_assets: bool = False) -> str:
    if not html:
        return html

    assets_dir = os.path.join(out_dir, "public", "assets") if public_assets else os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    url_prefix = "/assets" if public_assets else "assets"

    def _write_asset(raw: bytes, mime: str) -> str:
        ext = "bin"
        if "svg" in mime:
            ext = "svg"
        elif "png" in mime:
            ext = "png"
        elif "jpeg" in mime or "jpg" in mime:
            ext = "jpg"
        elif "webp" in mime:
            ext = "webp"
        name = hashlib.sha1(raw).hexdigest()[:16]
        filename = f"inline-{name}.{ext}"
        path = os.path.join(assets_dir, filename)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(raw)
        return f"{url_prefix}/{filename}"

    def _save_data_uri(m):
        mime = (m.group(1) or "").lower()
        data = m.group(2) or ""
        try:
            raw = base64.b64decode(data)
        except Exception:
            return m.group(0)
        return f'src="{_write_asset(raw, mime)}"'

    html = re.sub(
        r'src="data:([^;]+);base64,([^"]+)"',
        _save_data_uri,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html = re.sub(
        r"src='data:([^;]+);base64,([^']+)'",
        _save_data_uri,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _save_svg_text_uri(m):
        data = m.group(1) or ""
        try:
            raw_text = urllib.parse.unquote(data)
            raw = raw_text.encode("utf-8")
        except Exception:
            return m.group(0)
        return f'src="{_write_asset(raw, "image/svg+xml")}"'

    html = re.sub(
        r'src="data:image/svg\+xml(?:;charset=[^,;]+)?(?:;utf8)?,([^"]+)"',
        _save_svg_text_uri,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html = re.sub(
        r"src='data:image/svg\+xml(?:;charset=[^,;]+)?(?:;utf8)?,([^']+)'",
        _save_svg_text_uri,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _save_css_uri(m):
        mime = (m.group(1) or "").lower()
        data = m.group(2) or ""
        try:
            raw = base64.b64decode(data)
        except Exception:
            return m.group(0)
        return f'url("{_write_asset(raw, mime)}")'

    html = re.sub(
        r'url\(\s*[\"\']?data:([^;]+);base64,([^\"\')]+)[\"\']?\s*\)',
        _save_css_uri,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _save_css_text_uri(m):
        data = m.group(1) or ""
        try:
            raw_text = urllib.parse.unquote(data)
            raw = raw_text.encode("utf-8")
        except Exception:
            return m.group(0)
        return f'url("{_write_asset(raw, "image/svg+xml")}")'

    html = re.sub(
        r'url\(\s*[\"\']?data:image/svg\+xml(?:;charset=[^,;]+)?(?:;utf8)?,([^\"\')]+)[\"\']?\s*\)',
        _save_css_text_uri,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return html



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
        image_base = "/assets" if framework == "react" else "assets"
        image_map = build_image_ref_map(figma_json, file_key, base_path=image_base)

        #----------------------------------------

        # -------- 2. Parse layout --------
        layout = parse_figma_layout(figma_json)
        update_parsed_layout(figma_url, layout)

        # -------- Inject images --------
        inject_images_into_layout(layout, image_map)
        layout = normalize_layout(layout)

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
                    code = generate_code(screen_layout, framework, route_layout=layout)
                    code = _extract_data_uris(code, out_dir, public_assets=False)

                    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
                        f.write(code)

        # -------- REACT MODE (PRODUCTION) --------
        elif framework == "react":
            time.sleep(4)

            # 1. Copy deterministic base project
            copy_templates(out_dir)

            # 2. AI generates ONLY UI files
            ui_files = generate_code(layout, framework)
            ui_files = ensure_react_entry(ui_files)

            # 3. Inject AI files
            for path, content in ui_files.items():
                path = path.replace("FILE:", "").strip()
                content = _extract_data_uris(content, out_dir, public_assets=True)
                full = os.path.join(out_dir, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                     f.write(content)
            add_font_imports_to_index_css(layout, out_dir)

        else:
            raise Exception("Invalid framework")



        # ------- Export cached images to assets----------
        export_images_to_assets(file_key, out_dir, public_assets=(framework == "react"))


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
