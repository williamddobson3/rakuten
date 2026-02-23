"""
Root conftest for the test suite.

Pre-imports all modules that have module-level side effects (database engine
creation, Celery app initialisation) so that they execute BEFORE any
autouse fixtures patch ``app.config.settings``.

This prevents MagicMock objects from leaking into SQLAlchemy's
``create_engine`` or Celery's broker URL.
"""

from __future__ import annotations

# ── Pre-import modules with module-level side effects ──────────
# These imports use the *real* settings object, which has valid default
# values (localhost MySQL, localhost Redis).  The engines are lazy – they
# don't open actual connections until a query is issued.
import app.config  # noqa: F401  (instantiates Settings)
import app.celery_app  # noqa: F401  (creates Celery instance)
import app.db.session  # noqa: F401  (creates async_engine + sync_engine)

# Pre-import worker modules so that `@patch("app.workers.xxx.func")` can
# resolve the target attribute without triggering fresh imports inside
# the test (which would hit the mocked settings object).
import app.workers.api_discovery  # noqa: F401
import app.workers.list_scraper  # noqa: F401
import app.workers.detail_scraper  # noqa: F401

# Pre-import services used by workers
import app.services.item_service  # noqa: F401
import app.services.url_canonicalizer  # noqa: F401
import app.services.rakuten_auth  # noqa: F401
