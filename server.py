"""MCP server for PrintStash (https://github.com/xiao-villamor/PrintStash).

Gives Claude read and edit access to a self-hosted PrintStash library: search
models, read their files, G-code revisions and print history, look at
thumbnails, see statistics, tag and describe models, mark revisions, log
prints and import models from a URL.

It talks to the PrintStash REST API as one user. It signs in with that user's
API key (POST /api/v1/auth/login), keeps the short-lived access token in memory
and signs in again when the token expires.

Settings (environment variables):
  PRINTSTASH_URL         PrintStash address as seen from this container,
                         e.g. http://printstash:3000
  PRINTSTASH_USERNAME    PrintStash user that owns the API key
  PRINTSTASH_API_KEY     API key from Settings -> Users & Access -> API keys
  PRINTSTASH_PUBLIC_URL  Optional. Address you open in a browser, used to
                         build links to models, e.g. https://printstash.example.com
  MCP_PATH               HTTP path of the MCP endpoint. Make it long and random:
                         it is the only thing protecting the endpoint.
  PORT                   Listening port, 8000 by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Annotated, Any, Literal

import httpx
import uvicorn
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("printstash-mcp")

BASE_URL = os.environ.get("PRINTSTASH_URL", "http://printstash:3000").rstrip("/")
USERNAME = os.environ.get("PRINTSTASH_USERNAME", "")
API_KEY = os.environ.get("PRINTSTASH_API_KEY", "")
PUBLIC_URL = os.environ.get("PRINTSTASH_PUBLIC_URL", "").rstrip("/")
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
PORT = int(os.environ.get("PORT", "8000"))
API = f"{BASE_URL}/api/v1"

# Access tokens live 60 minutes by default; sign in again a little earlier.
TOKEN_TTL_S = 45 * 60

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
ADD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
FETCH = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


class PrintStashError(ToolError):
    """An error PrintStash returned, worded for the model to read (ToolError keeps the text)."""


class Client:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._token: str | None = None
        self._token_at = 0.0
        self._lock = asyncio.Lock()

    async def _login(self) -> str:
        if not USERNAME or not API_KEY:
            raise PrintStashError("PRINTSTASH_USERNAME and PRINTSTASH_API_KEY must be set.")
        r = await self._http.post(
            f"{API}/auth/login", json={"username": USERNAME, "api_key": API_KEY}
        )
        if r.status_code != 200:
            raise PrintStashError(
                f"PrintStash rejected the API key login (HTTP {r.status_code}): {_detail(r)}. "
                "Check PRINTSTASH_USERNAME and PRINTSTASH_API_KEY."
            )
        self._token = r.json()["access_token"]
        self._token_at = time.monotonic()
        return self._token

    async def _get_token(self, force: bool = False) -> str:
        async with self._lock:
            if force or not self._token or time.monotonic() - self._token_at > TOKEN_TTL_S:
                return await self._login()
            return self._token

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        for attempt in range(2):
            token = await self._get_token(force=attempt > 0)
            headers = {"Authorization": f"Bearer {token}", **kw.pop("headers", {})}
            try:
                r = await self._http.request(method, f"{API}{path}", headers=headers, **kw)
            except httpx.HTTPError as e:
                raise PrintStashError(f"Cannot reach PrintStash at {BASE_URL}: {e}") from e
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code >= 400:
                raise PrintStashError(f"PrintStash {method} {path} failed (HTTP {r.status_code}): {_detail(r)}")
            return r
        raise PrintStashError("PrintStash keeps rejecting the access token.")

    async def json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.request(method, path, **kw)
        return r.json() if r.content else None

    async def health(self) -> bool:
        try:
            r = await self._http.get(f"{API}/health", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300] or r.reason_phrase
    if isinstance(body, dict) and "detail" in body:
        detail = body["detail"]
        if body.get("errors"):
            return f"{detail}: " + "; ".join(
                f"{'.'.join(str(x) for x in e.get('loc', []))} {e.get('msg', '')}" for e in body["errors"]
            )
        return detail if isinstance(detail, str) else json.dumps(detail)[:300]
    return json.dumps(body)[:300]


ps = Client()

# ---------------------------------------------------------------------------
# Compact output: PrintStash responses carry editing bookkeeping and many empty
# fields that only cost tokens.
# ---------------------------------------------------------------------------

NOISE = {
    "edit_epoch", "edit_version", "effective_role", "similarity", "derivatives_revision",
    "expected_versions", "succeeded_versions", "mesh_file_id", "volume_measurement",
    "native_context", "identity", "reproducibility", "artifact_evidence",
    "artifact_capture_error", "artifact_capture_error_code", "artifact_capture_error_message",
}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in NOISE:
                continue
            v = _clean(v)
            if v is None or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]  # slicer settings sometimes keep their quotes
    return value


def _link(path: str) -> str | None:
    return f"{PUBLIC_URL}{path}" if PUBLIC_URL else None


def _model_card(m: dict[str, Any]) -> dict[str, Any]:
    card = {
        "id": m.get("id"),
        "name": m.get("name"),
        "collection": m.get("collection_label") or m.get("collection"),
        "collection_path": m.get("collection"),
        "tags": m.get("tags"),
        "files": m.get("file_count"),
        "recommended_revision": m.get("recommended_revision_label"),
        "recommended_revision_status": m.get("recommended_revision_status"),
        "print_summary": m.get("print_summary"),
        "starred": m.get("starred") or None,
        "updated_at": m.get("updated_at"),
        "url": _link(f"/models/{m.get('id')}"),
    }
    return _clean(card)


def _file_summary(f: dict[str, Any]) -> dict[str, Any]:
    out = {
        "file_id": f.get("id"),
        "filename": f.get("original_filename"),
        "type": f.get("file_type"),
        "size_bytes": f.get("size_bytes"),
        "external": f.get("is_external") or None,
        "uploaded_at": f.get("uploaded_at"),
        "tags": f.get("tags"),
        "metadata": f.get("metadata"),
    }
    if f.get("file_type") == "gcode":
        out["revision"] = {
            "number": f.get("gcode_revision_number"),
            "label": f.get("revision_label"),
            "status": f.get("revision_status"),
            "notes": f.get("revision_notes"),
            "recommended": f.get("is_recommended"),
        }
    return _clean(out)


def _model_detail(m: dict[str, Any]) -> dict[str, Any]:
    files = m.get("files") or []
    return _clean(
        {
            "id": m.get("id"),
            "name": m.get("name"),
            "description": m.get("description"),
            "source_url": m.get("source_url"),
            "collection": m.get("collection_label") or m.get("collection"),
            "collection_path": m.get("collection"),
            "tags": m.get("tags"),
            "starred": m.get("starred") or None,
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at"),
            "url": _link(f"/models/{m.get('id')}"),
            "source_files": [_file_summary(f) for f in files if f.get("file_type") != "gcode"],
            "gcode_revisions": [_file_summary(f) for f in files if f.get("file_type") == "gcode"],
        }
    )


def _print_job(j: dict[str, Any]) -> dict[str, Any]:
    return _clean(
        {
            "id": j.get("id"),
            "state": j.get("state"),
            "printer": j.get("printer_name"),
            "printer_id": j.get("printer_id"),
            "file_id": j.get("file_id"),
            "file": j.get("remote_filename"),
            "revision": j.get("revision_label"),
            "revision_number": j.get("gcode_revision_number"),
            "material": j.get("material_type"),
            "source": j.get("source"),
            "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
            "duration_s": j.get("actual_duration_s"),
            "filament_used_g": j.get("filament_used_g"),
            "cost": j.get("filament_cost"),
            "spool": j.get("spool_name"),
            "spool_id": j.get("spool_id"),
            "error": j.get("error"),
        }
    )


def _list_param(values: list[str] | None) -> list[str] | None:
    return [v for v in values if v] or None if values else None


# ---------------------------------------------------------------------------
# Server and tools
# ---------------------------------------------------------------------------

mcp = MCPServer(
    name="PrintStash",
    instructions=(
        "PrintStash is the user's self-hosted library of 3D models (STL, 3MF, STEP...) and "
        "sliced G-code. A Model holds source files and G-code Revisions; each Revision has a "
        "status (known_good, needs_test, failed, archived) and one is recommended. Prints are "
        "logged against a Revision. Collections are folders, written as paths like "
        "'functional/brackets'. Find a model with search_models or list_models first, then use "
        "its id. Edits are applied immediately, so confirm with the user before changing tags, "
        "names, revision status or logging prints they did not describe."
    ),
)


@mcp.tool(annotations=READ)
async def search_models(
    query: Annotated[str, Field(description="Words to look for in model names, files, tags and descriptions")],
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Search the library by text and return matching models with their print summary."""
    data = await ps.json("GET", "/search", params={"q": query, "limit": limit, "types[]": "model"})
    items = []
    for it in data.get("items", []):
        m = it.get("model")
        if m:
            items.append(_model_card(m))
        else:
            items.append(_clean({"type": it.get("subject_type"), "id": it.get("subject_id"), "name": it.get("name")}))
    return {"count": len(items), "models": items, "truncated": bool(data.get("next_cursor")) or None}


