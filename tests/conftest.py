"""Load ``.env`` before test collection.

``tests/test_db.py`` reads ``DATABASE_URL_TEST`` from ``os.environ`` at import time to
decide whether to skip, so without this the DB suite silently stays skipped even when the
DSN is sitting in ``.env``. pytest imports conftest before the test modules beside it, so
this runs early enough.

``load_dotenv`` does not override variables already in the environment, so an explicitly
exported DSN still wins over the file.
"""

from __future__ import annotations

from dotenv import load_dotenv

from srip_filter.config import DEFAULT_ENV_PATH

load_dotenv(DEFAULT_ENV_PATH)
