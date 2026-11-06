# printstash-mcp

An [MCP](https://modelcontextprotocol.io) server for [PrintStash](https://github.com/xiao-villamor/PrintStash), the self-hosted library for 3D models, G-code revisions and print history.

It lets an AI assistant such as Claude work with the whole library: search and organize models, read G-code revisions with their slicer settings, look at previews, log prints and read statistics, upload files and import from URLs, manage collections, tags, share links, pending imports, library sources, backups and connected printers, and move things to the trash, restore or purge them. Anything without a dedicated tool is reachable through `find_api_endpoints` and `api_request`, which cover the full PrintStash API.

Not affiliated with the PrintStash project.

## Run

The image is built for amd64 and arm64 on every change and rebuilt weekly.

```yaml
services:
  printstash-mcp:
    image: ghcr.io/dmmedakin/printstash-mcp:latest
    container_name: printstash_mcp
    restart: unless-stopped
    environment:
      PRINTSTASH_URL: http://printstash:3000
      PRINTSTASH_USERNAME: your-user
      PRINTSTASH_API_KEY: psk_...
      PRINTSTASH_PUBLIC_URL: https://printstash.example.com
      MCP_PATH: /mcp-<long random string>
    ports:
      - "3064:8000"
```

Create the API key in PrintStash under **Settings → Users & Access → API keys**. The server signs in with it and renews the short-lived access token by itself.

The endpoint is `http(s)://<host>:<port><MCP_PATH>` (Streamable HTTP). The server has no authentication of its own: **the long random `MCP_PATH` is the password**, so keep it secret and put the server behind your reverse proxy with TLS. Generate one with `openssl rand -hex 24`.

| Variable | Required | Meaning |
| --- | --- | --- |
| `PRINTSTASH_URL` | yes | PrintStash address as seen from this container |
| `PRINTSTASH_USERNAME` | yes | User that owns the API key |
| `PRINTSTASH_API_KEY` | yes | The API key (`psk_...`) |
| `PRINTSTASH_PUBLIC_URL` | no | Address you open in a browser; used for links to models and slicer downloads |
| `MCP_PATH` | yes | Path of the MCP endpoint |
| `PORT` | no | Listening port, default `8000` |

`GET /healthz` answers 200 while PrintStash is reachable and 503 otherwise; the image's health check uses it.

## Tools

| Area | Tools |
| --- | --- |
| Find and read | `search_models`, `list_models`, `get_model`, `get_model_thumbnail`, `get_print_history`, `get_slicer_link`, `library_overview`, `print_statistics` |
| Change models | `update_model`, `tag_models`, `move_models`, `set_star`, `update_gcode_revision`, `log_print` |
| Add files | `upload_model`, `add_gcode_revision`, `import_model_from_url` |
| Trash and delete | `trash_models`, `list_trash`, `restore_model`, `purge_model`, `trash_file`, `restore_file`, `delete_gcode_revision` |
| Collections and tags | `list_collections`, `create_collection`, `update_collection`, `delete_collection`, `get_collection_readme`, `set_collection_readme`, `list_tags`, `create_tag`, `delete_tag` |
| Sharing | `create_share_link`, `list_share_links`, `revoke_share_link` |
| Pending imports | `list_pending_imports`, `import_pending`, `dismiss_pending` |
| Library sources and jobs | `list_library_sources`, `create_library_source`, `update_library_source`, `remove_library_source`, `scan_library_source`, `list_jobs`, `get_job`, `cancel_job`, `retry_job` |
| Printers | `list_printers`, `get_printer_status`, `printer_command`, `set_printer_temperature`, `send_to_printer` |
| Spoolman, presets, system | `list_spools`, `get_spoolman_settings`, `update_spoolman_settings`, `sync_filament_presets`, `list_presets`, `list_notification_channels`, `test_notification_channel`, `list_backups`, `create_backup`, `verify_backup`, `system_health` |
| Everything else | `find_api_endpoints`, `api_request` |

Uploads and URL imports wait for the import and report `matched_existing_model`: PrintStash deduplicates by content, so a file that is already in the library is attached to the existing model rather than creating a new one.

`api_catalog.json` is the endpoint reference behind `find_api_endpoints`, generated from PrintStash's OpenAPI document with `tools/make_catalog.py`.

## Develop

```sh
pip install -r requirements.txt
PRINTSTASH_URL=http://localhost:3000 PRINTSTASH_USERNAME=me PRINTSTASH_API_KEY=psk_... MCP_PATH=/mcp python server.py
```

## License

MIT
