/**
 * pc-mcp-worker.js
 * Clean KV-only Cloudflare Worker reverse-proxy for bridge_v2.py + cloudflared.
 */

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS, HEAD",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*"
  };
}

function jsonRes(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() }
  });
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);

      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: corsHeaders() });
      }

      if (!env.TUNNEL_KV) {
        return jsonRes(500, {
          error: "TUNNEL_KV binding missing. Add a KV Namespace binding named TUNNEL_KV in Worker Settings -> Bindings."
        });
      }

      if (url.pathname === "/__update") {
        const expectedToken = env.WORKER_SECRET || "";
        const providedToken = url.searchParams.get("token") || "";
        if (expectedToken && providedToken !== expectedToken) {
          return jsonRes(401, { success: false, error: "Unauthorized: token mismatch" });
        }

        const tunnelUrl = url.searchParams.get("url");
        const name = url.searchParams.get("name") || "PC.MCP.Hub";
        if (!tunnelUrl || !tunnelUrl.startsWith("https://")) {
          return jsonRes(400, { success: false, error: "Invalid tunnel URL" });
        }

        const record = {
          tunnelUrl: tunnelUrl.replace(/\/$/, ""),
          serviceName: name,
          updatedAt: new Date().toISOString()
        };
        await env.TUNNEL_KV.put("bridge_state", JSON.stringify(record));
        return jsonRes(200, { success: true, ...record });
      }

      const rawState = await env.TUNNEL_KV.get("bridge_state");
      let stateObj = {};
      if (rawState) {
        try {
          stateObj = JSON.parse(rawState);
        } catch (_) {}
      }

      if (url.pathname === "/__status" || url.pathname === "/status") {
        return jsonRes(200, {
          status: stateObj.tunnelUrl ? "ready" : "waiting_for_bridge",
          serviceName: stateObj.serviceName || "PC.MCP.Hub",
          tunnelUrl: stateObj.tunnelUrl || null,
          updatedAt: stateObj.updatedAt || null
        });
      }

      if (!stateObj.tunnelUrl) {
        return jsonRes(503, {
          jsonrpc: "2.0",
          id: null,
          error: { code: -32000, message: "PC bridge is offline. Start bridge_v2.py on your PC." }
        });
      }

      const targetUrl = `${stateObj.tunnelUrl}${url.pathname}${url.search}`;
      const proxyHeaders = new Headers(request.headers);
      proxyHeaders.delete("host");

      const resp = await fetch(targetUrl, {
        method: request.method,
        headers: proxyHeaders,
        body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body
      });

      const respHeaders = new Headers(resp.headers);
      for (const [k, v] of Object.entries(corsHeaders())) {
        respHeaders.set(k, v);
      }
      return new Response(resp.body, { status: resp.status, headers: respHeaders });
    } catch (e) {
      return jsonRes(500, { error: e.message, stack: e.stack });
    }
  }
};
