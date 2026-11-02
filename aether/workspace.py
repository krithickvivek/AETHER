"""Workspace: the only way agents may write project files.

Every file goes into one run's own folder. Paths that are absolute, climb out with "..",
or resolve outside the folder are refused, as are unexpected file types, oversized files
and too many files. Nothing here runs code.
"""

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath

from aether.context import FileRecord

ALLOWED_SUFFIXES = {".py", ".md", ".txt", ".html", ".css", ".json", ".sql"}
MAX_FILE_BYTES = 100_000
MAX_FILES = 50


class WorkspaceError(ValueError):
    """A file operation was refused because it is unsafe or over a limit."""


class Workspace:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()

    def resolve(self, relative: str) -> Path:
        """The real path for a project-relative path, or WorkspaceError if it is not safe."""
        if not isinstance(relative, str) or not relative.strip() or "\0" in relative:
            raise WorkspaceError("A file path must be a non-empty relative path.")
        windows, posix = PureWindowsPath(relative), PurePosixPath(relative.replace("\\", "/"))
        if windows.is_absolute() or windows.drive or windows.root or posix.is_absolute():
            raise WorkspaceError(f"Absolute paths are not allowed: {relative!r}.")
        if ".." in posix.parts:
            raise WorkspaceError(f"Paths may not climb out of the project folder: {relative!r}.")
        if posix.suffix.lower() not in ALLOWED_SUFFIXES:
            raise WorkspaceError(f"File type {posix.suffix or '(none)'!r} is not allowed: {relative!r}.")
        target = (self.root / posix).resolve()  # also follows any links
        if not target.is_relative_to(self.root):
            raise WorkspaceError(f"Path resolves outside the project folder: {relative!r}.")
        return target

    def write_text(self, relative: str, content: str, author: str) -> FileRecord:
        target = self.resolve(relative)
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise WorkspaceError(f"{relative!r} is {len(data)} bytes (limit {MAX_FILE_BYTES}).")
        if not target.exists() and len(self.list_files()) >= MAX_FILES:
            raise WorkspaceError(f"The project already has the maximum of {MAX_FILES} files.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_bytes(data)
        temp.replace(target)  # replace in one step so a crash can't leave half a file
        return FileRecord(path=self._relative(target), author=author,
                          sha256=hashlib.sha256(data).hexdigest(), size=len(data))

    def read_text(self, relative: str) -> str:
        return self.resolve(relative).read_text(encoding="utf-8")

    def list_files(self) -> list[str]:
        """Project files as forward-slash paths, sorted."""
        return sorted(self._relative(p) for p in self.root.rglob("*") if p.is_file() and p.suffix != ".tmp")

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()
