"""JSON Schema (draft 2020-12) per Kancil tool action.

Derived from the runtime tool manifest (Kancil._manifest_cache()) — never
hand-written, so it can't drift from the real params. Used by:
- `kancil tool --schema [action]` (LLM introspection)
- the MCP server (tools/list inputSchema)
- input validation (validate(action, args) -> list of errors)
"""

DIALECT = "https://json-schema.org/draft/2020-12/schema"


def for_action(meta):
    """manifest entry -> JSON Schema dict for the action's params object."""
    props = {}
    required = []
    for name, p in (meta.get("params") or {}).items():
        prop = {"type": p.get("type", "string")}
        if p.get("example") is not None:
            prop["examples"] = [p["example"]]
        if p.get("default") is not None:
            try:
                # pastikan JSON-serializable
                import json as _j
                _j.dumps(p["default"])
                prop["default"] = p["default"]
            except Exception:
                pass
        props[name] = prop
        if p.get("required"):
            required.append(name)
    schema = {
        "$schema": DIALECT,
        "type": "object",
        "properties": props,
        "additionalProperties": True,
    }
    if required:
        schema["required"] = sorted(required)
    if meta.get("description"):
        schema["description"] = meta["description"]
    return schema


def for_all(manifest):
    """{action: schema} untuk seluruh manifest."""
    return {a: for_action(m) for a, m in sorted(manifest.items())}


def validate(action_meta, args):
    """Validasi ringan args terhadap schema. Returns [error, ...]."""
    errs = []
    if not isinstance(args, dict):
        return ["args must be an object"]
    schema = for_action(action_meta)
    for name in schema.get("required", []):
        if name not in args or args[name] in (None, ""):
            errs.append("missing required param %r" % name)
    type_map = {"string": str, "integer": int, "number": (int, float),
                "boolean": bool, "array": (list, tuple), "object": dict}
    for name, prop in schema["properties"].items():
        if name not in args or args[name] is None:
            continue
        want = type_map.get(prop["type"])
        got = args[name]
        # bool adalah subclass int: cek boolean dulu
        if prop["type"] == "integer" and isinstance(got, bool):
            errs.append("param %r must be integer, got boolean" % name)
        elif want and not isinstance(got, want):
            errs.append("param %r must be %s, got %s"
                        % (name, prop["type"], type(got).__name__))
    return errs
