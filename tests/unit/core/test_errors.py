import pytest

from luna.core.errors import (
    ConfigError,
    LunaError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailable,
    RateLimited,
    ToolError,
)


def test_rate_limited_carries_retry_after():
    err = RateLimited("slow down", retry_after_s=12.5)
    assert err.retry_after_s == 12.5
    assert isinstance(err, ProviderError)
    assert isinstance(err, LunaError)
    assert "slow down" in str(err)


def test_rate_limited_defaults():
    assert RateLimited().retry_after_s is None


@pytest.mark.parametrize(
    "cls", [ProviderError, ProviderRequestError, ProviderUnavailable, ConfigError, ToolError]
)
def test_all_errors_derive_from_luna_error(cls):
    assert issubclass(cls, LunaError)


def test_provider_unavailable_is_provider_error():
    assert issubclass(ProviderUnavailable, ProviderError)
    assert not issubclass(ConfigError, ProviderError)


def test_provider_request_error_is_provider_error_but_not_rate_limit():
    assert issubclass(ProviderRequestError, ProviderError)
    assert not issubclass(ProviderRequestError, RateLimited)
