#!/usr/bin/env python3
"""
DualPower All-in-One PC MCP Multiplexer (bridge_v2.py)
Includes built-in Windows PC Control & Computer-Use tools (PowerShell, WSL,
Filesystem, Window Management, Desktop Screenshots, Mouse & Keyboard) +
BrowserOS Neo + any extra HTTP/SSE or stdio MCP servers configured in mcps.json.
"""

import argparse
import base64
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Enable Windows DPI awareness so screenshot & mouse coordinates match 1:1
if os.name == "nt":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

CONFIG_FILE = Path(__file__).parent / "mcps.json"

DEFAULT_CONFIG = {
    "worker_url": "https://YOUR-PC-MCP-WORKER.workers.dev",
    "worker_secret": "",
    "hub_port": 9500,
    "service_name": "PC.MCP.Hub",
    "cloudflared_path": r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    "enable_builtin_pc_control": True,
    "pc_control_prefix": "pc_",
    "servers": {
        "browseros": {
            "enabled": True,
            "type": "http",
            "url": "http://127.0.0.1:9010/mcp",
            "prefix": "",
            "timeout": 30
        }
    }
}

# =====================================================================
# BUILT-IN PC CONTROL & COMPUTER-USE ENGINE
# =====================================================================

BUILTIN_PC_TOOLS = [
    {
        "name": "run_command",
        "description": "[PC] Execute a PowerShell, cmd, or WSL command on the Windows PC and return stdout, stderr, and exit code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
                "shell": {"type": "string", "enum": ["powershell", "cmd", "wsl"], "default": "powershell"},
                "cwd": {"type": "string", "description": "Optional working directory"},
                "timeout": {"type": "integer", "default": 30}
            },
            "required": ["command"]
        }
    },
    {
        "name": "list_dir",
        "description": "[PC] List files and subdirectories in a directory on the PC.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "read_file",
        "description": "[PC] Read a text file from the PC, optionally slicing by line numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "default": 1},
                "end_line": {"type": "integer", "default": 300}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "[PC] Write or append content to a file on the PC.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "append": {"type": "boolean", "default": False}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "search_files",
        "description": "[PC] Search for files matching a glob pattern or containing a text substring.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string"},
                "pattern": {"type": "string", "default": "*"},
                "text": {"type": "string", "description": "Optional text to grep inside matching files"}
            },
            "required": ["root"]
        }
    },
    {
        "name": "list_windows",
        "description": "[PC] List all visible desktop windows (apps, Antigravity, games) with their titles and PIDs.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "focus_window",
        "description": "[PC] Bring a desktop window to the foreground by matching part of its title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title_contains": {"type": "string"}
            },
            "required": ["title_contains"]
        }
    },
    {
        "name": "desktop_screenshot",
        "description": "[PC] Capture a PNG screenshot of the primary Windows desktop (or a region) to inspect any app or game.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "width": {"type": "integer"},
                "height": {"type": "integer"}
            }
        }
    },
    {
        "name": "mouse_action",
        "description": "[PC] Control the Windows mouse: move, click, double_click, right_click, or scroll at (x, y).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["move", "click", "double_click", "right_click", "scroll"]},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "scroll_delta": {"type": "integer", "description": "Positive=up, negative=down (e.g. -120)"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "keyboard_action",
        "description": "[PC] Type text or send keystrokes (SendKeys syntax, e.g. '^c', '%{TAB}', '{ENTER}') to the active window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Literal text or SendKeys sequence"},
                "literal": {"type": "boolean", "default": True, "description": "If true, escapes special SendKeys characters"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "register_mcp_server",
        "description": "[PC] Dynamically add or update an MCP server in mcps.json so Gemini can wire up new MCPs on the fly.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique key for the MCP server"},
                "type": {"type": "string", "enum": ["stdio", "http"], "default": "stdio"},
                "command": {"type": "string", "description": "Command for stdio MCP (e.g. 'python', 'npx')"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments for stdio MCP"},
                "url": {"type": "string", "description": "URL for http MCP"},
                "prefix": {"type": "string", "default": ""},
                "enabled": {"type": "boolean", "default": True}
            },
            "required": ["name"]
        }
    }
]


def _text_res(msg: str, is_error: bool = False):
    return {"content": [{"type": "text", "text": msg}], "isError": is_error}


def _run_ps(script: str, timeout: int = 20):
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


