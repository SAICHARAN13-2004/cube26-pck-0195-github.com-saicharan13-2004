"""Keep test databases and uploaded test media outside the working app data."""

import os
import tempfile
from pathlib import Path


_test_storage = tempfile.TemporaryDirectory(prefix="packguard-pytest-")
_test_root = Path(_test_storage.name)
os.environ["PACKGUARD_DB_PATH"] = str(_test_root / "packguard-test.db")
os.environ["PACKGUARD_UPLOAD_DIR"] = str(_test_root / "uploads")
