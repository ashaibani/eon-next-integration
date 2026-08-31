#!/usr/bin/env python3

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _account_device(account) -> DeviceInfo:
    from .sensor import _account_device
    return _account_device(account)


def _meter_device(meter) -> DeviceInfo:
    from .sensor import _meter_device
    return _meter_device(meter)


class EonNextBinaryBase(CoordinatorEntity, BinarySensorEntity):
    """Base for binary sensors that read account state from a coordinator."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._entry = entry

    @property
    def account(self):
        return self.coordinator.data


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Setup alerting binary sensors from a config entry."""

    store = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for account_number, domain_coordinators in store["coordinators"].items():

        account = domain_coordinators["balance"].account

        # Alerting entities are always on (they exist to warn), independent
        # of the sensor visibility toggles.
        if account.electricity_mpan != None:
            entities.append(NightRateActiveBinarySensor(
                domain_coordinators["rates"], config_entry))

        for meter in account.meters:
            if meter.is_prepay() == True:
                entities.append(PrepayLowCreditBinarySensor(
                    domain_coordinators["prepay"], config_entry, meter))

    async_add_entities(entities)


class PrepayLowCreditBinarySensor(EonNextBinaryBase):
    """On when the prepay credit has fallen to or below the configured
    threshold - wire this to a notification automation."""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry)
        self.meter = meter

        self._attr_name = self.meter.get_serial() + " Prepay Low Credit"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_icon = "mdi:battery-alert"
        self._attr_unique_id = self.meter.get_serial() + "__" + "prepay_low_credit"
        self._attr_device_info = _meter_device(meter)
    

    def _credit_pence(self):
        if self.meter.snapshot_credit_pence != None:
            return self.meter.snapshot_credit_pence
        return self.meter.prepay_credit_pence
    

    @property
    def is_on(self):
        credit = self._credit_pence()
        if credit == None:
            return None
        return credit <= self.account.low_credit_pence
    

    @property
    def extra_state_attributes(self):
        return {
            "threshold_pence": self.account.low_credit_pence,
            "credit_pence": self._credit_pence(),
            "as_at": self.meter.snapshot_as_at,
        }


class NightRateActiveBinarySensor(EonNextBinaryBase):
    """On while the cheaper day/night window is active. With a single flat
    rate there is no cheaper window, so the sensor stays off."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Night Rate Active"
        self._attr_icon = "mdi:weather-night"
        self._attr_unique_id = self.account.account_number + "__" + "night_rate_active"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def is_on(self):
        info = self.account.get_current_rate_info()
        if info == {}:
            return None

        minimum = info.get("min_pence_today")
        if minimum == None:
            return None

        # Flat tariffs have only one rate value; nothing is ever "night"
        maximum = info.get("max_pence_today")
        if maximum != None and minimum == maximum:
            return False

        return info["pence"] == minimum
    

    @property
    def extra_state_attributes(self):
        info = self.account.get_current_rate_info()
        if info == {}:
            return {}

        return {
            "current_rate_pence": info.get("pence"),
            "window_from": info.get("from"),
            "window_to": info.get("to"),
            "next_rate_pence": info.get("next_pence"),
            "next_rate_from": info.get("next_from"),
        }