class BuiltinPcControlClient:
    """Executes built-in PC Control & Computer-Use tools directly in-process."""

    def __init__(self, prefix: str = "pc_", mux_ref=None):
        self.name = "pc_control"
        self.prefix = prefix
        self.mux_ref = mux_ref

    def request(self, payload: dict) -> dict:
        rpc_id = payload.get("id")
        method = payload.get("method", "")
        params = payload.get("params", {})

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": BUILTIN_PC_TOOLS}}

        if method == "tools/call":
            tname = params.get("name", "")
            targs = params.get("arguments", {}) or {}
            try:
                res = self._exec_tool(tname, targs)
            except Exception as e:
                res = _text_res(f"Built-in PC tool error: {e}", True)
            return {"jsonrpc": "2.0", "id": rpc_id, "result": res}

        return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}

    def _exec_tool(self, name: str, args: dict) -> dict:
        if name == "run_command":
            cmd = args["command"]
            shell = args.get("shell", "powershell")
            cwd = args.get("cwd") or None
            timeout = int(args.get("timeout", 30))
            if shell == "powershell":
                full_cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd]
            elif shell == "wsl":
                full_cmd = ["wsl", "bash", "-lc", cmd]
            else:
                full_cmd = ["cmd", "/c", cmd]
            proc = subprocess.run(
                full_cmd, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout
            )
            out = f"Exit code: {proc.returncode}\n"
            if proc.stdout:
                out += f"--- STDOUT ---\n{proc.stdout[:12000]}\n"
            if proc.stderr:
                out += f"--- STDERR ---\n{proc.stderr[:4000]}\n"
            return _text_res(out.strip(), is_error=(proc.returncode != 0))

        if name == "list_dir":
            p = Path(args["path"])
            if not p.exists():
                return _text_res(f"Path not found: {p}", True)
            items = []
            for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                kind = "[DIR]" if entry.is_dir() else f"[{entry.stat().st_size} B]"
                items.append(f"{kind} {entry.name}")
            return _text_res("\n".join(items[:300]) or "(empty)")

        if name == "read_file":
            p = Path(args["path"])
            if not p.is_file():
                return _text_res(f"File not found: {p}", True)
            start = max(1, int(args.get("start_line", 1)))
            end = max(start, int(args.get("end_line", 300)))
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            sliced = [f"{i}: {lines[i-1]}" for i in range(start, min(end, len(lines)) + 1)]
            return _text_res(f"Total lines: {len(lines)}\n" + "\n".join(sliced))

        if name == "write_file":
            p = Path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if args.get("append", False) else "w"
            with open(p, mode, encoding="utf-8") as f:
                f.write(args["content"])
            return _text_res(f"Wrote {len(args['content'])} chars to {p}")

        if name == "search_files":
            root = Path(args["root"])
            pattern = args.get("pattern", "*")
            needle = args.get("text")
            matches = []
            for m in root.rglob(pattern):
                if len(matches) >= 100:
                    break
                if needle:
                    if m.is_file():
                        try:
                            if needle.lower() in m.read_text(encoding="utf-8", errors="ignore").lower():
                                matches.append(str(m))
                        except Exception:
                            pass
                else:
                    matches.append(str(m))
            return _text_res("\n".join(matches) or "No matches found.")

        if name == "list_windows":
            ps = (
                "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | "
                "Select-Object Id, ProcessName, MainWindowTitle | ConvertTo-Json -Compress"
            )
            rc, out, err = _run_ps(ps)
            return _text_res(out or err or "No visible windows.", is_error=(rc != 0))

        if name == "focus_window":
            target = args["title_contains"].replace("'", "''")
            ps = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {{
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}}
"@
$p = Get-Process | Where-Object {{ $_.MainWindowTitle -like '*{target}*' }} | Select-Object -First 1
if ($p) {{
  [WinAPI]::ShowWindow($p.MainWindowHandle, 9) | Out-Null
  [WinAPI]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
  Write-Output "Focused: $($p.MainWindowTitle)"
}} else {{
  Write-Error "No window matching '{target}'"
  exit 1
}}
"""
            rc, out, err = _run_ps(ps)
            return _text_res(out or err, is_error=(rc != 0))

        if name == "desktop_screenshot":
            tmp_png = os.path.join(tempfile.gettempdir(), "pc_mcp_screen.png")
            x, y, w, h = args.get("x"), args.get("y"), args.get("width"), args.get("height")
            if all(v is not None for v in (x, y, w, h)):
                region_code = f"$bounds = New-Object System.Drawing.Rectangle({int(x)},{int(y)},{int(w)},{int(h)})"
            else:
                region_code = "$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds"
            ps = f"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
{region_code}
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save('{tmp_png}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""
            rc, _, err = _run_ps(ps)
            if rc != 0 or not os.path.exists(tmp_png):
                return _text_res(f"Screenshot failed: {err}", True)
            b64 = base64.b64encode(Path(tmp_png).read_bytes()).decode("ascii")
            return {
                "content": [
                    {"type": "image", "data": b64, "mimeType": "image/png"},
                    {"type": "text", "text": "Captured desktop screenshot."}
                ],
                "isError": False
            }

        if name == "mouse_action":
            if os.name != "nt":
                return _text_res("mouse_action requires Windows.", True)
            user32 = ctypes.windll.user32
            action = args["action"]
            if "x" in args and "y" in args:
                user32.SetCursorPos(int(args["x"]), int(args["y"]))
            if action == "click":
                user32.mouse_event(0x0002, 0, 0, 0, 0)
                user32.mouse_event(0x0004, 0, 0, 0, 0)
            elif action == "double_click":
                for _ in range(2):
                    user32.mouse_event(0x0002, 0, 0, 0, 0)
                    user32.mouse_event(0x0004, 0, 0, 0, 0)
            elif action == "right_click":
                user32.mouse_event(0x0008, 0, 0, 0, 0)
                user32.mouse_event(0x0010, 0, 0, 0, 0)
            elif action == "scroll":
                delta = int(args.get("scroll_delta", -120))
                user32.mouse_event(0x0800, 0, 0, delta, 0)
            return _text_res(f"Mouse {action} completed.")

        if name == "keyboard_action":
            txt = args["text"]
            if args.get("literal", True):
                for ch in ("+", "^", "%", "~", "(", ")", "{", "}", "[", "]"):
                    txt = txt.replace(ch, "{" + ch + "}")
            escaped = txt.replace("'", "''")
            ps = f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('{escaped}')"
            rc, _, err = _run_ps(ps)
            return _text_res("Keys sent." if rc == 0 else err, is_error=(rc != 0))

        if name == "register_mcp_server":
            srv_name = args["name"]
            entry = {
                "enabled": args.get("enabled", True),
                "type": args.get("type", "stdio"),
                "prefix": args.get("prefix", f"{srv_name}_"),
                "timeout": 30
            }
            if entry["type"] == "http":
                entry["url"] = args.get("url", "")
            else:
                entry["command"] = args.get("command", "python")
                entry["args"] = args.get("args", [])

            cfg = load_or_create_config()
            cfg.setdefault("servers", {})[srv_name] = entry
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            if self.mux_ref:
                self.mux_ref.reload_from_config(cfg)
            return _text_res(f"Registered and loaded MCP server '{srv_name}' in mcps.json.")

        return _text_res(f"Unknown built-in tool: {name}", True)


# =====================================================================
# EXTERNAL MCP CLIENTS (HTTP/SSE & STDIO)
# =====================================================================

def load_or_create_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = {**DEFAULT_CONFIG, **data}
            if "servers" not in merged or not isinstance(merged["servers"], dict):
                merged["servers"] = DEFAULT_CONFIG["servers"]
            return merged
        except Exception as e:
            print(f"[!] Failed to parse mcps.json ({e}), using defaults.", flush=True)
    else:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            print(f"[*] Created default config at {CONFIG_FILE}", flush=True)
        except Exception as e:
            print(f"[!] Could not write default mcps.json: {e}", flush=True)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def extract_json_from_mcp_stream(body_bytes: bytes):
    if not body_bytes:
        return None
    text = body_bytes.decode("utf-8", errors="replace").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            pass
    for line in text.splitlines():
        sline = line.strip()
        if sline.startswith("data:"):
            cand = sline[5:].strip()
            if cand.startswith("{") and "jsonrpc" in cand:
                try:
                    return json.loads(cand)
                except Exception:
                    pass
    return None


class HttpMcpClient:
    """Connects to a local Streamable HTTP / SSE MCP server (e.g., BrowserOS Neo)."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.url = cfg["url"]
        self.prefix = cfg.get("prefix", "")
        self.timeout = cfg.get("timeout", 30)
        self.extra_headers = cfg.get("headers", {})
        self.session_id = None
        self.lock = threading.Lock()

    def _post(self, payload: dict, include_session: bool = True):
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.extra_headers,
        }
        if include_session and self.session_id:
            headers["mcp-session-id"] = self.session_id

        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def ensure_session(self):
        with self.lock:
            if self.session_id:
                return self.session_id
            init_req = {
                "jsonrpc": "2.0",
                "id": f"{self.name}-init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "DualPowerPCBridge", "version": "2.0.0"},
                },
            }
            status, headers, _ = self._post(init_req, include_session=False)
            for k, v in headers.items():
                if k.lower() == "mcp-session-id":
                    self.session_id = v
                    break
            if self.session_id:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, include_session=True)
                print(f"[âœ“] [{self.name}] HTTP MCP session ready: {self.session_id}", flush=True)
            elif status < 400:
                print(f"[âœ“] [{self.name}] HTTP MCP ready (stateless).", flush=True)
            return self.session_id

    def request(self, payload: dict):
        self.ensure_session()
        status, _, raw_body = self._post(payload, include_session=True)
        if status in (404, 422):
            with self.lock:
                self.session_id = None
            self.ensure_session()
            status, _, raw_body = self._post(payload, include_session=True)

        parsed = extract_json_from_mcp_stream(raw_body)
        if parsed is not None:
            return parsed
        text = raw_body.decode("utf-8", errors="replace") if raw_body else f"HTTP {status}"
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32000, "message": f"[{self.name}] HTTP {status}: {text[:300]}"},
        }


