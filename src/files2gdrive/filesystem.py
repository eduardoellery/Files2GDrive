"""Linux descriptor-based opening: never follow a symlink in a source path."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat


@contextmanager
def open_source(path):
    path = Path(path).absolute()
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor = None
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError('Backup source must be a regular file')
        stream = os.fdopen(descriptor, 'rb')
        descriptor = None
        with stream:
            yield stream
    finally:
        os.close(directory)
        if descriptor is not None:
            os.close(descriptor)