@mcp.tool(annotations=READ)
async def list_models(
    collection: Annotated[str | None, Field(description="Collection path, e.g. 'calibration' or 'functional/brackets'")] = None,
    tags: Annotated[list[str] | None, Field(description="Tag slugs; a model must have all of them")] = None,
    name_contains: Annotated[str | None, Field(description="Substring of the model name")] = None,
    file_type: Annotated[list[Literal["stl", "3mf", "gcode", "resin", "obj", "step", "dxf", "glb", "ply", "gltf", "3dm", "fbx"]] | None, Field(description="Only models that have files of these types")] = None,
    material: Annotated[list[str] | None, Field(description="Material types from G-code, e.g. PLA, PETG")] = None,
    revision_status: Annotated[list[Literal["known_good", "needs_test", "failed", "archived"]] | None, Field(description="Only models with G-code revisions in these states")] = None,
    printed: Annotated[bool | None, Field(description="true: printed at least once; false: never printed")] = None,
    print_outcome: Annotated[list[Literal["completed", "failed", "cancelled"]] | None, Field(description="Only models with prints that ended this way")] = None,
    favorites: Annotated[bool, Field(description="Only starred models")] = False,
    sort: Literal["date-desc", "date-asc", "name-asc", "name-desc", "success-desc", "printed-desc", "duration-asc", "filament-asc", "cost-asc"] = "date-desc",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from a previous call, to get the next page")] = None,
) -> dict[str, Any]:
    """List models with filters (collection, tags, material, revision status, print history) and sorting."""
    params: dict[str, Any] = {
        "collection": collection,
        "tag": _list_param(tags),
        "q": name_contains,
        "file_type": file_type,
        "material_type": _list_param(material),
        "revision_status": revision_status,
        "printed": None if printed is None else str(printed).lower(),
        "print_outcome": print_outcome,
        "favorites": "true" if favorites else None,
        "sort": sort,
        "limit": limit,
        "cursor": cursor,
    }
    params = {k: v for k, v in params.items() if v is not None}
    data = await ps.json("GET", "/models/page", params=params)
    items = data.get("items", data if isinstance(data, list) else [])
    return _clean(
        {
            "count": len(items),
            "models": [_model_card(m) for m in items],
            "next_cursor": data.get("next_cursor") if isinstance(data, dict) else None,
        }
    )


