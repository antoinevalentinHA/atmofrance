"""Setup and coordinator tests.

These cover the orchestration in __init__.py, so AtmoFranceDataApi is replaced
wholesale: what matters here is which zone code each indicator resolves to and
what happens when one of them resolves to nothing.
"""
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmofrance.api import InvalidAuthError
from custom_components.atmofrance.const import (
    CONF_INCLUDE_POLLEN,
    CONF_INCLUDE_POLLEN_FORECAST,
    CONF_INCLUDE_POLLUTION,
    CONF_INCLUDE_POLLUTION_FORECAST,
    CONF_INSEE_CODE,
    CONF_INSEE_EPCI,
    CONF_POLLEN_COORDINATOR,
    CONF_POLLUTION_COORDINATOR,
    DOMAIN,
    URL_CODE,
)

from .const import (
    CITY_CODE,
    ENTRY_DATA,
    EPCI_CODE,
    POLLUTION_AND_POLLEN,
    POLLUTION_ONLY,
    feature,
)

FEATURES = [feature("2026-07-30")]


class FakeApi:
    """Stands in for AtmoFranceDataApi, driven by a coverage table."""

    def __init__(self, coverage):
        self._coverage = coverage
        self.calls = []

    async def get_data(self, code, url_code):
        self.calls.append((code, url_code))
        return self._coverage.get((code, url_code))

    def get_key_value(self, key, shift=0):
        return 2

    @property
    def source(self):
        return "ATMO Nouvelle-Aquitaine"

    @property
    def last_update(self):
        return "2026-07-30 09:00:00"

    @property
    def type_zone(self):
        return "commune"

    @property
    def nom_zone(self):
        return "Bordeaux"


def patch_api(coverage):
    """Patch the API class, handing every instance the same coverage table."""
    return patch(
        "custom_components.atmofrance.AtmoFranceDataApi",
        side_effect=lambda *args, **kwargs: FakeApi(coverage),
    )


async def setup_entry(hass, coverage, options):
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=options, version=3)
    entry.add_to_hass(hass)
    with patch_api(coverage):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def coordinators(hass, entry):
    return hass.data.get(DOMAIN, {}).get(entry.entry_id, {})


# ------------------------------------------------- source resolution ----
async def test_city_data_wins_when_available(hass):
    entry = await setup_entry(
        hass, {(CITY_CODE, URL_CODE.POLLUTION): FEATURES}, POLLUTION_ONLY)

    assert entry.state is ConfigEntryState.LOADED
    assert coordinators(hass, entry)[
        CONF_POLLUTION_COORDINATOR]._source == CONF_INSEE_CODE


async def test_falls_back_to_epci_when_the_city_has_no_data(hass):
    entry = await setup_entry(
        hass, {(EPCI_CODE, URL_CODE.POLLUTION): FEATURES}, POLLUTION_ONLY)

    assert entry.state is ConfigEntryState.LOADED
    assert coordinators(hass, entry)[
        CONF_POLLUTION_COORDINATOR]._source == CONF_INSEE_EPCI


# ------------------------------ regression: source leaked across kinds ----
async def test_pollen_never_inherits_the_pollution_source(hass):
    """Pollution resolves on EPCI, pollen has nothing anywhere.

    The source variable used to be shared, so pollen silently reused the
    pollution zone and built a coordinator on an API holding no pollen data.
    """
    coverage = {(EPCI_CODE, URL_CODE.POLLUTION): FEATURES}

    entry = await setup_entry(hass, coverage, POLLUTION_AND_POLLEN)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert CONF_POLLEN_COORDINATOR not in coordinators(hass, entry)


async def test_each_indicator_resolves_its_own_zone(hass):
    """Pollution is only covered by the EPCI, pollen only by the city."""
    coverage = {
        (EPCI_CODE, URL_CODE.POLLUTION): FEATURES,
        (CITY_CODE, URL_CODE.POLLEN): FEATURES,
    }

    entry = await setup_entry(hass, coverage, POLLUTION_AND_POLLEN)

    assert entry.state is ConfigEntryState.LOADED
    built = coordinators(hass, entry)
    assert built[CONF_POLLUTION_COORDINATOR]._source == CONF_INSEE_EPCI
    assert built[CONF_POLLEN_COORDINATOR]._source == CONF_INSEE_CODE


# ------------------------------------------- retry instead of KeyError ----
async def test_no_data_at_all_asks_for_a_retry(hass):
    entry = await setup_entry(hass, {}, POLLUTION_ONLY)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_failed_setup_leaves_no_partial_state_behind(hass):
    """Otherwise the entry_id guard skips the rebuild and the retry dies."""
    entry = await setup_entry(hass, {}, POLLUTION_ONLY)

    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_retry_succeeds_once_the_api_answers(hass):
    entry = await setup_entry(hass, {}, POLLUTION_ONLY)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    with patch_api({(CITY_CODE, URL_CODE.POLLUTION): FEATURES}):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


