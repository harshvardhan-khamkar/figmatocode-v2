def is_component_like(node):
    name = (node.get("name") or "").lower()
    t = node.get("type")

    if t in ["COMPONENT", "INSTANCE"]:
        return True

    if any(k in name for k in ["nav", "header", "footer", "hero", "card", "section", "list", "item", "button", "modal"]):
        return True

    if t == "FRAME" and len(node.get("children", [])) > 2:
        return True

    return False

# ===============================
# COLOR
# ===============================

def color_to_hex(c):
    if not c or not isinstance(c, dict):
        return None
    try:
        return "#{:02x}{:02x}{:02x}".format(
            int(c.get("r", 0) * 255),
            int(c.get("g", 0) * 255),
            int(c.get("b", 0) * 255)
        )
    except Exception:
        return None


# ===============================
# FILLS (backgrounds, images)
# ===============================

def parse_fills(node, fills):
    if not fills or not isinstance(fills, list):
        fills = []

    for f in fills:
        if not f or f.get("visible") is False:
            continue

        if f.get("type") == "SOLID":
            return {
                "bg": color_to_hex(f.get("color")),
                "opacity": f.get("opacity", 1)
            }

        if f.get("type") in ["IMAGE", "VIDEO"]:
            return {
                "image": True,
                "imageRef": f.get("imageRef")
            }

    # Fallback to background fills (frames)
    for bg in node.get("background") or []:
        if not bg or bg.get("visible") is False:
            continue
        if bg.get("type") == "SOLID":
            return {
                "bg": color_to_hex(bg.get("color")),
                "opacity": bg.get("opacity", 1)
            }
        if bg.get("type") in ["IMAGE", "VIDEO"]:
            return {
                "image": True,
                "imageRef": bg.get("imageRef")
            }

    return {}


# ===============================
# STROKES (borders)
# ===============================

def parse_strokes(node):
    strokes = node.get("strokes") or []
    if not strokes or not isinstance(strokes, list):
        return None

    s = strokes[0]
    if not isinstance(s, dict):
        return None

    return {
        "color": color_to_hex(s.get("color")),
        "width": node.get("strokeWeight", 0),
        "align": node.get("strokeAlign")
    }


# ===============================
# EFFECTS (shadow, blur)
# ===============================

def parse_effects(effects):
    if not effects or not isinstance(effects, list):
        return None

    out = {}

    for e in effects:
        if not e or e.get("visible") is False:
            continue

        if e.get("type") == "DROP_SHADOW":
            out["shadow"] = {
                "x": e.get("offset", {}).get("x", 0),
                "y": e.get("offset", {}).get("y", 0),
                "blur": e.get("radius", 0),
                "color": color_to_hex(e.get("color"))
            }

        if e.get("type") == "LAYER_BLUR":
            out["blur"] = e.get("radius")

    return out or None


# ===============================
# CONSTRAINTS & AUTO-LAYOUT
# ===============================

def parse_constraints(node):
    c = node.get("constraints") or {}
    return {
        "h": c.get("horizontal"),
        "v": c.get("vertical"),
        "grow": node.get("layoutGrow", 0),
        "align": node.get("layoutAlign"),
        "hugX": node.get("layoutSizingHorizontal"),
        "hugY": node.get("layoutSizingVertical")
    }


# ===============================
# NODE EXTRACTION
# ===============================

