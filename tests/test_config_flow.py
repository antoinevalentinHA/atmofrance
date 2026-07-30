"""Config flow tests.

The point of these is the mapping from what the API raises to what the user
is shown: the flow used to catch only ValueError, which async_get_token never
raises, so every failure surfaced as an unexpected error.
"""
from unittest.mock import patch

import pytest
from aiohttp.client import ClientError
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType

from custom_components.atmofrance.api import TooManyRequestsError
from custom_components.atmofrance.const import (
    CONF_CODE_POSTAL,
    CONF_INCLUDE_POLLEN,
    CONF_INCLUDE_POLLUTION,
    DOMAIN,
)

CREDENTIALS = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "hunter2"}

COMMUNES = [{"code": "33063", "nom": "Bordeaux", "codeEpci": "243300316"}]


def patch_token(side_effect=None):
    return patch(
        "custom_components.atmofrance.config_flow.AtmoFranceDataApi.async_get_token",
        side_effect=side_effect,
    )


def patch_insee(return_value=COMMUNES, side_effect=None):
    return patch(
        "custom_components.atmofrance.config_flow.INSEEAPI.get_data",
        return_value=return_value,
        side_effect=side_effect,
    )


async def start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER})


# ------------------------------------------------- credential errors ----
@pytest.mark.parametrize(("raised", "expected"), [
    (ConnectionRefusedError("bad credentials"), "auth"),
    (TooManyRequestsError("slow down"), "too_many_requests"),
    (ClientError("network down"), "cannot_connect"),
    (TimeoutError(), "cannot_connect"),
])
async def test_credential_failures_map_to_a_translated_message(
        hass, raised, expected):
    result = await start(hass)
    assert result["type"] is FlowResultType.FORM

    with patch_token(side_effect=raised):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}


async def test_valid_credentials_move_on_to_the_location_step(hass):
    result = await start(hass)

    with patch_token():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "location"


# ------------------------------------------------------ INSEE lookup ----
@pytest.mark.parametrize(("raised", "expected"), [
    (ValueError(), "noinsee"),
    (ClientError("network down"), "cannot_connect"),
    (TimeoutError(), "cannot_connect"),
])
async def test_zip_code_failures_map_to_a_translated_message(
        hass, raised, expected):
    result = await start(hass)
    with patch_token():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS)

    with patch_insee(side_effect=raised):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CODE_POSTAL: "33000"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "location"
    assert result["errors"] == {"base": expected}


# ------------------------------------------------------- happy path ----
async def test_full_flow_creates_an_entry(hass):
    result = await start(hass)
    with patch_token():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS)

    with patch_insee():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CODE_POSTAL: "33000"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensors_type"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INCLUDE_POLLUTION: True})
    assert result["step_id"] == "forecast_sensor_type"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["INSEE"] == "33063"
    assert result["data"]["city"] == "Bordeaux"
    assert result["options"][CONF_INCLUDE_POLLUTION] is True
    assert result["options"][CONF_INCLUDE_POLLEN] is False


async def test_selecting_no_indicator_is_rejected(hass):
    result = await start(hass)
    with patch_token():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS)
    with patch_insee():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CODE_POSTAL: "33000"})

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "need_one_option"}
