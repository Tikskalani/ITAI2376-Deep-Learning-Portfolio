"""Atlas MCP server over stdio, for Claude Desktop on the laptop.

The tools live in core/mcp_server.py and are the same ones the web
process serves over HTTP at /mcp (behind ATLAS_MCP_TOKEN) for Cowork,
Claude Code in the cloud, and the Claude mobile app.

Register with Claude Desktop in %APPDATA%\\Claude\\claude_desktop_config.json:

  {
    "mcpServers": {
      "atlas": {
        "command": "C:\\\\Users\\\\you\\\\Projects\\\\atlas\\\\.venv\\\\Scripts\\\\python.exe",
        "args": ["-m", "scripts.mcp_server"],
        "cwd": "C:\\\\Users\\\\you\\\\Projects\\\\atlas"
      }
    }
  }

Restart Claude Desktop; "atlas" then appears under the + -> Connectors menu.
"""

from __future__ import annotations

import pathlib
import sys

# Claude Desktop may not honor cwd/PYTHONPATH; make the package importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.mcp_server import mcp  # noqa: E402


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