class StdioMcpClient:
    """Spawns and communicates with a local stdio MCP server subprocess."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.command = cfg["command"]
        self.args = cfg.get("args", [])
        self.cwd = cfg.get("cwd") or None
        self.env_overrides = cfg.get("env", {})
        self.prefix = cfg.get("prefix", "")
        self.timeout = cfg.get("timeout", 30)
        self.proc = None
        self.initialized = False
        self.lock = threading.Lock()
        self.next_id = 1

    def _start(self):
        if self.proc and self.proc.poll() is None:
            return
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.env_overrides.items()})
        cmd = [self.command] + [str(a) for a in self.args]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=(os.name == "nt" and self.command.lower() in ("npx", "npm", "uvx")),
            creationflags=creationflags,
        )
        self.initialized = False
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._initialize()

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                line = line.strip()
                if line:
                    print(f"[{self.name} stderr] {line}", flush=True)
        except Exception:
            pass

    def _send_and_wait(self, req: dict) -> dict:
        req_id = req.get("id")
        line = json.dumps(req) + "\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

        if req_id is None:
            return {}

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            raw = self.proc.stdout.readline()
            if not raw:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"Process exited with code {self.proc.returncode}")
                time.sleep(0.02)
                continue
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == req_id:
                return msg
        raise TimeoutError(f"[{self.name}] Timed out waiting for response to id={req_id}")

    def _initialize(self):
        if self.initialized:
            return
        init_req = {
            "jsonrpc": "2.0",
            "id": f"{self.name}-init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "DualPowerPCBridge", "version": "2.0.0"},
            },
        }
        self._send_and_wait(init_req)
        self._send_and_wait({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True
        print(f"[âœ“] [{self.name}] stdio MCP started (PID {self.proc.pid}).", flush=True)

    def request(self, payload: dict) -> dict:
        with self.lock:
            self._start()
            internal_id = self.next_id
            self.next_id += 1
            fwd = dict(payload)
            fwd["id"] = internal_id
            res = self._send_and_wait(fwd)
            res["id"] = payload.get("id")
            return res

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


class McpMultiplexer:
    """Maintains built-in PC Control + all configured MCP clients and routes tool calls."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.clients = {}
        self.tool_routes = {}
        self.route_lock = threading.Lock()
        self._init_clients()

    def _init_clients(self):
        if self.cfg.get("enable_builtin_pc_control", True):
            prefix = self.cfg.get("pc_control_prefix", "pc_")
            self.clients["pc_control"] = BuiltinPcControlClient(prefix=prefix, mux_ref=self)
            print(f"[âœ“] [pc_control] Built-in PC Control & Computer-Use active (prefix='{prefix}').", flush=True)

        for name, scfg in self.cfg.get("servers", {}).items():
            if name == "pc_control" and "pc_control" in self.clients:
                continue
            if not scfg.get("enabled", True):
                print(f"[-] [{name}] Disabled in mcps.json, skipping.", flush=True)
                continue
            stype = scfg.get("type", "http").lower()
            if stype == "http":
                self.clients[name] = HttpMcpClient(name, scfg)
            elif stype == "stdio":
                self.clients[name] = StdioMcpClient(name, scfg)
            else:
                print(f"[!] [{name}] Unknown server type '{stype}', skipping.", flush=True)

    def reload_from_config(self, new_cfg: dict):
        self.cfg = new_cfg
        for name, scfg in new_cfg.get("servers", {}).items():
            if name not in self.clients and scfg.get("enabled", True):
                stype = scfg.get("type", "stdio").lower()
                if stype == "http":
                    self.clients[name] = HttpMcpClient(name, scfg)
                elif stype == "stdio":
                    self.clients[name] = StdioMcpClient(name, scfg)
        self.list_all_tools()

    def list_all_tools(self) -> list:
        merged = []
        new_routes = {}
        for name, client in self.clients.items():
            try:
                res = client.request({"jsonrpc": "2.0", "id": f"list-{name}", "method": "tools/list", "params": {}})
                tools = res.get("result", {}).get("tools", [])
                prefix = client.prefix or ""
                for tool in tools:
                    orig_name = tool["name"]
                    exposed_name = f"{prefix}{orig_name}" if prefix else orig_name
                    if exposed_name in new_routes:
                        exposed_name = f"{name}_{orig_name}"
                    t_copy = dict(tool)
                    t_copy["name"] = exposed_name
                    merged.append(t_copy)
                    new_routes[exposed_name] = (name, orig_name)
                print(f"[âœ“] [{name}] Loaded {len(tools)} tools.", flush=True)
            except Exception as e:
                print(f"[!] [{name}] Offline or failed tools/list: {e}", flush=True)

        with self.route_lock:
            self.tool_routes = new_routes
        return merged

    def call_tool(self, rpc_id, tool_name: str, arguments: dict) -> dict:
        with self.route_lock:
            route = self.tool_routes.get(tool_name)

        if not route:
            self.list_all_tools()
            with self.route_lock:
                route = self.tool_routes.get(tool_name)

        if not route:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": f"Tool '{tool_name}' not found on any active local MCP server."}],
                    "isError": True,
                },
            }

        client_name, orig_name = route
        client = self.clients[client_name]
        try:
            res = client.request({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "tools/call",
                "params": {"name": orig_name, "arguments": arguments or {}},
            })
            res["id"] = rpc_id
            return res
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": f"[{client_name}] Execution error: {e}"}],
                    "isError": True,
                },
            }

    def close_all(self):
        for client in self.clients.values():
            if hasattr(client, "close"):
                client.close()


