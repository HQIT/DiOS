"""读取一次性任务的文本产物，目录内容由执行容器控制。"""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO, Callable


_RESULT = "task_result.md"
_MAX_REPORTS = 16
_REPORT_BYTES = 256 * 1024


@contextmanager
def _directory(path: str, parent: int | None = None):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _read_opened(stream: BinaryIO, metadata: os.stat_result, limit: int | None) -> str | None:
    opened = os.fstat(stream.fileno())
    if not os.path.samestat(metadata, opened) or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
        return None
    if limit is None:
        return stream.read().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if opened.st_size > limit:
        return None
    content = stream.read(limit + 1)
    return content.decode("utf-8") if len(content) <= limit else None


def _read_at(directory: int, name: str, limit: int | None) -> str | None:
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return None
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            return _read_opened(stream, metadata, limit)
    except (OSError, UnicodeError):
        return None
    return None


def _read_reports(names: list[str], read: Callable[[str, int | None], str | None]) -> list[tuple[str, str]]:
    reports = []
    remaining = _REPORT_BYTES
    for name in sorted(names):
        if name.casefold() == _RESULT or not name.casefold().endswith(".md"):
            continue
        try:
            name.encode("utf-8")
        except UnicodeError:
            # 文件名也会作为文本出现在 JSON 和下载响应头中。
            continue
        content = read(name, remaining)
        if content is not None:
            reports.append((name, content))
            remaining -= len(content.encode("utf-8"))
            if len(reports) >= _MAX_REPORTS or remaining == 0:
                break
    return reports


def _collect_at(directory: int) -> list[tuple[str, str]]:
    primary = _read_at(directory, _RESULT, None)
    if primary is None:
        return []
    try:
        reports = _read_reports(os.listdir(directory), lambda name, limit: _read_at(directory, name, limit))
    except OSError:
        reports = []
    return [(_RESULT, primary), *reports]


def collect_task_artifacts(workspace: Path, run_id: str) -> list[tuple[str, str]]:
    """回收原有主结果及同一运行目录的 Markdown 报告。"""
    if os.name == "nt":
        return _collect_windows(workspace, run_id)
    try:
        with ExitStack() as stack:
            directory = stack.enter_context(_directory(str(workspace.resolve())))
            for name in ("output", "events", run_id):
                directory = stack.enter_context(_directory(name, directory))
            direct = _collect_at(directory)
            if direct:
                return direct
            subruns = []
            for name in os.listdir(directory):
                try:
                    with _directory(name, directory) as nested:
                        metadata = os.stat(_RESULT, dir_fd=nested, follow_symlinks=False)
                        if stat.S_ISREG(metadata.st_mode):
                            subruns.append((metadata.st_mtime_ns, name))
                except OSError:
                    continue
            for _, name in sorted(subruns, reverse=True):
                try:
                    with _directory(name, directory) as nested:
                        result = _collect_at(nested)
                        if result:
                            return result
                except OSError:
                    continue
    except OSError:
        return []
    return []


def _read_windows(path: Path, limit: int | None) -> str | None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or path.resolve() != path:
            return None
        with path.open("rb") as stream:
            if path.resolve() == path:
                return _read_opened(stream, metadata, limit)
    except (OSError, UnicodeError, RuntimeError):
        pass
    return None


def _collect_windows(workspace: Path, run_id: str) -> list[tuple[str, str]]:
    root = workspace.resolve() / "output" / "events" / run_id
    try:
        if root.resolve() != root:
            return []
        candidates = []
        for path in (root / _RESULT, *root.glob(f"*/{_RESULT}")):
            if path.is_symlink() or path.resolve() != path:
                continue
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                candidates.append((path.parent == root, metadata.st_mtime_ns, path, metadata))
        for _, _, path, _ in sorted(candidates, reverse=True):
            primary = _read_windows(path, None)
            if primary is None:
                continue
            try:
                reports = _read_reports(os.listdir(path.parent), lambda name, limit: _read_windows(path.parent / name, limit))
            except OSError:
                reports = []
            return [(_RESULT, primary), *reports]
    except (OSError, RuntimeError):
        pass
    return []
