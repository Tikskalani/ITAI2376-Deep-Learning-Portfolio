"""Register the Atlas MCP server in Claude Desktop's config (safe merge, keeps a .bak)."""
import json
import os
import pathlib
import shutil

cfg_dir = pathlib.Path(os.environ["APPDATA"]) / "Claude"
cfg = cfg_dir / "claude_desktop_config.json"
data = {}
if cfg.exists():
    shutil.copy(str(cfg), str(cfg) + ".bak")
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        data = {}
if not isinstance(data, dict):
    data = {}
data.setdefault("mcpServers", {})
repo = r"C:\Users\you\Projects\atlas"
data["mcpServers"]["atlas"] = {
    "command": repo + r"\.venv\Scripts\python.exe",
    "args": [repo + r"\scripts\mcp_server.py"],
    "cwd": repo,
    "env": {"PYTHONPATH": repo},
}
cfg_dir.mkdir(parents=True, exist_ok=True)
cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
print("wrote:", cfg)
print("backup:", (str(cfg) + ".bak") if pathlib.Path(str(cfg) + ".bak").exists() else "(no prior config)")
print("mcpServers now:", list(data["mcpServers"].keys()))
