"""Built-in recovery operations, registered by name.

Journal records store ``recovery`` as a *name*. Rollback resolves names to
callables via the registry's ``by_name`` space. These are the standard
recoveries that configs, hooks, and examples reference - so a journal record
naming ``delete_file`` always resolves, no matter which harness wrote it.

Registered automatically at import into the global ``registry``.
"""

from __future__ import annotations

import os
import shutil

from .registry import registry


def delete_file(path: str) -> None:
    """Inverse of create_file / write-new: remove the file."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def delete_directory(path: str) -> None:
    """Inverse of create_directory (rmdir, empty): remove the dir."""
    try:
        os.rmdir(path)
    except (FileNotFoundError, OSError):
        pass


def truncate_file(path: str, original_size: int) -> None:
    """Inverse of append_write: truncate back to the pre-append size.

    ``original_size`` must be captured BEFORE the append (the preimage rule).
    """
    try:
        with open(path, "r+b") as fh:
            fh.truncate(original_size)
    except FileNotFoundError:
        pass


def restore_file(path: str, preimage_path: str) -> None:
    """Inverse of edit/overwrite: restore the pre-captured bytes.

    ``preimage_path`` is the snapshot taken before the overwrite.
    """
    try:
        shutil.copyfile(preimage_path, path)
    except FileNotFoundError:
        pass


def noop(*args: object, **kwargs: object) -> None:
    """Recovery for record-only K actions (e.g. bash): nothing to undo."""
    return None


def register_builtin_recoveries() -> None:
    """Register all built-in recoveries into the global registry by name."""
    registry.register_recovery("delete_file", delete_file)
    registry.register_recovery("delete_directory", delete_directory)
    registry.register_recovery("truncate_file", truncate_file)
    registry.register_recovery("restore_file", restore_file)
    registry.register_recovery("noop", noop)


register_builtin_recoveries()