class PCMCPHandler(BaseHTTPRequestHandler):
    mux: McpMultiplexer = None
    service_name: str = "PC.MCP.Hub"

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "*")

    def send_json(self, status: int, obj: dict):
        try:
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_sse_json(self, obj: dict):
        try:
            payload = f"event: message\ndata: {json.dumps(obj)}\n\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, format, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} - {format % args}", flush=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        accept = self.headers.get("Accept", "")
        if "text/event-stream" in accept and "application/json" not in accept:
            self.send_sse_json({"status": "ready", "server": self.service_name})
            return

        self.send_json(200, {
            "status": "ready",
            "server": self.service_name,
            "version": "2.1.0",
            "protocolVersion": "2024-11-05",
            "activeServers": list(self.mux.clients.keys()),
        })

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            rpc_req = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            self.send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
            return

        rpc_id = rpc_req.get("id")
        method = rpc_req.get("method", "")
        params = rpc_req.get("params", {})

        if method == "initialize":
            self.send_sse_json({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "logging": {},
                        "prompts": {},
                        "resources": {},
                        "tools": {"listChanged": True},
                    },
                    "serverInfo": {"name": self.service_name, "title": self.service_name, "version": "2.1.0"},
                    "instructions": (
                        "All-in-One PC MCP Hub: Built-in Windows PC Control (pc_run_command, pc_read_file, "
                        "pc_write_file, pc_desktop_screenshot, pc_mouse_action, pc_keyboard_action, "
                        "pc_register_mcp_server) + BrowserOS Neo + any extra MCPs in mcps.json."
                    ),
                },
            })
        elif method == "notifications/initialized":
            self.send_response(202)
            self.send_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif method == "tools/list":
            tools = self.mux.list_all_tools()
            self.send_sse_json({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": tools}})
        elif method == "prompts/list":
            self.send_sse_json({"jsonrpc": "2.0", "id": rpc_id, "result": {"prompts": []}})
        elif method == "resources/list":
            self.send_sse_json({"jsonrpc": "2.0", "id": rpc_id, "result": {"resources": []}})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            res = self.mux.call_tool(rpc_id, tool_name, tool_args)
            self.send_json(200, res)
        else:
            self.send_json(200, {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })


def update_worker(worker_url: str, tunnel_url: str, secret: str = "", service_name: str = "PC.MCP.Hub") -> bool:
    clean_worker = worker_url.rstrip("/")
    query = urllib.parse.urlencode({
        "url": tunnel_url,
        "name": service_name,
        **({"token": secret} if secret else {}),
    })
    update_endpoint = f"{clean_worker}/__update?{query}"
    req = urllib.request.Request(update_endpoint, headers={"User-Agent": "DualPowerPCBridge/2.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("success", False))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"[!] Worker HTTP {e.code}: {err_body[:300]}", flush=True)
        return False
    except Exception as e:
        print(f"[!] Failed to notify worker ({e})", flush=True)
        return False


def main():
    parser = argparse.ArgumentParser(description="All-in-One PC MCP Multiplexer (bridge_v2.py)")
    parser.add_argument("--no-tunnel", action="store_true", help="Run local HTTP hub only without cloudflared")
    args = parser.parse_args()

    cfg = load_or_create_config()
    hub_port = int(cfg["hub_port"])
    worker_url = cfg["worker_url"].rstrip("/")
    worker_secret = cfg.get("worker_secret", "")
    service_name = cfg.get("service_name", "PC.MCP.Hub")

    print("=" * 72, flush=True)
    print("      âš¡ ALL-IN-ONE PC MCP MULTIPLEXER + PC CONTROL (bridge_v2.py) âš¡", flush=True)
    print("=" * 72, flush=True)
    print(f"[*] Config file:         {CONFIG_FILE}", flush=True)
    print(f"[*] Local Hub Port:      http://127.0.0.1:{hub_port}/mcp", flush=True)
    print(f"[*] Permanent Worker:    {worker_url}/mcp", flush=True)
    print("-" * 72, flush=True)

    mux = McpMultiplexer(cfg)
    tools = mux.list_all_tools()
    print(f"[*] Initial aggregated tool count: {len(tools)}", flush=True)

    PCMCPHandler.mux = mux
    PCMCPHandler.service_name = service_name

    server = ThreadingHTTPServer(("127.0.0.1", hub_port), PCMCPHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[âœ“] Multiplexer listening on 127.0.0.1:{hub_port}", flush=True)

    cf_proc = None
    if not args.no_tunnel:
        cf_bin = cfg["cloudflared_path"]
        if not os.path.isfile(cf_bin):
            print(f"[X] cloudflared.exe not found at: {cf_bin}", flush=True)
            mux.close_all()
            sys.exit(1)

        print("[*] Starting cloudflared Quick Tunnel...", flush=True)
        cf_proc = subprocess.Popen(
            [cf_bin, "tunnel", "--http-host-header", f"127.0.0.1:{hub_port}", "--url", f"http://127.0.0.1:{hub_port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        tunnel_url = None
        tunnel_regex = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        t0 = time.time()
        while time.time() - t0 < 30:
            line = cf_proc.stdout.readline()
            if not line:
                if cf_proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue
            m = tunnel_regex.search(line)
            if m and "api.trycloudflare.com" not in m.group(0):
                tunnel_url = m.group(0)
                break

        if not tunnel_url:
            print("[X] Failed to obtain trycloudflare.com URL.", flush=True)
            cf_proc.terminate()
            mux.close_all()
            sys.exit(1)

        print(f"[âœ“] Tunnel URL: {tunnel_url}", flush=True)
        if update_worker(worker_url, tunnel_url, worker_secret, service_name):
            print(f"[âœ“] Worker synchronized -> {worker_url}/mcp", flush=True)
        else:
            print("[!] Worker sync failed (check worker_url / worker_secret).", flush=True)

    def cleanup(signum, frame):
        print("\n[*] Shutting down bridge_v2...", flush=True)
        if cf_proc and cf_proc.poll() is None:
            cf_proc.terminate()
        mux.close_all()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("=" * 72, flush=True)
    print("  Ready! Keep this window open. Press Ctrl+C to stop.", flush=True)
    print("=" * 72, flush=True)

    while True:
        if cf_proc:
            line = cf_proc.stdout.readline()
            if not line and cf_proc.poll() is not None:
                print("[!] cloudflared exited unexpectedly.", flush=True)
                break
        time.sleep(0.5)


if __name__ == "__main__":
    main()