@mcp.tool(annotations=READ)
async def get_model(model_id: int) -> dict[str, Any]:
    """Full detail of one model: description, source files with dimensions, and every G-code
    revision with its slicer settings (printer, nozzle, layer height, material, temperatures,
    estimated time and filament), label, status, notes and which one is recommended."""
    return _model_detail(await ps.json("GET", f"/models/{model_id}"))


@mcp.tool(annotations=READ)
async def get_print_history(model_id: int) -> dict[str, Any]:
    """Print history of one model: each print's outcome, printer, revision, dates, duration,
    filament used, cost and error."""
    jobs = await ps.json("GET", f"/models/{model_id}/print-jobs")
    jobs = [_print_job(j) for j in jobs]
    done = [j for j in jobs if j.get("state") in ("completed", "failed", "cancelled")]
    ok = sum(1 for j in done if j["state"] == "completed")
    return _clean(
        {
            "model_id": model_id,
            "prints": len(jobs),
            "success_rate": round(ok / len(done), 2) if done else None,
            "jobs": jobs,
        }
    )


@mcp.tool(annotations=READ)
async def get_model_thumbnail(
    model_id: Annotated[int | None, Field(description="Model whose preview to show")] = None,
    file_id: Annotated[int | None, Field(description="A specific file instead of the model's main preview")] = None,
) -> Image:
    """Preview image of a model (or of one of its files), so you can see what it looks like."""
    if file_id is None:
        if model_id is None:
            raise PrintStashError("Give model_id or file_id.")
        m = await ps.json("GET", f"/models/{model_id}")
        thumb = m.get("thumbnail_url")
        if not thumb:
            raise PrintStashError("This model has no preview yet.")
        path = thumb.split("/api/v1", 1)[-1]
    else:
        path = f"/files/{file_id}/thumbnail"
    r = await ps.request("GET", path)
    fmt = r.headers.get("content-type", "image/png").split("/")[-1].split(";")[0]
    return Image(data=r.content, format=fmt)


