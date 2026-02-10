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
    "POLYGON",
}


def _strip_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, {}, [], "")}


def _normalize_style(style: dict) -> dict:
    if not style:
        return {}
    keep = {
        "bg",
        "opacity",
        "image",
        "imageRef",
        "imageUrl",
        "imageScale",
        "border",
        "radius",
        "effects",
        "size",
        "weight",
        "family",
        "align",
        "line",
        "letterSpacing",
        "color",
        "vector",
    }
    out = {k: style.get(k) for k in keep if k in style}
    return _strip_empty(out)


def _normalize_layout(layout: dict) -> dict:
    if not layout:
        return {}
    keep = {"dir", "gap", "padding", "align", "cross", "wrap"}
    out = {k: layout.get(k) for k in keep if k in layout}
    # Drop empty padding keys
    if "padding" in out and isinstance(out["padding"], dict):
        out["padding"] = _strip_empty(out["padding"])
        if not out["padding"]:
            out.pop("padding", None)
    return _strip_empty(out)


def _normalize_node(node: dict) -> dict:
    if not node:
        return {}

    constraints = node.get("constraints") or {}
    norm_constraints = {}
    for key in ("hugX", "hugY", "grow", "align"):
        if key in constraints and constraints.get(key) not in (None, "", 0):
            norm_constraints[key] = constraints.get(key)

    out = {
        "type": node.get("type"),
        "name": node.get("name"),
        "box": node.get("box"),
        "layout": _normalize_layout(node.get("layout") or {}),
        "style": _normalize_style(node.get("style") or {}),
        "text": node.get("text"),
    }
    if norm_constraints:
        out["constraints"] = norm_constraints

    children = []
    for child in node.get("children") or []:
        c = _normalize_node(child)
        if c:
            children.append(c)
    if children:
        out["children"] = children

    # Ensure image-capable nodes are preserved for image injection
    if out.get("type") in RENDERABLE_IMAGE_TYPES:
        style = out.get("style") or {}
        if style.get("image") or style.get("imageRef") or style.get("imageUrl"):
            out["style"] = style
        out["style"] = out.get("style") or {}

    return _strip_empty(out)


def normalize_layout(layout: dict) -> dict:
    if not layout:
        return {}

    pages_out = []
    for page in layout.get("pages") or []:
        screens_out = []
        for screen in page.get("screens") or []:
            tree = []
            for node in screen.get("tree") or []:
                n = _normalize_node(node)
                if n:
                    tree.append(n)
            screens_out.append(
                {
                    "screen": screen.get("screen"),
                    "box": screen.get("box"),
                    "tree": tree,
                }
            )
        pages_out.append({"page": page.get("page"), "screens": screens_out})

    return {"pages": pages_out}
