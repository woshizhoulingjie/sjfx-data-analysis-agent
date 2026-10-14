"""Scratch files for parsers belong to the configured writable instance."""
from pathlib import Path
import tempfile


def temporary_parse_directory(*, prefix):
    """Avoid system TEMP and the read-only application/model directories."""
    from config import Config
    root = Path(Config.PARSE_TEMP_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=root)
