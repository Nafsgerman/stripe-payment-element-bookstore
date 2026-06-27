"""Test configuration shared by the suite.

Lives at the repo root so the app modules (config, orders, payments, webhooks)
are importable from tests/, and so the test environment is set before config.py
reads it at import time.
"""
import os

# config.py loads the environment once at import; populate it first.
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_PUBLISHABLE_KEY", "pk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")

import pytest

from config import config
import orders


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Point every test at its own fresh SQLite file.

    `config` is a frozen dataclass, so we bypass its immutability to repoint the
    path per test; each test starts from an empty, migrated database.
    """
    object.__setattr__(config, "database_path", str(tmp_path / "orders.db"))
    orders.init_db()
    yield
