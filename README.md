# PC MCP Hub (BrowserOS Neo + Windows PC Control + Universal Multiplexer)

Connect Gemini (or any MCP client) to **BrowserOS Neo**, **Windows PC Control & Computer-Use**, and **any local `http` or `stdio` MCP server** on your PC through a single permanent Cloudflare Worker URL — with **zero Python `pip install` dependencies**.

## Features

- **Built-in Windows PC Control (`pc_*`):** PowerShell/CMD/WSL command execution, file read/write/search, window listing/focus, desktop screenshots, mouse clicks/scroll, and keyboard input.
- **BrowserOS Neo Integration:** Automatically manages `mcp-session-id` and unwraps SSE streams.
- **Universal MCP Multiplexer (`mcps.json`):** Add any local `http` or `stdio` MCP server (or register them dynamically with `pc_register_mcp_server`).

## Setup Guide

### 1. Deploy the Cloudflare Worker (Free Plan)

You can deploy using either the **Cloudflare Dashboard (Web UI)** or **Wrangler CLI**. Both use the exact same KV binding (`TUNNEL_KV`).

#### Option A: Cloudflare Dashboard (Recommended, No CLI / No Wrangler Required)
1. In the [Cloudflare Dashboard](https://dash.cloudflare.com/), go to **Storage & Databases → KV** and click **Create a namespace** (e.g., name it `pc-mcp-kv`).
2. Go to **Compute (Workers) → Create application → Create Worker**, paste the contents of [`cloudflare-worker/pc-mcp-worker.js`](cloudflare-worker/pc-mcp-worker.js), and click **Deploy**.
3. Go to your Worker's **Settings → Bindings → Add → KV Namespace**:
   - Set **Variable name** to: `TUNNEL_KV`
   - Select your KV namespace: `pc-mcp-kv`
   - Click **Save**.
4. *(Recommended)* Under **Settings → Variables and Secrets**, add an environment variable `WORKER_SECRET` with a random secret key.

#### Option B: Wrangler CLI (Optional)
1. Run `npx wrangler kv namespace create TUNNEL_KV` and copy the generated namespace ID.
2. In `cloudflare-worker/wrangler.toml`, uncomment `[[kv_namespaces]]` and paste your ID.
3. Run `npx wrangler deploy`.

### 2. Configure & Run on Windows

1. Install **Python 3.10+** and **[cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)**.
2. Copy `mcps.example.json` to `mcps.json` and set your `worker_url` (and `worker_secret` if configured).
3. Start BrowserOS Neo (if using browser tools), then double-click `start_bridge.bat` (or run `python bridge_v2.py`).

### 3. Connect to Gemini

1. Open **Gemini Settings → Integrations / MCPs → Connect to an MCP server**.
2. Enter `https://<your-worker>.workers.dev/mcp`.
3. Start a new chat and Gemini will automatically load all enabled tools.
