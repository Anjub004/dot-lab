"""Sandboxed file-system tool.

The agent can only read, write and list files inside the configured workspace
(``./data/workspace`` by default). Every path is validated:

* absolute paths (``/etc/passwd``, ``C:\\Windows``) are rejected,
* any ``..`` component is rejected,
* the fully resolved path (after following symlinks) must stay inside the
  workspace, which also defeats symlinks pointing outside it.

Overwriting an existing file requires human approval.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.tools.base import BaseTool, ToolContext, ToolError, ToolInputError, ToolResult

MAX_LIST_ENTRIES = 500


class PathSecurityError(ToolInputError):
    """Raised when a path would escape the workspace."""


class FileSystemInput(BaseModel):
    operation: Literal["read", "write", "list"] = Field(
        description="'read' a file, 'write' (create/overwrite) a file, or 'list' a directory."
    )
    path: str = Field(
        default=".",
        max_length=255,
        description="Path relative to the workspace root, e.g. 'reports/summary.md'.",
    )
    content: str | None = Field(default=None, description="File content (required for 'write').")

    @model_validator(mode="after")
    def _content_for_write(self) -> FileSystemInput:
        if self.operation == "write" and self.content is None:
            raise ValueError("'content' is required for write operations")
        return self


class Workspace:
    """Resolves user-supplied paths safely inside a root directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        """Return an absolute path inside the workspace or raise PathSecurityError."""
        raw = (relative or ".").strip()
        if "\x00" in raw:
            raise PathSecurityError("Path contains a null byte")
        if (
            raw.startswith(("/", "\\", "~"))
            or PurePosixPath(raw).is_absolute()
            or PureWindowsPath(raw).is_absolute()
            or PureWindowsPath(raw).drive
        ):
            raise PathSecurityError("Absolute paths are not allowed; use a workspace-relative path")
        parts = PureWindowsPath(raw).parts  # splits on both / and \
        if any(part == ".." for part in parts):
            raise PathSecurityError("Parent directory references ('..') are not allowed")

        candidate = (self.root / Path(*parts)).resolve() if parts else self.root
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise PathSecurityError("Path escapes the workspace")
        return candidate

    def relative(self, path: Path) -> str:
        rel = path.relative_to(self.root).as_posix()
        return rel or "."


class FileSystemTool(BaseTool):
    name = "filesystem"
    description = (
        "Read, write, or list files inside the agent's sandboxed workspace. "
        "Paths must be relative (e.g. 'notes/result.md'). Overwriting an existing "
        "file requires human approval."
    )
    input_model = FileSystemInput

    def __init__(self, workspace_dir: Path, max_bytes: int, writes_require_approval: bool = False):
        self.workspace = Workspace(workspace_dir)
        self.max_bytes = max_bytes
        self.writes_require_approval = writes_require_approval

    def approval_reason(self, args: BaseModel) -> str | None:
        params = self.typed(args, FileSystemInput)
        if params.operation != "write":
            return None
        if self.writes_require_approval:
            return "All file writes are configured to require approval."
        try:
            target = self.workspace.resolve(params.path)
        except PathSecurityError:
            return None  # will be rejected at execution time
        if target.exists():
            return f"This would overwrite the existing file '{params.path}'."
        return None

    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
        params = self.typed(args, FileSystemInput)
        target = self.workspace.resolve(params.path)
        if params.operation == "read":
            return self._read(target)
        if params.operation == "write":
            return self._write(target, params.content or "")
        return self._list(target)

    def _read(self, target: Path) -> ToolResult:
        if not target.is_file():
            raise ToolError(f"File not found: {self.workspace.relative(target)}")
        if target.stat().st_size > self.max_bytes:
            raise ToolError("File is larger than the configured read limit")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("Only UTF-8 text files can be read") from exc
        rel = self.workspace.relative(target)
        return ToolResult(content=text, data={"path": rel, "bytes": len(text.encode())})

    def _write(self, target: Path, content: str) -> ToolResult:
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ToolError("Content exceeds the configured write limit")
        if target.is_dir():
            raise ToolError("Cannot write to a directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(encoded)
        os.replace(tmp, target)  # atomic replace
        rel = self.workspace.relative(target)
        return ToolResult(
            content=f"Wrote {len(encoded)} bytes to {rel}",
            data={"path": rel, "bytes": len(encoded)},
        )

    def _list(self, target: Path) -> ToolResult:
        if not target.is_dir():
            raise ToolError(f"Directory not found: {self.workspace.relative(target)}")
        entries = []
        for child in sorted(target.iterdir())[:MAX_LIST_ENTRIES]:
            if child.name.startswith("."):
                continue
            entries.append(
                {
                    "path": self.workspace.relative(child),
                    "type": "dir" if child.is_dir() else "file",
                    "bytes": child.stat().st_size if child.is_file() else None,
                }
            )
        listing = "\n".join(f"{e['type']:4} {e['path']}" for e in entries) or "(empty)"
        return ToolResult(content=listing, data={"entries": entries})