@mcp.tool(annotations=READ)
async def get_slicer_link(file_id: int) -> dict[str, Any]:
    """A temporary download link for one file that works without signing in, to open it in a
    slicer or download it. Needs PRINTSTASH_PUBLIC_URL to be a full address."""
    data = await ps.json("GET", f"/files/{file_id}/slicer-url")
    url = data.get("url", "")
    full = f"{PUBLIC_URL}{url}" if PUBLIC_URL and url.startswith("/") else url
    return {"file_id": file_id, "url": full, "orcaslicer": f"orcaslicer://open?file={full}" if full.startswith("http") else None}


@mcp.tool(annotations=READ)
async def list_collections() -> dict[str, Any]:
    """All collections (folders) with their paths and model counts."""
    cols = await ps.json("GET", "/collections")
    return {
        "collections": [
            _clean({"id": c["id"], "name": c["name"], "path": c["path"], "parent_id": c.get("parent_id"), "models": c.get("model_count"), "tags": c.get("tags")})
            for c in cols
        ]
    }


@mcp.tool(annotations=READ)
async def list_tags() -> dict[str, Any]:
    """All tags with how many models use each."""
    tags = await ps.json("GET", "/tags")
    return {"tags": [_clean({"name": t["name"], "slug": t["slug"], "models": t.get("model_count")}) for t in tags]}


@mcp.tool(annotations=READ)
async def library_overview() -> dict[str, Any]:
    """Library size and storage, plus counts by file type, material, slicer, printer model,
    revision status and print outcome. Good starting point for questions about the whole library."""
    stats, facets = await asyncio.gather(ps.json("GET", "/models/stats"), ps.json("GET", "/models/facets"))
    breakdown = {k: {i["value"]: i["count"] for i in v} for k, v in facets.items() if isinstance(v, list)}
    return _clean({"totals": stats, "breakdown": breakdown})


@mcp.tool(annotations=READ)
async def print_statistics(
    period: Literal["7d", "30d", "90d", "1y", "all"] = "30d",
) -> dict[str, Any]:
    """Print statistics for a period: number of prints, filament (g), cost, print time, and the
    top collections, filaments, models and printers, with a monthly breakdown."""
    return _clean(await ps.json("GET", "/models/stats/prints", params={"period": period}))


