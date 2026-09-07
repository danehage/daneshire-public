"""TLS negotiation for the database URL.

asyncpg's connect() takes ``ssl=``, not libpq's ``sslmode=``, and the
SQLAlchemy asyncpg dialect does not translate between them: a URL carrying
``?sslmode=...`` reached the driver as an unexpected keyword and raised
TypeError at startup. .env.example documents exactly that form, so copying
it verbatim broke the app. The option is now parsed out of the URL and
expressed only through connect_args.
"""

import pytest

from app.database import _split_ssl_option


def test_plain_url_uses_tls():
    url, tls = _split_ssl_option("postgresql+asyncpg://u:p@host/db")
    assert tls is True
    assert url == "postgresql+asyncpg://u:p@host/db"


@pytest.mark.parametrize("param", ["sslmode", "ssl"])
def test_ssl_param_is_stripped_from_the_url(param):
    """Whatever the value, the driver must never see this parameter."""
    url, tls = _split_ssl_option(f"postgresql+asyncpg://u:p@host/db?{param}=require")
    assert tls is True
    assert param not in url


@pytest.mark.parametrize(
    "value", ["disable", "disabled", "false", "off", "0", "DISABLE", " disable "]
)
def test_explicit_opt_out_disables_tls(value):
    _, tls = _split_ssl_option(f"postgresql+asyncpg://u:p@host/db?sslmode={value}")
    assert tls is False


@pytest.mark.parametrize("value", ["require", "verify-full", "prefer", "allow", "1"])
def test_anything_else_keeps_tls_on(value):
    """Production must not lose TLS to an unrecognised value."""
    _, tls = _split_ssl_option(f"postgresql+asyncpg://u:p@host/db?sslmode={value}")
    assert tls is True


def test_other_query_params_are_preserved():
    url, _ = _split_ssl_option(
        "postgresql+asyncpg://u:p@host/db?sslmode=require&application_name=danecast"
    )
    assert "application_name=danecast" in url
    assert "sslmode" not in url


def test_empty_url_is_handled():
    url, tls = _split_ssl_option("")
    assert url == ""
    assert tls is False
