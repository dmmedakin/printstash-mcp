# printstash-mcp

An [MCP](https://modelcontextprotocol.io) server for [PrintStash](https://github.com/xiao-villamor/PrintStash), the self-hosted library for 3D models, G-code revisions and print history.

It lets an AI assistant such as Claude search your library, read models with their G-code revisions and slicer settings, look at previews, check print history and statistics, and keep the library tidy: tags, descriptions, revision status, logged prints and imports from a URL. It has no delete tools.

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

**Read:** `search_models`, `list_models` (filters by collection, tags, material, revision status, print outcome, favorites), `get_model` (files, G-code revisions and slicer settings), `get_print_history`, `get_model_thumbnail` (returns the preview image), `get_slicer_link`, `list_collections`, `list_tags`, `library_overview`, `print_statistics`, `list_library_sources`, `get_job`, `list_printers`, `list_spools`.

**Change:** `update_model`, `tag_models`, `set_star`, `update_gcode_revision`, `log_print`, `import_model_from_url`, `scan_library_source`.

## Develop

```sh
pip install -r requirements.txt
PRINTSTASH_URL=http://localhost:3000 PRINTSTASH_USERNAME=me PRINTSTASH_API_KEY=psk_... MCP_PATH=/mcp python server.py
```

## License

MIT
