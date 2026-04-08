"""Shared utility functions for the SR Dashboard."""

import json
import os
import tempfile


def atomic_json_write(filepath, data, indent=2, cls=None):
    """Write JSON atomically via temp file + rename."""
    dir_name = os.path.dirname(os.path.abspath(filepath))
    with tempfile.NamedTemporaryFile('w', dir=dir_name, suffix='.tmp', delete=False) as f:
        json.dump(data, f, indent=indent, ensure_ascii=False, cls=cls)
        tmp_path = f.name
    os.replace(tmp_path, filepath)


def atomic_text_write(filepath, text):
    """Write text atomically via temp file + rename."""
    dir_name = os.path.dirname(os.path.abspath(filepath))
    with tempfile.NamedTemporaryFile('w', dir=dir_name, suffix='.tmp', delete=False) as f:
        f.write(text)
        tmp_path = f.name
    os.replace(tmp_path, filepath)
