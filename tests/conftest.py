"""Keep the suite hermetic: tests must not read the developer's `.env`.

`Settings` is a pydantic-settings model with `env_file=".env"`, so every
`Settings(...)` a test builds silently inherits whatever the machine it runs on
happens to have configured for any field the test did not pass explicitly.
That makes the suite's result a property of the checkout's config rather than
of the code — the live VM's percentage per-market cap, for instance, resized
`max_per_market_usd` expectations in four unrelated tests and turned a passing
suite red the moment a laptop was synced to production.

This fixture is autouse and session-wide: it clears `PMBOT_*` from the
environment and detaches the model from its env file, so a test's settings are
exactly what the test wrote down. Anything that genuinely wants to exercise
env loading should build its own `SettingsConfigDict` rather than lean on
ambient state.
"""

from __future__ import annotations

import os

import pytest

from pmbot.config.settings import Settings


@pytest.fixture(autouse=True, scope="session")
def _isolate_settings_from_dotenv():
    original = dict(Settings.model_config)
    Settings.model_config["env_file"] = None
    saved = {k: v for k, v in os.environ.items() if k.startswith("PMBOT_")}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        os.environ.update(saved)
        Settings.model_config.clear()
        Settings.model_config.update(original)
