"""Build api_catalog.json, the compact endpoint reference the MCP server ships.

PrintStash serves its OpenAPI document only from the API process itself
(/openapi.json), which the bundled nginx does not expose, so the server cannot
fetch it at run time. Regenerate the catalog when PrintStash changes its API:

    curl -s http://<api-host>:8000/openapi.json -o openapi.json
    python tools/make_catalog.py openapi.json api_catalog.json
"""

from __future__ import annotations

import json
import sys

METHODS = ("get", "post", "put", "patch", "delete")


def type_of(schema: dict, schemas: dict, depth: int = 0) -> object:
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        target = schemas.get(name, {})
        if "enum" in target:
            return {"enum": target["enum"]}
        if depth < 1 and target.get("type") == "object" and target.get("properties"):
            return fields(target, schemas, depth + 1)
        return name
    if "anyOf" in schema:
        parts = [type_of(s, schemas, depth) for s in schema["anyOf"] if s.get("type") != "null"]
        return parts[0] if len(parts) == 1 else parts
    if "enum" in schema:
        return {"enum": schema["enum"]}
    if schema.get("type") == "array":
        return [type_of(schema.get("items", {}), schemas, depth)]
    return schema.get("type", "any")


def fields(schema: dict, schemas: dict, depth: int = 0) -> dict:
    required = set(schema.get("required", []))
    out = {}
    for name, prop in schema.get("properties", {}).items():
        key = name + ("*" if name in required else "")
        out[key] = type_of(prop, schemas, depth)
    return out


def main(src: str, dst: str) -> None:
    doc = json.load(open(src))
    schemas = doc["components"]["schemas"]
    endpoints = []
    for path, ops in sorted(doc["paths"].items()):
        for method in METHODS:
            op = ops.get(method)
            if not op:
                continue
            entry = {
                "method": method.upper(),
                "path": path.removeprefix("/api/v1"),
                "summary": op.get("summary", ""),
                "tag": (op.get("tags") or [""])[0],
            }
            params = {}
            for p in op.get("parameters", []):
                if p["in"] == "header":
                    continue
                key = f"{p['in']}:{p['name']}" + ("*" if p.get("required") else "")
                params[key] = type_of(p.get("schema", {}), schemas)
            if params:
                entry["params"] = params
            for ctype, content in op.get("requestBody", {}).get("content", {}).items():
                body_schema = content.get("schema", {})
                if "$ref" in body_schema:
                    body_schema = schemas.get(body_schema["$ref"].split("/")[-1], {})
                entry["body"] = {"content_type": ctype, "fields": fields(body_schema, schemas)}
                break
            endpoints.append(entry)
    info = doc.get("info", {})
    json.dump(
        {"printstash_version": info.get("version"), "endpoints": endpoints},
        open(dst, "w"),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    print(f"{len(endpoints)} endpoints -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
