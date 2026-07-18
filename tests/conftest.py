"""Point the app at a throwaway data dir BEFORE any app module is imported, so
tests that touch the DB (voices roster, KV) never read or write real data/."""
import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="podcastfeeds-tests-")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault("CONFIG_DIR", str(Path(_TMP) / "config"))
