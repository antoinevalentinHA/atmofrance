"""Unit tests for AtmoFranceDataApi.

The session is faked rather than mocked at the HTTP layer: these tests are
about token lifetime and error handling, not about wire format.
"""
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiohttp.client import ClientError

from custom_components.atmofrance.api import (
    DEFAULT_TIMEOUT,
    TOKEN_DEFAULT_TTL,
    AtmoFranceDataApi,
    InvalidAuthError,
    TooManyRequestsError,
    _jwt_expiry,
)
from custom_components.atmofrance.const import REFRESH_INTERVALL, URL_CODE

from .const import ENTRY_DATA, feature

FAKE_HASS = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Paris"))


def make_jwt(expires_in_seconds):
    exp = int((datetime.now(timezone.utc)
               + timedelta(seconds=expires_in_seconds)).timestamp())
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class Response:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Replays scripted responses and counts calls."""

    def __init__(self, login_responses, get_responses):
        self._logins = list(login_responses)
        self._gets = list(get_responses)
        self.posts = 0
        self.gets = 0
        self.timeouts = []

    async def post(self, url, json=None, timeout=None):
        self.posts += 1
        self.timeouts.append(timeout)
        return self._logins.pop(0) if len(self._logins) > 1 else self._logins[0]

    async def get(self, url, headers=None, timeout=None):
        self.gets += 1
        self.timeouts.append(timeout)
        resp = self._gets.pop(0) if len(self._gets) > 1 else self._gets[0]
        if isinstance(resp, Exception):
            raise resp
        return resp


TODAY = datetime.now().strftime("%Y-%m-%d")
DATA_OK = Response(200, {"features": [feature(TODAY)]})


def build(login_responses, get_responses):
    session = FakeSession(login_responses, get_responses)
    return AtmoFranceDataApi(ENTRY_DATA, session, hass=FAKE_HASS), session


def valid_login():
    return [Response(200, {"token": make_jwt(3600)})]


# --------------------------------------------------------------- jwt ----
def test_jwt_expiry_reads_the_exp_claim():
    expiry = _jwt_expiry(make_jwt(3600))
    delta = (expiry - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < delta < 3700


@pytest.mark.parametrize("token", ["opaque-token", None, "a.!!!!.c", "", 42])
def test_jwt_expiry_tolerates_anything_that_is_not_a_jwt(token):
    assert _jwt_expiry(token) is None


def test_default_ttl_outlives_the_refresh_interval():
    """A TTL below the poll interval makes the cache useless."""
    assert TOKEN_DEFAULT_TTL > timedelta(minutes=REFRESH_INTERVALL)


# ----------------------------------------------------------- timeouts ----
async def test_every_request_carries_a_timeout():
    """The timeout used to be stored and passed to no call at all."""
    api, session = build(valid_login(), [DATA_OK])
    await api.get_data("33063", URL_CODE.POLLUTION)

    assert session.timeouts, "no request was made"
    assert all(t is not None for t in session.timeouts)
    assert all(t.total == DEFAULT_TIMEOUT for t in session.timeouts)


# ------------------------------------------------------- token cache ----
async def test_token_is_reused_across_calls():
    api, session = build(valid_login(), [DATA_OK])
    for _ in range(3):
        await api.get_data("33063", URL_CODE.POLLUTION)
    assert session.posts == 1
    assert session.gets == 3


async def test_token_within_the_expiry_margin_is_renewed():
    api, session = build([Response(200, {"token": make_jwt(30)})], [DATA_OK])
    await api.get_data("33063", URL_CODE.POLLUTION)
    await api.get_data("33063", URL_CODE.POLLUTION)
    assert session.posts == 2


async def test_opaque_token_is_cached_using_the_default_ttl():
    api, session = build([Response(200, {"token": "opaque"})], [DATA_OK])
    await api.get_data("33063", URL_CODE.POLLUTION)
    await api.get_data("33063", URL_CODE.POLLUTION)
    assert session.posts == 1


# ------------------------------------------------------- 401 retries ----
async def test_rejected_token_triggers_one_relogin_and_retry():
    api, session = build(valid_login(), [Response(401, {}), DATA_OK])
    data = await api.get_data("33063", URL_CODE.POLLUTION)
    assert (session.posts, session.gets) == (2, 2)
    assert len(data) == 1


async def test_permanent_401_gives_up_after_two_attempts():
    api, session = build(valid_login(), [Response(401, {"error": "nope"})])
    assert await api.get_data("33063", URL_CODE.POLLUTION) is None
    assert session.gets == 2


# ---------------------------------------------------- error handling ----
@pytest.mark.parametrize("raised", [
    ClientError("boom"),
    # Observed in production 2026-08-08: str() is empty, so the log line read
    # "Failed to get data for INSEE 33063:" and said nothing at all.
    TimeoutError(),
    ClientError(),
])
async def test_network_error_returns_none_and_never_the_exception(raised):
    api, _ = build(valid_login(), [raised])
    result = await api.get_data("33063", URL_CODE.POLLUTION)
    assert result is None
    assert not isinstance(result, Exception)


async def test_the_log_names_the_exception_type(caplog):
    """An exception with an empty str() must still identify itself."""
    api, _ = build(valid_login(), [TimeoutError()])

    await api.get_data("33063", URL_CODE.POLLUTION)

    assert "TimeoutError" in caplog.text


@pytest.mark.parametrize("payload", [
    "<html>error</html>",
    {"message": "quota exceeded"},
    None,
])
async def test_unexpected_payload_returns_none(payload):
    api, _ = build(valid_login(), [Response(200, payload)])
    assert await api.get_data("33063", URL_CODE.POLLUTION) is None


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credentials_propagate_for_reauth(status):
    """Swallowing this is what used to hide a changed password forever."""
    api, _ = build([Response(status, {})], [DATA_OK])
    with pytest.raises(InvalidAuthError):
        await api.get_data("33063", URL_CODE.POLLUTION)


async def test_a_server_error_is_not_an_auth_problem():
    """A 500 must not send the user off to retype a valid password."""
    api, _ = build([Response(500, {})], [DATA_OK])
    assert await api.get_data("33063", URL_CODE.POLLUTION) is None


async def test_too_many_requests_returns_none():
    api, _ = build([Response(429, {}, {"Retry-After": "60"})], [DATA_OK])
    assert await api.get_data("33063", URL_CODE.POLLUTION) is None


async def test_async_get_token_raises_on_429():
    api, _ = build([Response(429, {}, {"Retry-After": "60"})], [DATA_OK])
    with pytest.raises(TooManyRequestsError):
        await api.async_get_token()


async def test_async_get_token_raises_on_other_errors():
    api, _ = build([Response(500, {})], [DATA_OK])
    with pytest.raises(ConnectionRefusedError):
        await api.async_get_token()


# ----------------------------------------------------- nominal path ----
async def test_nominal_fetch_exposes_metadata():
    api, _ = build(valid_login(), [DATA_OK])
    data = await api.get_data("33063", URL_CODE.POLLUTION)
    assert len(data) == 1
    assert api.source == "ATMO Nouvelle-Aquitaine"
    assert api.nom_zone == "Bordeaux"
    assert api.type_zone == "commune"


async def test_empty_result_set_returns_an_empty_list():
    api, _ = build(valid_login(), [Response(200, {"features": []})])
    assert await api.get_data("33063", URL_CODE.POLLUTION) == []


# ------------------------------------- what sensor._raw_value() sees ----
async def test_get_key_value_variants():
    api, _ = build(valid_login(), [DATA_OK])
    await api.get_data("33063", URL_CODE.POLLUTION)
    api._data = {"features": [feature(TODAY, code_no2=None, code_o3=0)]}

    # J+1 not published yet: no feature carries tomorrow's date
    assert api.get_key_value("code_pm10", 1) == ""
    # JSON null, which int() would have crashed on
    assert api.get_key_value("code_no2", 0) is None
    # a real 0 must survive, not be mistaken for "absent"
    assert api.get_key_value("code_o3", 0) == 0
    assert api.get_key_value("code_pm10", 0) == 2
