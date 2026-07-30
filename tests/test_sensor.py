"""Sensor tests.

Missing readings used to be forced to 0, which the recorder files as a real
measurement. They must now be None, and the label/colour lookups must cope
with that instead of raising KeyError.
"""
import logging

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmofrance.const import (
    DOMAIN,
    POLLEN_ALERT_SENSORS,
    POLLEN_CONC_SENSORS,
    POLLUTION_SENSORS,
)
from custom_components.atmofrance.sensor import (
    AtmoFrancePollenConcentrationEntity,
    AtmoFrancePollenLevelEntity,
    AtmoFrancePollutionEntity,
)

from .const import ENTRY_DATA, POLLUTION_ONLY

_LOGGER = logging.getLogger(__name__)

PM10 = next(d for d in POLLUTION_SENSORS if d.key == "code_pm10")
AMBROISIE = next(d for d in POLLEN_ALERT_SENSORS if d.key == "code_ambr")
CONC_GRAM = next(d for d in POLLEN_CONC_SENSORS if d.key == "conc_gram")


class StubApi:
    """Returns whatever get_key_value was told to return."""

    def __init__(self, values):
        self._values = values

    def get_key_value(self, key, shift=0):
        return self._values.get((key, shift), "")

    source = "ATMO Nouvelle-Aquitaine"
    last_update = "2026-07-30 09:00:00"
    type_zone = "commune"
    nom_zone = "Bordeaux"


def make_entity(hass, entity_class, description, values, shift=0):
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=POLLUTION_ONLY, version=3)
    entry.add_to_hass(hass)
    coordinator = DataUpdateCoordinator(
        hass, _LOGGER, name="test", config_entry=entry)
    coordinator.api = StubApi(values)
    return entity_class(hass, entry, description, coordinator, shift)


# ------------------------------------------ absent readings are None ----
async def test_absent_pollution_reading_is_none(hass):
    entity = make_entity(hass, AtmoFrancePollutionEntity, PM10, {})
    assert entity.native_value is None


async def test_absent_forecast_reading_is_none(hass):
    """The J+1 case: Atmo publishes tomorrow's index later in the day."""
    entity = make_entity(
        hass, AtmoFrancePollutionEntity, PM10,
        {("code_pm10", 0): 2}, shift=1)
    assert entity.native_value is None


async def test_json_null_is_treated_as_absent(hass):
    """int(None) used to raise TypeError inside a state property."""
    entity = make_entity(
        hass, AtmoFrancePollutionEntity, PM10, {("code_pm10", 0): None})
    assert entity.native_value is None


async def test_non_numeric_reading_is_none(hass):
    entity = make_entity(
        hass, AtmoFrancePollutionEntity, PM10, {("code_pm10", 0): "n/a"})
    assert entity.native_value is None


# ----------------------------------------------- present readings ----
@pytest.mark.parametrize("raw", [2, "2", 2.0])
async def test_pollution_reading_is_cast_to_int(hass, raw):
    entity = make_entity(
        hass, AtmoFrancePollutionEntity, PM10, {("code_pm10", 0): raw})
    assert entity.native_value == 2


async def test_pollen_level_is_cast_to_int(hass):
    entity = make_entity(
        hass, AtmoFrancePollenLevelEntity, AMBROISIE,
        {("code_ambr", 0): 3.0})
    assert entity.native_value == 3


async def test_a_real_zero_concentration_is_kept(hass):
    """0 grains is a measurement, not a missing value."""
    entity = make_entity(
        hass, AtmoFrancePollenConcentrationEntity, CONC_GRAM,
        {("conc_gram", 0): 0})
    assert entity.native_value == 0


async def test_absent_concentration_is_none(hass):
    entity = make_entity(
        hass, AtmoFrancePollenConcentrationEntity, CONC_GRAM, {})
    assert entity.native_value is None


# ------------------------------------------------------- attributes ----
async def test_attributes_survive_a_missing_value(hass):
    entity = make_entity(hass, AtmoFrancePollutionEntity, PM10, {})

    attributes = entity.extra_state_attributes

    assert attributes["Libellé"] is None
    assert attributes["Couleur"] is None
    assert attributes["Nom de la zone"] == "Bordeaux"


async def test_attributes_label_a_known_level(hass):
    entity = make_entity(
        hass, AtmoFrancePollutionEntity, PM10, {("code_pm10", 0): 4})

    attributes = entity.extra_state_attributes

    assert attributes["Libellé"] == "Mauvais"
    assert attributes["Couleur"] == "#ff5050"


async def test_attributes_survive_an_out_of_range_code(hass):
    """A code the level table does not know must not raise."""
    entity = make_entity(
        hass, AtmoFrancePollutionEntity, PM10, {("code_pm10", 0): 99})

    attributes = entity.extra_state_attributes

    assert attributes["Libellé"] is None
    assert attributes["Couleur"] is None


async def test_pollen_level_attributes(hass):
    entity = make_entity(
        hass, AtmoFrancePollenLevelEntity, AMBROISIE, {("code_ambr", 0): 1})

    assert entity.extra_state_attributes["Libellé"] == "Très faible"


# ------------------------------------------------------- forecast id ----
async def test_forecast_entities_get_a_distinct_name_and_id(hass):
    today = make_entity(hass, AtmoFrancePollutionEntity, PM10, {})
    tomorrow = make_entity(hass, AtmoFrancePollutionEntity, PM10, {}, shift=1)

    assert today._attr_name != tomorrow._attr_name
    assert today._attr_unique_id != tomorrow._attr_unique_id
    assert tomorrow._attr_name.endswith("J+1")