def extract_node(node, parent_x=0, parent_y=0):
    if not node or not isinstance(node, dict):
        return None

    bb = node.get("absoluteBoundingBox") or {}
    abs_x = bb.get("x", 0)
    abs_y = bb.get("y", 0)

    rel_x = round(abs_x - parent_x)
    rel_y = round(abs_y - parent_y)

    out = {
        "id": node.get("id"),
        "type": node.get("type"),
        "name": node.get("name"),
        "isComponent": False,
        "componentName": None,
        "box": {
            "w": round(bb.get("width", 0)),
            "h": round(bb.get("height", 0)),
            "x": rel_x,
            "y": rel_y
        },
        "layout": {},
        "style": {},
        "constraints": parse_constraints(node),
        "text": None,
        "children": []
    }

    # ===============================
    # AUTO-LAYOUT
    # ===============================

    # Only treat nodes with explicit auto-layout as flex containers.
    # Non-auto-layout frames should preserve absolute positions.
    layout_mode = node.get("layoutMode")
    if layout_mode in {"HORIZONTAL", "VERTICAL"}:
        out["layout"] = {
            "dir": layout_mode,
            "gap": node.get("itemSpacing", 0),
            "padding": {
                "t": node.get("paddingTop", 0),
                "b": node.get("paddingBottom", 0),
                "l": node.get("paddingLeft", 0),
                "r": node.get("paddingRight", 0)
            },
            "align": node.get("primaryAxisAlignItems"),
            "cross": node.get("counterAxisAlignItems"),
            "wrap": node.get("layoutWrap")
        }

    # ===============================
    # FILLS
    # ===============================

    out["style"].update(parse_fills(node, node.get("fills")))


    # ===============================
    # BORDERS
    # ===============================

    border = parse_strokes(node)
    if border and border.get("width", 0) > 0:
        name = (node.get("name") or "").lower()
        node_type = node.get("type")

        if any(k in name for k in ["button", "btn", "input", "card", "badge", "pill"]) or node_type == "LINE":
            out["style"]["border"] = border

    # ===============================
    # RADIUS
    # ===============================

    if node.get("cornerRadius") is not None:
        out["style"]["radius"] = node.get("cornerRadius")

    # ===============================
    # EFFECTS
    # ===============================

    effects = parse_effects(node.get("effects"))
    if effects:
        out["style"]["effects"] = effects

    # ===============================
    # TEXT (SAFE)
    # ===============================

    if node.get("type") == "TEXT":
        s = node.get("style") or {}
        fills = node.get("fills") or []

        color = None
        if fills and isinstance(fills[0], dict) and fills[0].get("color"):
            color = color_to_hex(fills[0].get("color"))

        font_size = s.get("fontSize")
        letter_spacing = s.get("letterSpacing")
        if isinstance(letter_spacing, dict):
            ls_value = letter_spacing.get("value")
            if letter_spacing.get("unit") == "PERCENT" and font_size:
                letter_spacing = (font_size * ls_value) / 100
            else:
                letter_spacing = ls_value

        out["text"] = node.get("characters")
        out["style"].update({
            "size": font_size,
            "weight": s.get("fontWeight"),
            "family": s.get("fontFamily"),
            "align": s.get("textAlignHorizontal"),
            "line": s.get("lineHeightPx"),
            "letterSpacing": letter_spacing,
            "color": color
        })

    # ===============================
    # VECTOR DETECTION
    # ===============================

    if node.get("type") == "VECTOR":
        out["style"]["vector"] = True

    # ===============================
    # CHILDREN
    # ===============================

    for child in node.get("children") or []:
        c = extract_node(child, abs_x, abs_y)
        if c:
            out["children"].append(c)

    # ===============================
    # REACT COMPONENT DETECTION
    # ===============================

    if is_component_like(out):
        clean = (out.get("name") or "Component").replace(" ", "").replace("-", "")
        out["isComponent"] = True
        out["componentName"] = clean

    return out


# ===============================
# DOCUMENT PARSER
# ===============================

def parse_figma_layout(figma_json):
    print("LOADED NEW layout_parser.py")
    pages = []
    document = figma_json.get("document") or {}

    for page in document.get("children") or []:
        if not page:
            continue

        screens = []

        for screen in page.get("children") or []:
            if not screen or screen.get("type") != "FRAME":
                continue

            bb = screen.get("absoluteBoundingBox") or {}
            root_x = bb.get("x", 0)
            root_y = bb.get("y", 0)

            screens.append({
                "screen": screen.get("name"),
                "box": {
                    "w": round(bb.get("width", 0)),
                    "h": round(bb.get("height", 0)),
                    "x": round(root_x),
                    "y": round(root_y)
                },
                "tree": [
                    extract_node(child, root_x, root_y)
                    for child in (screen.get("children") or [])
                    if child
                ]
            })

        pages.append({
            "page": page.get("name"),
            "screens": screens
        })

    return {"pages": pages}
