import os
import time
import json
import re
import ast
from google.genai import Client
from dotenv import load_dotenv

load_dotenv()
client = Client(api_key=os.getenv("GEMINI_API_KEY"))

MOJIBAKE_REPLACEMENTS = {
    "\u00c3\u00a2\u00e2\u201a\u00ac\u00e2\u201e\u00a2": "\u2019",
    "\u00c3\u00a2\u00e2\u201a\u00ac\u00cb\u0153": "\u2018",
    "\u00c3\u00a2\u00e2\u201a\u00ac\u00c5\u201c": "\u201c",
    "\u00c3\u00a2\u00e2\u201a\u00ac\u00c2\u009d": "\u201d",
    "\u00c3\u00a2\u00e2\u201a\u00ac\u201c\u201c": "\u2013",
    "\u00c3\u00a2\u00e2\u201a\u00ac\u201d\u201c": "\u2014",
    "\u00c3\u201a\u00c2\u00a9": "\u00a9",
    "\u00c3\u201a\u00c2\u00ae": "\u00ae",
    "\u00c2\u00a9": "\u00a9",
}


def _fix_mojibake(text: str) -> str:
    if not text:
        return text
    for bad, good in MOJIBAKE_REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text


def _sanitize_html_output(html: str) -> str:
    """
    Post-process model HTML to avoid invalid CSS and duplicate style blocks.
    - Remove any <style> blocks that are NOT type="text/tailwindcss"
    - Normalize tracking classes like tracking-[-1_5px] to tracking-n1_5 when defined
    - Normalize non-standard spacing classes like gap-30 or pt-15 to bracketed px
    """
    if not html:
        return html

    # Remove/repair malformed HTML comments that can swallow real markup.
    # Example: "<!-- Main<div ...>" becomes "<div ...>"
    html = re.sub(r"<!--\s*[^<]*<", "<", html)
    # Strip any remaining HTML comments
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

    # Fix stray 'html' tokens inside arbitrary value classes
    html = re.sub(r"(text|leading|tracking)-\[([0-9.\-]+)px\s*html", r"\1-[\2px]", html)

    def _extract_defined_classes(content: str) -> set[str]:
        defined = set()
        for style in re.findall(
            r"<style[^>]*type=[\"']text/tailwindcss[\"'][^>]*>(.*?)</style>",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            for m in re.finditer(r"\.([a-zA-Z0-9_\\\-\[\]\.]+)\s*\{", style):
                name = m.group(1)
                defined.add(name)
                defined.add(name.replace("\\", ""))
        return defined

    # Remove stray leading tokens like "html" before the doctype.
    html = re.sub(r"^\s*html\s+(?=<!DOCTYPE html>)", "", html, flags=re.IGNORECASE)

    # Remove non-tailwind <style> blocks
    html = re.sub(
        r"<style(?![^>]*type=[\"']text/tailwindcss[\"'])[^>]*>.*?</style>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Clamp extreme letter-spacing values to prevent collapsed text.
    # This applies to all letter-spacing declarations emitted by the model.
    def _clamp_letter_spacing(match):
        prefix = match.group(1)
        value = float(match.group(2))
        if abs(value) <= 10:
            clamped = value
        else:
            clamped = max(-10.0, min(10.0, value))
        # Preserve integer formatting when possible
        if float(clamped).is_integer():
            clamped_str = str(int(clamped))
        else:
            clamped_str = f"{clamped:.3f}".rstrip("0").rstrip(".")
        return f"{prefix}{clamped_str}px"

    html = re.sub(
        r"(letter-spacing:\s*)(-?\d+(?:\.\d+)?)px",
        _clamp_letter_spacing,
        html,
    )

    VALID_SPACING_SCALE = {
        "0", "0.5", "1", "1.5", "2", "2.5", "3", "3.5", "4", "5", "6",
        "7", "8", "9", "10", "11", "12", "14", "16", "20", "24", "28",
        "32", "36", "40", "44", "48", "52", "56", "60", "64", "72", "80", "96"
    }

    defined_classes = _extract_defined_classes(html)

    def _normalize_dash_decimal(value: str) -> str:
        if re.match(r"^\d+-\d+$", value):
            left, right = value.split("-", 1)
            return f"{left}.{right}"
        return value

    def _alias_custom_color(token: str) -> str:
        if token in defined_classes:
            return token
        m = re.match(r"^(text|bg|border)-custom-([a-zA-Z0-9\-]+)$", token)
        if not m:
            return token
        prefix, name = m.group(1), m.group(2)
        # Try gray/grey swap if only one exists
        if "gray" in name:
            alt = name.replace("gray", "grey")
            candidate = f"{prefix}-custom-{alt}"
            if candidate in defined_classes:
                return candidate
        if "grey" in name:
            alt = name.replace("grey", "gray")
            candidate = f"{prefix}-custom-{alt}"
            if candidate in defined_classes:
                return candidate
        # Try dropping -dark/-light if base exists
        base = re.sub(r"-(dark|light|lighter|medium)$", "", name)
        if base != name:
            candidate = f"{prefix}-custom-{base}"
            if candidate in defined_classes:
                return candidate
        return token

    def _replace_spacing_token(token: str) -> str:
        # Convert spacing tokens not in default scale to bracketed px.
        # Examples: gap-30 -> gap-[30px], pt-15 -> pt-[15px], space-x-27 -> space-x-[27px]
        if token in defined_classes:
            return token
        m = re.match(r"^(gap|p[trblxy]?|m[trblxy]?|space-[xy])-(\d+(?:-\d+)?(?:\.\d+)?)$", token)
        if not m:
            return token
        prefix = m.group(1)
        raw_value = m.group(2)
        value = _normalize_dash_decimal(raw_value)
        return f"{prefix}-[{value}px]"

    def _fix_padding_margin_prefixes(token: str) -> str:
        # Fix invalid tailwind prefixes like p-l-40 -> pl-40
        token = re.sub(r"^(p)-([trblxy])-", r"\1\2-", token)
        token = re.sub(r"^(m)-([trblxy])-", r"\1\2-", token)
        return token

    def _normalize_px_suffix(token: str) -> str:
        if token in defined_classes:
            return token
        neg = token.startswith("-")
        core = token[1:] if neg else token
        prefixes = (
            "gap", "space-x", "space-y",
            "p", "pt", "pr", "pb", "pl", "px", "py",
            "m", "mt", "mr", "mb", "ml", "mx", "my",
            "w", "h", "min-w", "max-w", "min-h", "max-h",
            "top", "right", "bottom", "left", "inset", "inset-x", "inset-y",
            "translate-x", "translate-y",
            "rounded", "rounded-t", "rounded-b", "rounded-l", "rounded-r",
            "rounded-tl", "rounded-tr", "rounded-bl", "rounded-br",
        )
        prefix_group = "|".join(re.escape(p) for p in prefixes)
        m = re.match(rf"^({prefix_group})-(\d+(?:\.\d+)?)px$", core)
        if not m:
            return token
        prefix, value = m.group(1), m.group(2)
        converted = f"{prefix}-[{value}px]"
        return f"-{converted}" if neg else converted


    # Normalize tracking class tokens in class=""
    def _replace_tracking_token(token: str) -> str:
        # token like tracking-[-1_5px] or tracking-[0_35px]
        m = re.match(r"tracking-\[([+-]?[\d_\.]+)px\]", token)
        if not m:
            return token
        val = m.group(1)
        prefix = "n" if val.startswith("-") else "p"
        cleaned = val.lstrip("+-").replace(".", "_")
        candidate = f"tracking-{prefix}{cleaned}"
        if candidate in defined_classes:
            return candidate
        return token

    def _normalize_text_leading_numeric(token: str) -> str:
        if token in defined_classes:
            return token
        m = re.match(r"^(text|leading)-(\d+(?:-\d+)?(?:\.\d+)?)$", token)
        if not m:
            return token
        prefix = m.group(1)
        value = _normalize_dash_decimal(m.group(2))
        return f"{prefix}-[{value}px]"

    def _normalize_tracking_alias(token: str) -> str:
        if token in defined_classes:
            return token
        m = re.match(r"^tracking-(neg-)?([np])?(-)?(\d+(?:[_-]\d+)?)$", token)
        if not m:
            return token
        neg = bool(m.group(1)) or bool(m.group(3)) or (m.group(2) == "n")
        val = m.group(4).replace("_", ".")
        val = _normalize_dash_decimal(val)
        sign = "-" if neg else ""
        return f"tracking-[{sign}{val}px]"

    def _normalize_numeric_token(token: str) -> str:
        if token in defined_classes:
            return token
        neg = token.startswith("-")
        core = token[1:] if neg else token
        if "[" in core or "/" in core:
            return token
        prefixes = (
            "w", "h", "min-w", "max-w", "min-h", "max-h",
            "top", "right", "bottom", "left", "inset", "inset-x", "inset-y",
            "translate-x", "translate-y",
            "rounded", "rounded-t", "rounded-b", "rounded-l", "rounded-r",
            "rounded-tl", "rounded-tr", "rounded-bl", "rounded-br",
        )
        prefix_group = "|".join(re.escape(p) for p in prefixes)
        m = re.match(rf"^({prefix_group})-(\d+(?:-\d+)?(?:\.\d+)?)$", core)
        if not m:
            return token
        prefix, value = m.group(1), _normalize_dash_decimal(m.group(2))
        converted = f"{prefix}-[{value}px]"
        return f"-{converted}" if neg else converted

    def _replace_class_attr(match):
        classes = match.group(1)
        tokens = classes.split()
        normalized = []
        for t in tokens:
            if t == "html":
                continue
            t = t.replace("\\.", ".")
            t = _replace_tracking_token(t)
            t = _replace_spacing_token(t)
            t = _fix_padding_margin_prefixes(t)
            t = _normalize_px_suffix(t)
            t = _alias_custom_color(t)
            t = _normalize_text_leading_numeric(t)
            t = _normalize_tracking_alias(t)
            t = _normalize_numeric_token(t)
            normalized.append(t)
        tokens = normalized
        # Normalize model-generated prefixes like leading-line-height-* -> line-height-*
        # and tracking-letter-spacing-* -> letter-spacing-*
        tokens = [re.sub(r"^leading-line-height-", "line-height-", t) for t in tokens]
        tokens = [re.sub(r"^tracking-letter-spacing-", "letter-spacing-", t) for t in tokens]
        return f'class="{" ".join(tokens)}"'

    html = re.sub(r'class="([^"]+)"', _replace_class_attr, html)

    # Normalize tracking selectors inside CSS when a normalized class exists
    def _replace_tracking_selector(match):
        val = match.group(1)
        prefix = "n" if val.startswith("-") else "p"
        cleaned = val.lstrip("+-").replace(".", "_")
        candidate = f"tracking-{prefix}{cleaned}"
        if candidate in defined_classes:
            return f".{candidate}"
        return match.group(0)

    html = re.sub(r"\.tracking-\[([+-]?[\d_\.]+)px\]", _replace_tracking_selector, html)
    # Fix malformed tracking class selectors like ".tracking-.tracking-custom-tight-3\.2"
    html = html.replace(".tracking-.tracking-", ".tracking-")

    # If a container uses overflow-hidden but has absolutely-positioned children
    # with negative offsets, remove overflow-hidden to allow intentional overlap.
    def _relax_overflow_hidden(match):
        classes = match.group(1)
        tail = match.group(2)
        if "clip-true" in classes or "clip-content" in classes:
            return f'<div class="{classes}">{tail}'
        if re.search(r"\babsolute\b", tail) and re.search(r"(?:top|left|right|bottom)-\[-|-(?:top|left|right|bottom)-\[", tail):
            classes = re.sub(r"\boverflow-hidden\b", "", classes).strip()
            classes = re.sub(r"\s{2,}", " ", classes)
        return f'<div class="{classes}">{tail}'

    html = re.sub(
        r'<div class="([^"]*\boverflow-hidden\b[^"]*)">(.{0,8000})',
        _relax_overflow_hidden,
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    return html


def _ensure_custom_class(html: str, class_name: str, css_body: str) -> str:
    if not html or class_name not in html:
        return html
    pattern = rf"\.{re.escape(class_name)}\s*\{{"
    if re.search(pattern, html):
        return html
    style_block = (
        "<style type=\"text/tailwindcss\">\n"
        "@layer utilities {\n"
        f"  .{class_name} {{ {css_body} }}\n"
        "}\n"
        "</style>"
    )
    if "</head>" in html.lower():
        return re.sub(r"(</head>)", style_block + "\n\\1", html, count=1, flags=re.IGNORECASE)
    return html + "\n" + style_block

def _ensure_missing_color_utilities(html: str) -> str:
    if not html:
        return html

    def _extract_defined_classes(content: str) -> set[str]:
        defined = set()
        for style in re.findall(
            r"<style[^>]*type=[\"']text/tailwindcss[\"'][^>]*>(.*?)</style>",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            for m in re.finditer(r"\.([a-zA-Z0-9_\\\-\[\]\.]+)\s*\{", style):
                name = m.group(1)
                defined.add(name)
                defined.add(name.replace("\\", ""))
        return defined

    defined_classes = _extract_defined_classes(html)
    matches = re.findall(r"\b(text|bg|border)-custom-([0-9a-fA-F]{3,8})\b", html)
    if not matches:
        return html

    def _hex_to_color(hex_value: str) -> str:
        hex_value = hex_value.lower()
        if len(hex_value) == 3:
            hex_value = "".join([c * 2 for c in hex_value])
        if len(hex_value) == 6:
            return f"#{hex_value}"
        if len(hex_value) == 8:
            r = int(hex_value[0:2], 16)
            g = int(hex_value[2:4], 16)
            b = int(hex_value[4:6], 16)
            a = int(hex_value[6:8], 16) / 255
            return f"rgba({r}, {g}, {b}, {a:.3f})".rstrip("0").rstrip(".")
        return f"#{hex_value}"

    rules = []
    seen = set()
    for kind, hex_value in matches:
        class_name = f"{kind}-custom-{hex_value}"
        if class_name in defined_classes or class_name in seen:
            continue
        seen.add(class_name)
        color_value = _hex_to_color(hex_value)
        prop = "color" if kind == "text" else "background-color" if kind == "bg" else "border-color"
        rules.append(f"  .{class_name} {{ {prop}: {color_value}; }}")

    if not rules:
        return html

    style_block = (
        "<style type=\"text/tailwindcss\">\n"
        "@layer utilities {\n"
        + "\n".join(rules) +
        "\n}\n</style>"
    )

    if "</head>" in html.lower():
        return re.sub(r"(</head>)", style_block + "\n\\1", html, count=1, flags=re.IGNORECASE)
    return html + "\n" + style_block

def extract_files(js_text):
    files = {}
    matches = re.split(r"FILE:\s*", js_text)   # Split by 'FILE: ' marker

    for block in matches:
        if not block.strip():
            continue
        
        lines = block.split("\n", 1)
        if len(lines) < 2:
            continue
            
        path = lines[0].strip()
        content = lines[1].strip()
        files[path] = content

    return files


def _extract_text(response):
    if hasattr(response, "text") and response.text:
        return response.text

    if hasattr(response, "candidates"):
        for c in response.candidates:
            if hasattr(c, "content"):
                for p in c.content.parts:
                    if hasattr(p, "text") and p.text:
                        return p.text
    return None

def _collect_fonts_from_layout(layout):
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

    if isinstance(layout, dict):
        if "pages" in layout:
            for page in layout.get("pages") or []:
                for screen in page.get("screens") or []:
                    for node in screen.get("tree") or []:
                        _walk(node)
        elif "tree" in layout:
            for node in layout.get("tree") or []:
                _walk(node)

    return families

def _build_google_fonts_link(families):
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
    return (
        "<link rel=\"stylesheet\" "
        f"href=\"https://fonts.googleapis.com/css2?{'&'.join(parts)}&display=swap\">"
    )

def _normalize_font_classes(html: str, families: dict) -> str:
    if not html or not families:
        return html
    slug_map = {}
    for family in families.keys():
        slug = re.sub(r"[^a-z0-9]", "", family.lower())
        if slug:
            slug_map[slug] = family

    def _font_slug(raw: str) -> str:
        return re.sub(r"[^a-z0-9]", "", raw.replace("_", " ").lower())

    def _replace_bracketed(match):
        raw = match.group(1)
        slug = _font_slug(raw)
        if slug in slug_map:
            return f"font-{slug}"
        return match.group(0)

    html = re.sub(r"font-\['([^']+)'\]", _replace_bracketed, html)
    html = re.sub(r"font-\[\"([^\"]+)\"\]", _replace_bracketed, html)
    # Normalize hyphenated family classes like font-work-sans -> font-worksans
    for slug in slug_map.keys():
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", slug)
        hyphen = re.sub(r"\\s+", "-", spaced)
        if hyphen and hyphen != slug:
            html = re.sub(rf"font-{re.escape(hyphen)}\\b", f"font-{slug}", html)
    return html

def _ensure_html_document(text: str) -> str:
    if not text:
        return text
    lower = text.lower()
    # Only attempt wrapping if it looks like HTML
    if "<" not in text or ">" not in text:
        return text

    if "<html" in lower:
        return text

    tailwind_script = '<script src="https://cdn.tailwindcss.com"></script>'
    has_tailwind = "cdn.tailwindcss.com" in lower

    # Extract Tailwind style blocks to move into head if we build one
    styles = re.findall(
        r"<style[^>]*type=[\"']text/tailwindcss[\"'][^>]*>.*?</style>",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text_wo_styles = re.sub(
        r"<style[^>]*type=[\"']text/tailwindcss[\"'][^>]*>.*?</style>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()

    def _inject_head(head_open: str) -> str:
        inject = []
        if not has_tailwind:
            inject.append(tailwind_script)
        inject.extend(styles)
        if not inject:
            return head_open
        return head_open + "\n    " + "\n    ".join(inject)

    if "<head" in lower or "<body" in lower:
        doc = text_wo_styles
        if "<head" in lower:
            doc = re.sub(r"(<head[^>]*>)", lambda m: _inject_head(m.group(1)), doc, count=1, flags=re.IGNORECASE)
        else:
            head_parts = [
                "<head>",
                '    <meta charset="UTF-8">',
                '    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
            ]
            if not has_tailwind:
                head_parts.append(f"    {tailwind_script}")
            head_parts.extend([f"    {s}" for s in styles])
            head_parts.append("</head>")
            doc = "\n".join(head_parts) + "\n" + doc

        if "<html" not in doc.lower():
            doc = "<html lang=\"en\">\n" + doc + "\n</html>"
        if "<!doctype" not in doc.lower():
            doc = "<!DOCTYPE html>\n" + doc
        return doc

    # Plain fragment: wrap in full document
    head = [
        "<head>",
        '    <meta charset="UTF-8">',
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        "    <title>Figma to HTML</title>",
    ]
    if not has_tailwind:
        head.append(f"    {tailwind_script}")
    head.extend([f"    {s}" for s in styles])
    head.append("</head>")

    body = "<body>\n" + text_wo_styles + "\n</body>"
    return "<!DOCTYPE html>\n<html lang=\"en\">\n" + "\n".join(head) + "\n" + body + "\n</html>"

def _fix_apply_font(html: str) -> str:
    if not html:
        return html

    def _replace_apply(match):
        raw = match.group(1).replace("_", " ")
        return f"font-family: '{raw}', sans-serif;"

    html = re.sub(r"@apply\s+font-\['([^']+)'\];", _replace_apply, html)
    html = re.sub(r"@apply\s+font-\[\"([^\"]+)\"\];", _replace_apply, html)
    html = re.sub(r"@apply\s+font-([a-z0-9_-]+);", r"font-family: '\1', sans-serif;", html)
    return html

def _collect_image_meta(layout: dict):
    scale_map = {}
    radius_map = {}
    size_map = {}
    screen_map = {}

    def _walk(node, screen_box):
        if not node:
            return
        style = node.get("style") or {}
        url = style.get("imageUrl")
        if url:
            box = node.get("box") or {}
            w = box.get("w")
            h = box.get("h")
            area = (w or 0) * (h or 0)
            prev_area = size_map.get(url, {}).get("area", -1)
            if area >= prev_area:
                scale_map[url] = style.get("imageScale")
                if style.get("radius") is not None:
                    radius_map[url] = style.get("radius")
                size_map[url] = {"w": w, "h": h, "area": area}
                if screen_box:
                    screen_map[url] = {"w": screen_box.get("w"), "h": screen_box.get("h")}
        for child in node.get("children") or []:
            _walk(child, screen_box)

    if isinstance(layout, dict):
        if "pages" in layout:
            for page in layout.get("pages") or []:
                for screen in page.get("screens") or []:
                    screen_box = screen.get("box") or {}
                    for node in screen.get("tree") or []:
                        _walk(node, screen_box)
        elif "tree" in layout:
            for node in layout.get("tree") or []:
                _walk(node, None)

    return scale_map, radius_map, size_map, screen_map

def _apply_image_meta(html: str, layout: dict) -> str:
    if not html:
        return html
    scale_map, radius_map, size_map, screen_map = _collect_image_meta(layout)
    if not scale_map and not radius_map and not size_map:
        return html

    def _desired_object(scale, src):
        if scale == "FIT":
            return "object-contain"
        if scale == "FILL" or scale == "CROP":
            size = size_map.get(src) or {}
            screen = screen_map.get(src) or {}
            sw = screen.get("w")
            sh = screen.get("h")
            w = size.get("w")
            h = size.get("h")
            # If the image occupies a small portion of the screen, prefer contain to avoid cropping.
            if sw and sh and w and h:
                if (w / sw) < 0.6 and (h / sh) < 0.6:
                    return "object-contain"
            return "object-cover"
        return "object-contain"

    def _replace_img(match):
        full = match.group(0)
        src = match.group(1)
        scale = scale_map.get(src)
        radius = radius_map.get(src)
        desired = _desired_object(scale, src) if scale is not None else "object-contain"

        class_match = re.search(r'class="([^"]*)"', full)
        if class_match:
            classes = class_match.group(1).split()
            classes = [
                c for c in classes
                if c not in {"object-cover", "object-contain", "object-fill", "object-scale-down"}
            ]
            if desired not in classes:
                classes.append(desired)
            if radius is not None:
                radius_px = f"rounded-[{radius}px]"
                if not any(c.startswith("rounded-") for c in classes):
                    classes.append(radius_px)
            new_class = " ".join(classes)
            return re.sub(r'class="[^"]*"', f'class="{new_class}"', full)

        extra = desired
        if radius is not None:
            extra += f" rounded-[{radius}px]"
        return full.replace("<img ", f'<img class="{extra}" ', 1)

    def _unclip_wrappers(match):
        classes = match.group(1)
        src = match.group(2)
        # Only remove overflow-hidden if this image has no radius.
        if radius_map.get(src) is not None:
            return match.group(0)
        classes = re.sub(r"\\boverflow-hidden\\b", "", classes).strip()
        classes = re.sub(r"\\s{2,}", " ", classes)
        return f'<div class="{classes}"><img src="{src}"'

    html = re.sub(
        r"<img[^>]+src=\"([^\"]+)\"[^>]*>",
        _replace_img,
        html,
        flags=re.IGNORECASE,
    )

    html = re.sub(
        r'<div class="([^"]*)">\\s*<img src="([^"]+)"',
        _unclip_wrappers,
        html,
        flags=re.IGNORECASE,
    )

    return html

def _slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words) if words else ""

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())

def _stem(token: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if token.endswith(suf) and len(token) > len(suf) + 2:
            return token[: -len(suf)]
    return token

def _build_route_index(layout: dict):
    routes = []
    if not isinstance(layout, dict):
        return routes
    pages = layout.get("pages") or []
    for page in pages:
        for screen in page.get("screens") or []:
            name = screen.get("screen") or ""
            if not name:
                continue
            filename = _slugify(name) + ".html"
            tokens = [_stem(t) for t in re.findall(r"[a-z0-9]+", name.lower())]
            routes.append(
                {
                    "name": name,
                    "norm": _norm(name),
                    "tokens": set(tokens),
                    "file": filename,
                }
            )
    return routes

def _match_route(label: str, routes: list[dict]) -> str | None:
    if not label:
        return None
    label_norm = _norm(label)
    for r in routes:
        if label_norm == r["norm"]:
            return r["file"]

    label_tokens = [_stem(t) for t in re.findall(r"[a-z0-9]+", label.lower())]
    label_set = set(label_tokens)
    if not label_set:
        return None

    best = (0.0, None)
    for r in routes:
        inter = label_set.intersection(r["tokens"])
        if not inter:
            continue
        score = len(inter) / max(len(r["tokens"]), 1)
        if score > best[0]:
            best = (score, r["file"])

    if best[0] >= 0.5:
        return best[1]
    return None

def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")

def _collapse_ws(text: str) -> str:
    return " ".join((text or "").split())

def _convert_nav_to_links(html: str, layout: dict) -> str:
    if not html:
        return html
    routes = _build_route_index(layout)
    if not routes:
        return html

    def _with_anchor_classes(attrs: str) -> str:
        class_match = re.search(r'\sclass="([^"]*)"', attrs)
        extras = ["no-underline", "text-inherit"]
        display_re = re.compile(r"\b(inline-flex|flex|inline-block|block|grid|inline-grid)\b")
        display_fallback = "inline-flex"
        if class_match:
            classes = class_match.group(1)
            for extra in extras:
                if extra not in classes:
                    classes += " " + extra
            if not display_re.search(classes):
                classes += " " + display_fallback
            return re.sub(r'\sclass="[^"]*"', f' class="{classes}"', attrs, count=1)
        return f'{attrs} class="{" ".join(extras + [display_fallback])}"'

    def _protect_anchors(html_in: str):
        anchors = []

        def _stash(match):
            anchors.append(match.group(0))
            return f"__ANCHOR_{len(anchors) - 1}__"

        protected = re.sub(r"<a\b[^>]*>.*?</a>", _stash, html_in, flags=re.DOTALL | re.IGNORECASE)
        return protected, anchors

    def _restore_anchors(html_in: str, anchors: list[str]) -> str:
        restored = html_in
        for idx, anchor in enumerate(anchors):
            restored = restored.replace(f"__ANCHOR_{idx}__", anchor)
        return restored

    def _convert_tag(tag: str, html_in: str, allow_nested_div: bool = True) -> str:
        pattern = rf"<{tag}([^>]*)>(.*?)</{tag}>"

        def _replace(match):
            attrs = match.group(1) or ""
            inner = match.group(2) or ""
            inner_lower = inner.lower()
            if not allow_nested_div and "<div" in inner_lower:
                return match.group(0)
            if "<a" in inner_lower:
                return match.group(0)
            text = _collapse_ws(_strip_tags(inner))
            href = _match_route(text, routes)
            if not href:
                return match.group(0)
            attrs = re.sub(r'\s*type="[^"]*"', "", attrs)
            attrs = re.sub(r'\s*href="[^"]*"', "", attrs)
            attrs = _with_anchor_classes(attrs)
            return f'<a{attrs} href="{href}">{inner}</a>'

        return re.sub(pattern, _replace, html_in, flags=re.DOTALL | re.IGNORECASE)

    html = _convert_tag("button", html)
    html = _convert_tag("div", html, allow_nested_div=False)
    protected_html, anchors = _protect_anchors(html)
    protected_html = _convert_tag("p", protected_html)
    return _restore_anchors(protected_html, anchors)

def _build_font_utilities(families):
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
    return "<style type=\"text/tailwindcss\">\n" + base + utilities + "</style>"

def _inject_google_fonts(html: str, layout: dict) -> str:
    if not html:
        return html
    families = _collect_fonts_from_layout(layout)
    link = _build_google_fonts_link(families)
    if link and "fonts.googleapis.com/css2" not in html:
        html = re.sub(
            r"(<head[^>]*>)",
            lambda m: m.group(1) + "\n    " + link,
            html,
            count=1,
            flags=re.IGNORECASE,
        )

    font_utils = _build_font_utilities(families)
    if font_utils and not re.search(r"\.font-[a-z0-9]", html):
        html = re.sub(
            r"(</head>)",
            lambda m: font_utils + "\n" + m.group(1),
            html,
            count=1,
            flags=re.IGNORECASE,
        )

    html = _normalize_font_classes(html, families)
    html = _fix_apply_font(html)
    return html

# ---------- HTML GENERATOR  ----------
def _html_prompt(layout):
    return f"""
YOU MUST OUTPUT ONLY RAW HTML.
DO NOT WRITE MARKDOWN.
DO NOT WRITE PYTHON.
DO NOT WRITE EXPLANATIONS.
DO NOT USE CODE FENCES.

You are a senior frontend engineer building a production website from a Figma scene graph.

Your job is to reconstruct the same UI and match the Figma design as closely as possible.

RULES:
1. Preserve structure and hierarchy exactly.
2. Preserve visual intent (layout, spacing, grouping, typography).
3. If a node has auto-layout info (layout.dir), use flex layout to match it.
4. If a node has no auto-layout info, use absolute positioning with box.x/box.y
   and explicit width/height from box.w/box.h to match Figma coordinates. In this
   case, ensure the parent is position: relative and children are absolute.
4.1 ROOT FRAME NORMALIZATION (STRICT):
    For any top-level frame/section/header/footer that has a background color or image,
    the OUTER element must be full width (w-full) and must NOT have max-w or mx-auto.
    Place all its contents inside an INNER container with
    max-w-[<frame width>px] mx-auto w-full (and any padding).
    Only use max-w/mx-auto on the inner container, never on the background element itself.
5. Use Tailwind utility classes for all layout.
6. For values not available in default Tailwind (colors, letter-spacing, font sizes, line-heights):
   -> create custom utilities using <style type="text/tailwindcss"> and @layer.
7. Do NOT use inline style="" attributes.
8. Do NOT use plain <style> (only <style type="text/tailwindcss"> is allowed).
9. Use semantic tags: header, nav, main, section, footer, article, button.
10. Desktop must match Figma. Add responsive adjustments only if they do not
    change the desktop layout, sizes, or spacing.
11. Keep code clean and readable.
12. Text content must be verbatim from the input. Do NOT paraphrase or change punctuation.
13. If you use a custom class (e.g., bg-custom-xxxxxx), you MUST define it in the
    custom utilities block. Do NOT generate undefined custom classes.
13.1 SIZING FIDELITY (CRITICAL):
    Respect node.constraints when present:
    - constraints.hugX == "HUG": do NOT set w-full or flex-1; size by content + padding.
    - constraints.hugX == "FILL" or constraints.grow > 0: use flex-1 or w-full.
    - constraints.hugX == "FIXED": set w-[box.w].
    Apply the same logic for hugY with h-[box.h] vs content height.
14. FONT FIDELITY (CRITICAL):
    Every text node MUST include a font-family class derived from style.family.
    Do not default to Times/serif. If the layout uses a single font family across
    the screen, apply that font to the body as well.
14.1 TEXT SIZE FIDELITY (CRITICAL):
    Use exact font sizes, line-heights, and letter-spacing from the layout.
    If a value is not in Tailwind's default scale, use arbitrary values like
    text-[80px], leading-[80px], tracking-[-6.4px]. Do NOT approximate.
15. IMAGE RADIUS (CRITICAL):
    If a node has style.radius and contains an image (style.image == true),
    apply the radius and `overflow-hidden` on the image wrapper, and apply the
    same rounded class to the <img>.
15.1 CLIPPING (CRITICAL):
    If style.clips == true, add overflow-hidden and also add class "clip-true".
    If style.clips is false or missing, do NOT add overflow-hidden just because of radius.

IMAGE RULE (CRITICAL):
If style.image == true AND style.imageUrl exists ->
<img src="{{style.imageUrl}}" />

If style.image == true but style.imageUrl is missing ->
<img src="https://placehold.co/600x400?text=Image" />

NEVER embed base64/data: URIs or inline SVG in src or CSS.
If no imageUrl is provided, use the placeholder above.

IMAGE ASPECT RATIO (IMPORTANT):
If style.imageScale is "FILL" -> use `object-cover`.
If style.imageScale is "FIT" -> use `object-contain`.
If unknown, use `object-contain`.
Prefer setting the size on a parent wrapper and use `w-full h-full` on the <img>.

OUTPUT:
ONE COMPLETE HTML DOCUMENT.

Must include:
<script src="https://cdn.tailwindcss.com"></script>
<style type="text/tailwindcss"> with custom utilities if needed.

INPUT:
{layout}
"""

def _html_continue_prompt(layout, tail):
    return f"""
YOU MUST OUTPUT ONLY RAW HTML.
DO NOT WRITE MARKDOWN.
DO NOT WRITE PYTHON.
DO NOT WRITE EXPLANATIONS.
DO NOT USE CODE FENCES.

You are continuing an HTML document that was truncated.
Return ONLY the remaining HTML starting immediately after the snippet below.
Do NOT repeat any earlier content. Do NOT restart <html>, <head>, or <body>.
Only output the remaining raw HTML until the document is complete.

SNIPPET (end of previous output):
{tail}

FIGMA LAYOUT (for reference):
{layout}
"""

# ---------- REACT GENERATOR ----------
def _react_prompt(layout):
    return f"""
YOU ARE A REACT UI CODE GENERATOR.

YOU MUST NOT generate:
- package.json
- vite.config.js
- tailwind.config.js
- postcss.config.js
- index.html
- src/main.jsx
- src/index.css

YOU MUST ONLY generate:
- src/App.jsx
- src/components/**
- src/pages/**

====================
OUTPUT FORMAT
====================

Every file MUST be written like this:

FILE: <path>
<file contents>

Example:
FILE: src/App.jsx
export default function App() {{
  return <div>Hello</div>
}}

Rules:
- Every file MUST start with "FILE: "
- The path must be exact
- File contents must NOT be escaped
- Do NOT output JSON
- Do NOT output markdown
- Do NOT explain anything
- Do NOT write anything before or after the FILE blocks

====================
UI RULES
====================

- Use React 18
- Use functional components
- Use Tailwind for all styling
- Root component must be src/App.jsx
- App must use min-h-screen
- Do NOT use h-screen on root
- Do NOT use overflow-hidden on root
- If a node has auto-layout info (layout.dir), use flex layout to match it.
- If a node has no auto-layout info, use absolute positioning with box.x/box.y
  and explicit width/height from box.w/box.h to match Figma coordinates. In this
  case, ensure the parent is position: relative and children are absolute.
- Text content must be verbatim from the input. Do NOT paraphrase or change punctuation.
- Desktop must match Figma. Add responsive adjustments only if they do not
  change the desktop layout, sizes, or spacing.
- If the layout contains multiple pages/screens, generate a page component
  for each screen under src/pages/ and set up routing in src/App.jsx using
  react-router-dom (BrowserRouter, Routes, Route). The first screen should
  be the default "/" route.
- SIZING FIDELITY (CRITICAL):
  Respect node.constraints when present:
  - constraints.hugX == "HUG": do NOT set w-full or flex-1; size by content + padding.
  - constraints.hugX == "FILL" or constraints.grow > 0: use flex-1 or w-full.
  - constraints.hugX == "FIXED": set w-[box.w].
  Apply the same logic for hugY with h-[box.h] vs content height.
- FONT FIDELITY (CRITICAL):
  Every text node MUST include a font-family class derived from style.family.
  Do not default to Times/serif. If the layout uses a single font family across
  the screen, apply that font to a wrapping container as well.
- TEXT SIZE FIDELITY (CRITICAL):
  Use exact font sizes, line-heights, and letter-spacing from the layout.
  If a value is not in Tailwind's default scale, use arbitrary values like
  text-[80px], leading-[80px], tracking-[-6.4px]. Do NOT approximate.
- IMAGE RADIUS (CRITICAL):
  If a node has style.radius and contains an image (style.image == true),
  apply the radius and `overflow-hidden` on the image wrapper, and apply the
  same rounded class to the <img>.
- CLIPPING (CRITICAL):
  If style.clips == true, add overflow-hidden and also add class "clip-true".
  If style.clips is false or missing, do NOT add overflow-hidden just because of radius.

IMAGE RULE (CRITICAL):
If style.image == true AND style.imageUrl exists ->
<img src="{{style.imageUrl}}" />

If style.image == true but style.imageUrl is missing ->
<img src="https://placehold.co/600x400?text=Image" />

If style.imageScale is "FILL" -> use `object-cover`.
If style.imageScale is "FIT" -> use `object-contain`.
If unknown, use `object-contain`.
Prefer setting the size on a parent wrapper and use `w-full h-full` on the <img>.

Use free public CDNs for logos/icons if no imageUrl is provided:
jsDelivr, Unpkg, Icons8, SimpleIcons.

Never generate image files.
Never embed base64/data: URIs or inline SVG.

Fonts:
- Use fontFamily from the Figma layout

CRITICAL RULE (NO EXCEPTIONS):
If you use a component in JSX like <Xyz /> you MUST:
1. Generate a file for it under src/components/Xyz.jsx
2. Import it at the top of the file using it

Undefined components are forbidden.
Missing imports are forbidden.


====================
FIGMA LAYOUT
====================
{layout}
"""


def generate_code(layout: dict, framework: str, route_layout: dict | None = None) -> str:
    # Compact layout to reduce prompt tokens without losing any values.
    layout_payload = layout
    if not isinstance(layout_payload, str):
        layout_payload = json.dumps(layout_payload, separators=(",", ":"))

    if framework == "react":
        prompt = _react_prompt(layout_payload)
    else:
        prompt = _html_prompt(layout_payload)

    retries = 5
    delay = 3

    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"max_output_tokens": 16384}
            )

            text = _extract_text(response)

            if not text or not text.strip():
                raise ValueError("Empty model response")

            text = text.strip().replace("```", "")
            text = _fix_mojibake(text)

            # ---------- HTML MODE ----------
            if framework != "react":
                if "<html" not in text.lower():
                    text = _ensure_html_document(text)
                if "<html" not in text.lower():
                    raise Exception("AI did not return HTML")
                if "</html>" in text.lower():
                    routing_layout = route_layout or layout
                    html = _sanitize_html_output(text)
                    html = _ensure_missing_color_utilities(html)
                    html = _apply_image_meta(_inject_google_fonts(html, layout), layout)
                    html = _ensure_custom_class(html, "text-custom-ffffff", "color: #ffffff;")
                    html = _convert_nav_to_links(html, routing_layout)
                    return html

                # Attempt continuation if truncated
                partial = text
                for _ in range(8):
                    tail = partial[-2000:]
                    try:
                        cont = client.models.generate_content(
                            model="gemini-2.5-flash",
                            contents=_html_continue_prompt(layout_payload, tail),
                            config={"max_output_tokens": 16384}
                        )
                    except Exception:
                        break
                    cont_text = _extract_text(cont)
                    if not cont_text:
                        break
                    cont_text = _fix_mojibake(cont_text.strip().replace("```", ""))
                    # Avoid repeating the tail if the model echoes it back
                    if cont_text and cont_text in partial[-3000:]:
                        cont_text = cont_text.replace(tail, "")
                    partial += cont_text
                    if "</html>" in partial.lower():
                        routing_layout = route_layout or layout
                        html = _sanitize_html_output(partial)
                        html = _ensure_missing_color_utilities(html)
                        html = _apply_image_meta(_inject_google_fonts(html, layout), layout)
                        html = _ensure_custom_class(html, "text-custom-ffffff", "color: #ffffff;")
                        html = _convert_nav_to_links(html, routing_layout)
                        return html

                raise Exception("AI output truncated before </html>")

            # ---------- REACT MODE ----------
            else:
                # Parse FILE: format
                files = extract_files(text)
                
                if not files:
                    raise Exception("No files found in AI output")
                
                # if "package.json" not in files:
                    # raise Exception("AI did not generate package.json")

                return files

        except Exception as e:
            msg = str(e)
            if (
                "503" in msg
                or "UNAVAILABLE" in msg
                or "ReadError" in msg
                or "WinError 10053" in msg
            ):
                print(f"[AI] Network/overload issue, retrying in {delay}s... ({attempt+1}/{retries})")
                time.sleep(delay)
                delay *= 2
                continue
            raise

    raise RuntimeError("AI failed after multiple retries")
