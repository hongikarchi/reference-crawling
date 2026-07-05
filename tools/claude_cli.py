"""Cross-platform resolution of the Claude Code CLI executable.

On Windows npm installs only `claude.cmd`/`claude.ps1` shims (no .exe), and
CreateProcess does not apply PATHEXT to a bare name — so
subprocess.run(["claude", ...]) raises FileNotFoundError there. shutil.which
applies PATHEXT and returns the shim; on macOS/Linux it returns the plain
binary, and the "claude" fallback preserves the old behavior if PATH lookup
fails at import time.
"""
from __future__ import annotations

import shutil

CLAUDE_BIN = shutil.which("claude") or "claude"
