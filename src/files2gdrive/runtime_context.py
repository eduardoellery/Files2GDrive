"""Installed launcher context, inherited by using the same wrapper for children."""
from pathlib import Path
import sys

_launcher = None


def configure_launcher(executable):
    global _launcher
    _launcher = Path(executable).absolute()


def launcher():
    return _launcher


def command(state_directory, *arguments, python=None):
    prefix = [str(_launcher)] if _launcher is not None and python is None else [python or sys.executable, '-m', 'files2gdrive']
    return [*prefix, '--state-dir', str(state_directory), *arguments]
