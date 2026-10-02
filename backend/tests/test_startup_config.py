"""Fail-closed startup checks (``app.config.validate_startup_config``).

DB-free: builds ``Settings`` objects directly and monkeypatches the process
environment; nothing here opens a connection.
"""

from __future__ import annotations

import pytest

from app import config
from app.config import Settings, validate_startup_config
from app.main import app

SECRET_VALUES = {
    "auth_username": "owner-user-value",
    "auth_password": "owner-pass-value",
    "scheduler_secret": "scheduler-secret-value",
}


def _settings(**overrides) -> Settings:
    values = {"environment": "production", **SECRET_VALUES, **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def _not_on_cloud_run(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)


def test_production_with_all_required_settings_starts():
    validate_startup_config(_settings())


@pytest.mark.parametrize("missing", ["auth_username", "auth_password", "scheduler_secret"])
def test_production_missing_required_setting_raises(missing):
    with pytest.raises(RuntimeError) as exc:
        validate_startup_config(_settings(**{missing: ""}))

    message = str(exc.value)
    assert missing.upper() in message
    # Never echo configured values.
    for value in SECRET_VALUES.values():
        assert value not in message


def test_production_names_every_missing_setting():
    with pytest.raises(RuntimeError) as exc:
        validate_startup_config(
            _settings(auth_username="", auth_password="", scheduler_secret="")
        )
    assert "AUTH_USERNAME" in str(exc.value)
    assert "AUTH_PASSWORD" in str(exc.value)
    assert "SCHEDULER_SECRET" in str(exc.value)


@pytest.mark.parametrize("environment", ["development", "test"])
def test_non_production_off_cloud_run_allows_empty_settings(environment):
    validate_startup_config(
        _settings(
            environment=environment,
            auth_username="",
            auth_password="",
            scheduler_secret="",
        )
    )


@pytest.mark.parametrize("environment", ["development", "test", "prod"])
def test_cloud_run_requires_production_environment(monkeypatch, environment):
    monkeypatch.setenv("K_SERVICE", "danecast-trades")
    with pytest.raises(RuntimeError, match="ENVIRONMENT"):
        validate_startup_config(_settings(environment=environment))


def test_cloud_run_production_fully_configured_starts(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "danecast-trades")
    validate_startup_config(_settings())


def test_defaults_to_module_settings(monkeypatch):
    monkeypatch.setattr(config.settings, "environment", "production")
    monkeypatch.setattr(config.settings, "auth_username", "")
    with pytest.raises(RuntimeError, match="AUTH_USERNAME"):
        validate_startup_config()


async def test_lifespan_refuses_to_start_when_misconfigured(monkeypatch):
    monkeypatch.setattr(config.settings, "environment", "production")
    monkeypatch.setattr(config.settings, "scheduler_secret", "")
    with pytest.raises(RuntimeError, match="SCHEDULER_SECRET"):
        async with app.router.lifespan_context(app):
            pass
