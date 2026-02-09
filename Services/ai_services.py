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
    - Normalize tracking classes like tracking-[-1_5px] to tracking-n1_5
    - Normalize non-standard spacing classes like gap-30 or pt-15 to bracketed px
    """
    if not html:
        return html

    # Remove non-tailwind <style> blocks
    html = re.sub(
        r"<style(?![^>]*type=[\"']text/tailwindcss[\"'])[^>]*>.*?</style>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    VALID_SPACING_SCALE = {
        "0", "0.5", "1", "1.5", "2", "2.5", "3", "3.5", "4", "5", "6",
        "7", "8", "9", "10", "11", "12", "14", "16", "20", "24", "28",
        "32", "36", "40", "44", "48", "52", "56", "60", "64", "72", "80", "96"
    }

    def _replace_spacing_token(token: str) -> str:
        # Convert spacing tokens not in default scale to bracketed px.
        # Examples: gap-30 -> gap-[30px], pt-15 -> pt-[15px], space-x-27 -> space-x-[27px]
        m = re.match(r"^(gap|p[trblxy]?|m[trblxy]?|space-[xy])-(\d+(?:\.\d+)?)$", token)
        if not m:
            return token
        prefix, value = m.group(1), m.group(2)
        if value in VALID_SPACING_SCALE:
            return token
        return f"{prefix}-[{value}px]"

    def _fix_padding_margin_prefixes(token: str) -> str:
        # Fix invalid tailwind prefixes like p-l-40 -> pl-40
        token = re.sub(r"^(p)-([trblxy])-", r"\\1\\2-", token)
        token = re.sub(r"^(m)-([trblxy])-", r"\\1\\2-", token)
        return token

    # Normalize tracking class tokens in class=""
    def _replace_tracking_token(token: str) -> str:
        # token like tracking-[-1_5px] or tracking-[0_35px]
        m = re.match(r"tracking-\[([+-]?[\d_\.]+)px\]", token)
        if not m:
            return token
        val = m.group(1)
        prefix = "n" if val.startswith("-") else "p"
        cleaned = val.lstrip("+-").replace(".", "_")
        return f"tracking-{prefix}{cleaned}"

    def _replace_class_attr(match):
        classes = match.group(1)
        tokens = classes.split()
        tokens = [
            _fix_padding_margin_prefixes(
                _replace_spacing_token(_replace_tracking_token(t))
            )
            for t in tokens
        ]
        return f'class="{" ".join(tokens)}"'

    html = re.sub(r'class="([^"]+)"', _replace_class_attr, html)

    # Normalize tracking selectors inside CSS
    def _replace_tracking_selector(match):
        val = match.group(1)
        prefix = "n" if val.startswith("-") else "p"
        cleaned = val.lstrip("+-").replace(".", "_")
        return f".tracking-{prefix}{cleaned}"

    html = re.sub(r"\.tracking-\[([+-]?[\d_\.]+)px\]", _replace_tracking_selector, html)

    return html


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
4.1 ROOT FRAME NORMALIZATION:
    Use a full-width outer wrapper (w-full) for each top-level screen/frame so
    backgrounds stretch edge-to-edge. Inside each section, place content in an
    inner container constrained to the design width (max-w-[<frame width>px] mx-auto w-full).
    This avoids side gaps in backgrounds while keeping content from stretching.
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

IMAGE RULE (CRITICAL):
If style.image == true AND style.imageUrl exists ->
<img src="{{style.imageUrl}}" />

If style.image == true but style.imageUrl is missing ->
<img src="https://placehold.co/600x400?text=Image" />

IMAGE ASPECT RATIO (IMPORTANT):
If you set BOTH width and height on an <img>, include `object-contain` to avoid
stretching the image. Prefer setting the size on a parent wrapper and use
`w-full h-full object-contain` on the <img>.

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

Use placeholder images only:
https://placehold.co/600x400?text=Image

Use free public CDNs for logos/icons:
jsDelivr, Unpkg, Icons8, SimpleIcons.

Never reference local assets.
Never generate image files.

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


def generate_code(layout: dict, framework: str) -> str:
    if framework == "react":
        prompt = _react_prompt(layout)
    else:
        prompt = _html_prompt(layout)

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
                    raise Exception("AI did not return HTML")
                if "</html>" in text.lower():
                    return _sanitize_html_output(text)

                # Attempt continuation if truncated
                partial = text
                for _ in range(5):
                    tail = partial[-1500:]
                    cont = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=_html_continue_prompt(layout, tail),
                        config={"max_output_tokens": 16384}
                    )
                    cont_text = _extract_text(cont)
                    if not cont_text:
                        break
                    cont_text = _fix_mojibake(cont_text.strip().replace("```", ""))
                    # Avoid repeating the tail if the model echoes it back
                    if cont_text and cont_text in partial[-3000:]:
                        cont_text = cont_text.replace(tail, "")
                    partial += cont_text
                    if "</html>" in partial.lower():
                        return _sanitize_html_output(partial)

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
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                print(f"[AI] Overloaded, retrying in {delay}s... ({attempt+1}/{retries})")
                time.sleep(delay)
                delay *= 2
                continue
            raise

    raise RuntimeError("AI failed after multiple retries")