@mcp.tool(annotations=READ)
async def list_library_sources() -> dict[str, Any]:
    """Folders and storage that PrintStash indexes in place (mounted folders, NAS, S3...), with
    the result of their last scan."""
    try:
        libs = await ps.json("GET", "/libraries")
    except PrintStashError as e:
        if "feature_disabled" in str(e):
            return {"sources": [], "note": "Library sources are turned off in PrintStash settings."}
        raise
    return {
        "sources": [
            _clean({
                "id": lib["id"], "name": lib["name"], "path": lib.get("root_path"), "kind": lib.get("source_kind"),
                "enabled": lib.get("enabled"), "schedule": lib.get("scan_schedule"),
                "last_scanned_at": lib.get("last_scanned_at"), "last_scan_status": lib.get("last_scan_status"),
                "last_scan_summary": lib.get("last_scan_summary"),
            })
            for lib in libs
        ]
    }


@mcp.tool(annotations=ADD)
async def scan_library_source(library_id: int) -> dict[str, Any]:
    """Rescan one library source now so new or changed files appear. Returns a job id to check
    with get_job."""
    return _clean(await ps.json("POST", f"/libraries/{library_id}/scan"))


@mcp.tool(annotations=READ)
async def get_job(job_id: str) -> dict[str, Any]:
    """State of a background job (import, scan...), and the model it created when finished."""
    j = await ps.json("GET", f"/jobs/{job_id}")
    return _clean({k: j.get(k) for k in ("job_id", "kind", "state", "model_id", "file_id", "error", "created_at", "finished_at")})


@mcp.tool(annotations=READ)
async def list_printers() -> dict[str, Any]:
    """Printers connected to PrintStash with their provider, group and current state."""
    printers = await ps.json("GET", "/printers")
    return {"printers": [_clean(p) for p in printers]}


@mcp.tool(annotations=READ)
async def list_spools(include_archived: bool = False) -> dict[str, Any]:
    """Filament spools from Spoolman as PrintStash sees them, when the Spoolman connection is on.
    Use the spool ids with log_print."""
    status = await ps.json("GET", "/spoolman")
    if not status.get("enabled"):
        return {"spools": [], "note": "The Spoolman connection is turned off in PrintStash settings."}
    spools = await ps.json("GET", "/spoolman/spools", params={"include_archived": str(include_archived).lower()})
    return {"spools": [_clean(s) for s in spools]}


@mcp.tool(annotations=EDIT)
async def update_model(
    model_id: int,
    name: str | None = None,
    description: Annotated[str | None, Field(description="Plain text, up to 4096 characters")] = None,
    source_url: Annotated[str | None, Field(description="Where the model came from, e.g. a Printables page")] = None,
    collection: Annotated[str | None, Field(description="Move to this collection path; it is created if missing")] = None,
    tags: Annotated[list[str] | None, Field(description="Replaces ALL tags of the model. To add or remove a few, use tag_models")] = None,
) -> dict[str, Any]:
    """Change a model's name, description, source link, collection or full tag list. Only the
    fields you pass are changed."""
    body = {k: v for k, v in {"name": name, "description": description, "source_url": source_url, "collection": collection, "tags": tags}.items() if v is not None}
    if not body:
        raise PrintStashError("Nothing to change: pass at least one field.")
    return _model_detail(await ps.json("PATCH", f"/models/{model_id}", json=body))


@mcp.tool(annotations=EDIT)
async def tag_models(
    model_ids: Annotated[list[int], Field(min_length=1, max_length=500)],
    add: Annotated[list[str] | None, Field(description="Tags to add")] = None,
    remove: Annotated[list[str] | None, Field(description="Tags to remove")] = None,
) -> dict[str, Any]:
    """Add and/or remove tags on several models at once, keeping their other tags."""
    if not add and not remove:
        raise PrintStashError("Pass tags to add or remove.")
    r = await ps.json("POST", "/models/batch/tags", json={"model_ids": model_ids, "add": add or [], "remove": remove or []})
    return _clean(r)


