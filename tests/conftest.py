"""Load ``.env`` before test collection.

``test_db.py`` reads ``DATABASE_URL_TEST`` at import time to decide whether to skip, so
without this the DB suite stays silently skipped even with the DSN sitting in ``.env``.
``load_dotenv`` never overrides the environment, so an exported DSN still wins.
"""

from __future__ import annotations

from dotenv import load_dotenv

from srip_filter.config import DEFAULT_ENV_PATH

load_dotenv(DEFAULT_ENV_PATH)
