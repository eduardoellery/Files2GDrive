"""Conservative local filesystem snapshots, without following symbolic links.

An incomplete scan must never be used to infer file deletions. Directory file
descriptors keep traversal anchored if a directory is renamed during a scan.
The transfer worker must still reopen files safely and check their fingerprint.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from .restore import _open_absolute_directory


@dataclass(frozen=True)
class FileSnapshot:
    size: int
    mtime_ns: int
    mode: int
    device: int
    inode: int

    @property
    def fingerprint(self) -> tuple[int, int, int]:
        """Cheap change indicator, not a content checksum."""
        return self.size, self.mtime_ns, self.mode


@dataclass
class ScanResult:
    files: dict[str, FileSnapshot] = field(default_factory=dict)
    complete: bool = True
    issues: list[str] = field(default_factory=list)


def scan_root(path: str | os.PathLike[str], *, should_descend=None, include_file=None) -> ScanResult:
    """Snapshot regular files recursively, including hidden entries.

    Missing/inaccessible roots and traversal errors mark the result incomplete.
    Ignored symlinks and special files are reported but are not scan errors.
    Only ordinary permission bits are retained, excluding setuid/setgid/sticky.
    """
    result = ScanResult()
    root = Path(path).absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def failure(relative: str, error: str) -> None:
        result.complete = False
        result.issues.append(f"{relative}: {error}")

    def walk(fd: int, prefix: str) -> None:
        directory_stat = os.fstat(fd)
        if not directory_stat.st_mode & 0o444 or not directory_stat.st_mode & 0o111:
            failure(prefix or ".", "directory is not readable/searchable")
            return
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    relative = f"{prefix}/{entry.name}" if prefix else entry.name
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            result.issues.append(f"{relative}: symbolic link ignored")
                        elif stat.S_ISDIR(info.st_mode):
                            if should_descend is not None and not should_descend(relative):
                                continue
                            child_fd = os.open(entry.name, flags, dir_fd=fd)
                            try:
                                walk(child_fd, relative)
                            finally:
                                os.close(child_fd)
                        elif stat.S_ISREG(info.st_mode):
                            if include_file is not None and not include_file(relative):
                                continue
                            if not info.st_mode & 0o444 or not os.access(
                                entry.name, os.R_OK, dir_fd=fd, follow_symlinks=False
                            ):
                                failure(relative, "file is not readable")
                                continue
                            result.files[relative] = FileSnapshot(
                                size=info.st_size,
                                mtime_ns=info.st_mtime_ns,
                                mode=stat.S_IMODE(info.st_mode) & 0o777,
                                device=info.st_dev,
                                inode=info.st_ino,
                            )
                        else:
                            result.issues.append(f"{relative}: non-regular file ignored")
                    except OSError as error:
                        failure(relative, str(error))
        except OSError as error:
            failure(prefix or ".", str(error))

    try:
        # Validate/open every ancestor too: O_NOFOLLOW on root alone does not
        # stop a replaced parent directory from redirecting the entire scan.
        with _open_absolute_directory(root) as root_fd:
            walk(root_fd, "")
    except (OSError, ValueError) as error:
        failure(str(root), str(error))
    return result