@mcp.tool(annotations=EDIT)
async def set_star(model_id: int, starred: bool = True) -> dict[str, Any]:
    """Star (favorite) or unstar a model."""
    await ps.request("PUT" if starred else "DELETE", f"/models/{model_id}/star")
    return {"model_id": model_id, "starred": starred}


@mcp.tool(annotations=EDIT)
async def update_gcode_revision(
    model_id: int,
    file_id: Annotated[int, Field(description="file_id of the G-code revision, from get_model")],
    label: Annotated[str | None, Field(description="Short name, e.g. 'PETG 0.16 mm'")] = None,
    status: Literal["known_good", "needs_test", "failed", "archived"] | None = None,
    notes: Annotated[str | None, Field(description="Replaces the revision notes")] = None,
    recommended: Annotated[bool | None, Field(description="true makes this the model's recommended revision")] = None,
) -> dict[str, Any]:
    """Set a G-code revision's label, status, notes, or make it the recommended one."""
    body = {k: v for k, v in {"revision_label": label, "revision_status": status, "revision_notes": notes, "is_recommended": recommended}.items() if v is not None}
    if not body:
        raise PrintStashError("Nothing to change: pass at least one field.")
    m = await ps.json("PATCH", f"/models/{model_id}/files/{file_id}/revision", json=body)
    return _model_detail(m)


@mcp.tool(annotations=ADD)
async def log_print(
    model_id: int,
    file_id: Annotated[int, Field(description="The G-code revision (or source file) that was printed, from get_model")],
    state: Literal["completed", "failed", "cancelled"] = "completed",
    printer_name: Annotated[str | None, Field(description="Free text when the printer is not connected to PrintStash, e.g. 'Creator 5 Pro'")] = None,
    printer_id: Annotated[int | None, Field(description="A printer connected to PrintStash, from list_printers")] = None,
    started_at: Annotated[str | None, Field(description="ISO date-time, e.g. 2026-11-05T10:00:00")] = None,
    finished_at: Annotated[str | None, Field(description="ISO date-time")] = None,
    error: Annotated[str | None, Field(description="What went wrong, for failed or cancelled prints")] = None,
    spool_id: Annotated[int | None, Field(description="Spoolman spool id, from list_spools")] = None,
) -> dict[str, Any]:
    """Record a print of a model that PrintStash did not see itself, with its outcome. The first
    completed print of a G-code revision marks it known good."""
    body = {
        "file_id": file_id, "state": state, "printer_name": printer_name, "printer_id": printer_id,
        "started_at": started_at, "finished_at": finished_at, "error": error, "spool_id": spool_id,
    }
    j = await ps.json("POST", f"/models/{model_id}/print-jobs", json={k: v for k, v in body.items() if v is not None})
    return _print_job(j)


@mcp.tool(annotations=FETCH)
async def import_model_from_url(
    url: Annotated[str, Field(description="A direct file or .zip link, or a model page on Printables or Thingiverse")],
    collection: Annotated[str | None, Field(description="Collection path to put it in")] = None,
    tags: Annotated[list[str] | None, Field(description="Tags to add")] = None,
) -> dict[str, Any]:
    """Download a model into the library from a URL. Returns a job id; check it with get_job to
    get the new model id."""
    body = {"url": url, "collection": collection, "tags": ",".join(tags) if tags else None}
    return _clean(await ps.json("POST", "/ingest/url", json={k: v for k, v in body.items() if v is not None}))


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_: Request) -> JSONResponse:
    ok = await ps.health()
    return JSONResponse({"ok": ok, "printstash": BASE_URL}, status_code=200 if ok else 503)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        # The secret path is the access control; the proxy in front sets its own Host.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    log.info("PrintStash MCP for %s listening on :%s", BASE_URL, PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", proxy_headers=True)


if __name__ == "__main__":
    main()
