import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="incon-test-"))
os.environ["META_API_ENABLED"] = "false"
os.environ["ANTHROPIC_API_KEY"] = ""


@pytest.fixture()
def conn(tmp_path):
    from app.db import connect, init_db
    c = connect(tmp_path / "t.sqlite3")
    init_db(c)
    return c