# ------------------------------------------------ coordinator failure ----
async def test_coordinator_marks_failure_when_the_api_returns_nothing(hass):
    entry = await setup_entry(
        hass, {(CITY_CODE, URL_CODE.POLLUTION): FEATURES}, POLLUTION_ONLY)
    coordinator = coordinators(hass, entry)[CONF_POLLUTION_COORDINATOR]

    coordinator.api = FakeApi({})
    with pytest.raises(UpdateFailed):
        await coordinator._update_method()


async def test_coordinator_failure_reaches_last_update_success(hass):
    entry = await setup_entry(
        hass, {(CITY_CODE, URL_CODE.POLLUTION): FEATURES}, POLLUTION_ONLY)
    coordinator = coordinators(hass, entry)[CONF_POLLUTION_COORDINATOR]

    coordinator.api = FakeApi({})
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)


async def test_coordinator_returns_the_payload_on_success(hass):
    entry = await setup_entry(
        hass, {(CITY_CODE, URL_CODE.POLLUTION): FEATURES}, POLLUTION_ONLY)
    coordinator = coordinators(hass, entry)[CONF_POLLUTION_COORDINATOR]

    assert await coordinator._update_method() == FEATURES


# ---------------------------------------------------- options access ----
async def test_setup_survives_options_missing_a_key(hass):
    """Direct indexing raised KeyError when a migration left options short."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA,
        options={CONF_INCLUDE_POLLUTION: True}, version=3)
    entry.add_to_hass(hass)

    with patch_api({(CITY_CODE, URL_CODE.POLLUTION): FEATURES}):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


# ------------------------------------------------------ options flow ----
async def test_options_flow_reports_the_missing_indicator_error(hass):
    """The error was computed and then dropped before reaching the form."""
    entry = await setup_entry(
        hass, {(CITY_CODE, URL_CODE.POLLUTION): FEATURES}, POLLUTION_ONLY)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "need_one_option"}


# ----------------------------------------------------------- unload ----
async def test_unload_entry(hass):
    entry = await setup_entry(
        hass, {(CITY_CODE, URL_CODE.POLLUTION): FEATURES}, POLLUTION_ONLY)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


# ------------------------------------------------------------ reauth ----
class RejectingApi(FakeApi):
    """Credentials no longer accepted."""

    async def get_data(self, code, url_code):
        raise InvalidAuthError("Atmo France rejected the credentials")


async def test_rejected_credentials_at_setup_ask_for_reauth(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=POLLUTION_ONLY, version=3)
    entry.add_to_hass(hass)

    with patch("custom_components.atmofrance.AtmoFranceDataApi",
               side_effect=lambda *a, **k: RejectingApi({})):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth"
               for flow in hass.config_entries.flow.async_progress())


async def test_rejected_credentials_while_polling_ask_for_reauth(hass):
    entry = await setup_entry(
        hass, {(CITY_CODE, URL_CODE.POLLUTION): FEATURES}, POLLUTION_ONLY)
    coordinator = coordinators(hass, entry)[CONF_POLLUTION_COORDINATOR]

    coordinator.api = RejectingApi({})
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._update_method()


async def test_a_missing_dataset_is_not_an_auth_problem(hass):
    """Only rejected credentials may interrupt the user."""
    entry = await setup_entry(
        hass, {(CITY_CODE, URL_CODE.POLLUTION): FEATURES}, POLLUTION_ONLY)
    coordinator = coordinators(hass, entry)[CONF_POLLUTION_COORDINATOR]

    coordinator.api = FakeApi({})
    with pytest.raises(UpdateFailed):
        await coordinator._update_method()


async def test_failed_auth_leaves_no_partial_state_behind(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=POLLUTION_ONLY, version=3)
    entry.add_to_hass(hass)

    with patch("custom_components.atmofrance.AtmoFranceDataApi",
               side_effect=lambda *a, **k: RejectingApi({})):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.entry_id not in hass.data.get(DOMAIN, {})


# ------------------------------------------- forecast entity creation ----
async def test_pollen_forecast_covers_two_days(hass):
    """Pollen publishes J+1 and J+2; pollution only publishes J+1."""
    options = {
        CONF_INCLUDE_POLLUTION: True, CONF_INCLUDE_POLLUTION_FORECAST: True,
        CONF_INCLUDE_POLLEN: True, CONF_INCLUDE_POLLEN_FORECAST: True,
    }
    coverage = {
        (CITY_CODE, URL_CODE.POLLUTION): FEATURES,
        (CITY_CODE, URL_CODE.POLLEN): FEATURES,
    }
    entry = await setup_entry(hass, coverage, options)
    assert entry.state is ConfigEntryState.LOADED

    names = {e.entity_id for e in hass.states.async_all("sensor")}
    j1 = [n for n in names if "j_1" in n]
    j2 = [n for n in names if "j_2" in n]

    assert j1, "no J+1 entity was created"
    assert j2, "pollen J+2 entities are missing"
    # Nothing on the pollution feed goes past J+1.
    assert not [n for n in j2 if "azote" in n or "ozone" in n or "pm10" in n]
