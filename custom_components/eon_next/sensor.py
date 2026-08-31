#!/usr/bin/env python3

import logging
from datetime import datetime as dt

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass
)
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfVolume
)
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    OPTION_SHOW_BALANCE,
    OPTION_SHOW_PREPAY,
    OPTION_SHOW_RATES,
    OPTION_SHOW_USAGE,
)
from .eonnext import METER_TYPE_GAS, METER_TYPE_ELECTRIC

_LOGGER = logging.getLogger(__name__)


def _option_enabled(entry, key: str, default: bool) -> bool:
    try:
        return bool(entry.options.get(key, default))
    except (TypeError, ValueError):
        return default


def _fuel_label(meter) -> str:
    if meter.get_type() == METER_TYPE_GAS:
        return "Gas"
    if meter.get_type() == METER_TYPE_ELECTRIC:
        return "Electricity"
    return "Meter"


def _account_device(account) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, account.account_number)},
        name="E.ON Next " + account.account_number,
        manufacturer="E.ON Next",
    )


def _meter_device(meter) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, "meter_" + meter.get_serial())},
        name="E.ON Next Meter " + meter.get_serial(),
        manufacturer="E.ON Next",
        via_device=(DOMAIN, meter.account.account_number),
    )


class EonNextCoordinatorEntity(CoordinatorEntity, SensorEntity):
    """Base for entities that read account state from a coordinator.

    Entities never call the API: all fetching happens in the coordinator,
    and an API failure marks the coordinator - and so every one of these
    entities - unavailable, with the last good state preserved. This is the
    graceful-failure pattern used by the Octopus Energy integration.
    """

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._entry = entry

    @property
    def account(self):
        return self.coordinator.data


class EonNextMeterCoordinatorEntity(EonNextCoordinatorEntity):
    """Base for per-meter entities (readings domain coordinator)."""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry)
        self.meter = meter


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Setup sensors from a config entry created in the integrations UI."""

    store = hass.data[DOMAIN][config_entry.entry_id]
    coordinators_by_account = store["coordinators"]

    show_balance = _option_enabled(config_entry, OPTION_SHOW_BALANCE, True)
    show_rates = _option_enabled(config_entry, OPTION_SHOW_RATES, True)
    show_usage = _option_enabled(config_entry, OPTION_SHOW_USAGE, True)
    show_prepay = _option_enabled(config_entry, OPTION_SHOW_PREPAY, True)

    entities = []
    for account_number, domain_coordinators in coordinators_by_account.items():

        balance_coordinator = domain_coordinators["balance"]
        rates_coordinator = domain_coordinators["rates"]
        tariff_coordinator = domain_coordinators["tariff"]
        readings_coordinator = domain_coordinators["readings"]
        prepay_coordinator = domain_coordinators["prepay"]

        account = balance_coordinator.account

        if show_balance == True:
            entities.append(AccountBalanceSensor(balance_coordinator, config_entry))

        if show_rates == True and account.electricity_mpan != None:
            entities.append(ElectricityPriceSensor(rates_coordinator, config_entry))
            entities.append(ElectricityNextRateSensor(rates_coordinator, config_entry))
            entities.append(ElectricityStandingChargeSensor(tariff_coordinator, config_entry))
            entities.append(ElectricityTariffNameSensor(tariff_coordinator, config_entry))

        if show_rates == True and account.gas_mprn != None:
            entities.append(GasPriceSensor(tariff_coordinator, config_entry))
            entities.append(GasStandingChargeSensor(tariff_coordinator, config_entry))

        if show_usage == True and account.electricity_mpan != None:
            entities.append(BillingDocumentSensor(tariff_coordinator, config_entry))
            entities.append(AnnualEstimatesSensor(tariff_coordinator, config_entry))

        for meter in account.meters:

            if meter.latest_reading != None:
                entities.append(LatestReadingDateSensor(readings_coordinator, config_entry, meter))

                if meter.get_type() == METER_TYPE_ELECTRIC:
                    entities.append(LatestElectricKwhSensor(readings_coordinator, config_entry, meter))

                    if show_usage == True:
                        entities.append(UsageDaySensor(readings_coordinator, config_entry, meter))
                        entities.append(UsageNightSensor(readings_coordinator, config_entry, meter))
                        entities.append(UsageTotalSensor(readings_coordinator, config_entry, meter))
                        entities.append(CostDaySensor(rates_coordinator, config_entry, meter))

                if meter.get_type() == METER_TYPE_GAS:
                    entities.append(LatestGasCubicMetersSensor(readings_coordinator, config_entry, meter))
                    entities.append(LatestGasKwhSensor(readings_coordinator, config_entry, meter))

            if show_prepay == True and meter.is_prepay() == True:
                entities.append(PrepayCreditSensor(prepay_coordinator, config_entry, meter))
                entities.append(PrepayDebtSensor(prepay_coordinator, config_entry, meter))
                entities.append(PrepayEmergencyCreditSensor(prepay_coordinator, config_entry, meter))
                entities.append(PrepayCreditUpdatedSensor(prepay_coordinator, config_entry, meter))

    async_add_entities(entities)



class AccountBalanceSensor(EonNextCoordinatorEntity):
    """Current account ledger balance in pounds"""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Balance"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:cash"
        self._attr_unique_id = self.account.account_number + "__" + "balance"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def native_value(self):
        return self.account.get_balance_amount()


class ElectricityPriceSensor(EonNextCoordinatorEntity):
    """Current electricity unit rate in pounds per kWh. Select this as the
    "entity with current price" on the Energy dashboard."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Electricity Rate"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP/kWh"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 4
        self._attr_icon = "mdi:cash-multiple"
        self._attr_unique_id = self.account.account_number + "__" + "electricity_rate"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def native_value(self):
        info = self.account.get_current_rate_info()
        if info == {}:
            return None
        return round(info["pence"] / 100.0, 4)
    

    @property
    def extra_state_attributes(self):
        info = self.account.get_current_rate_info()

        attributes = {
            "rate_windows_loaded": len(self.account.rate_windows),
        }

        if info == {}:
            return attributes

        attributes.update({
            "valid_from": info.get("from"),
            "valid_to": info.get("to"),
            "rate_in_pence": info.get("pence"),
            "rate_block_from": info.get("block_from"),
            "rate_block_to": info.get("block_to"),
        })

        if info.get("next_pence") != None:
            attributes["next_rate_in_pence"] = info.get("next_pence")
            attributes["next_rate_from"] = info.get("next_from")

        if info.get("min_pence_today") != None:
            attributes["min_rate_today_pence"] = info.get("min_pence_today")
            attributes["max_rate_today_pence"] = info.get("max_pence_today")
            attributes["average_rate_today_pence"] = info.get("average_pence_today")

        tariff = self.account.tariff.get("electricity") or {}
        if tariff.get("displayName") != None:
            attributes["tariff"] = tariff.get("displayName")

        return attributes


class ElectricityNextRateSensor(EonNextCoordinatorEntity):
    """The next unit rate after the current block ends - for "night rate
    starts at" style automations."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Electricity Next Rate"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP/kWh"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 4
        self._attr_icon = "mdi:cash-clock"
        self._attr_unique_id = self.account.account_number + "__" + "electricity_next_rate"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def native_value(self):
        info = self.account.get_current_rate_info()
        if info == {} or info.get("next_pence") == None:
            return None
        return round(info["next_pence"] / 100.0, 4)
    

    @property
    def extra_state_attributes(self):
        info = self.account.get_current_rate_info()
        if info == {}:
            return {}
        
        return {
            "next_rate_from": info.get("next_from"),
            "current_rate_in_pence": info.get("pence"),
            "current_rate_until": info.get("block_to"),
        }


class ElectricityStandingChargeSensor(EonNextCoordinatorEntity):
    """Daily electricity standing charge in pounds"""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Electricity Standing Charge"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP/day"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 4
        self._attr_icon = "mdi:calendar-clock"
        self._attr_unique_id = self.account.account_number + "__" + "electricity_standing_charge"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def native_value(self):
        tariff = self.account.tariff.get("electricity") or {}
        pence = tariff.get("standingCharge")
        if pence == None:
            return None
        return round(pence / 100.0, 4)
    

    @property
    def extra_state_attributes(self):
        tariff = self.account.tariff.get("electricity") or {}
        return {
            "tariff": tariff.get("displayName"),
            "product_code": tariff.get("productCode"),
            "tariff_code": tariff.get("tariffCode"),
            "standing_charge_in_pence": tariff.get("standingCharge"),
        }


class ElectricityTariffNameSensor(EonNextCoordinatorEntity):
    """Human-readable name of the active electricity tariff"""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Electricity Tariff"
        self._attr_icon = "mdi:tag-text-outline"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_unique_id = self.account.account_number + "__" + "electricity_tariff"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def native_value(self):
        tariff = self.account.tariff.get("electricity") or {}
        return tariff.get("displayName")
    

    @property
    def extra_state_attributes(self):
        tariff = self.account.tariff.get("electricity") or {}
        return {
            "full_name": tariff.get("fullName"),
            "product_code": tariff.get("productCode"),
            "tariff_code": tariff.get("tariffCode"),
            "agreement_from": tariff.get("agreement_from"),
            "agreement_to": tariff.get("agreement_to"),
        }


class GasPriceSensor(EonNextCoordinatorEntity):
    """Current gas unit rate in pounds per kWh from the active tariff"""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Gas Rate"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP/kWh"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 4
        self._attr_icon = "mdi:cash-multiple"
        self._attr_unique_id = self.account.account_number + "__" + "gas_rate"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def native_value(self):
        tariff = self.account.tariff.get("gas") or {}
        pence = tariff.get("unitRate")
        if pence == None:
            return None
        return round(pence / 100.0, 4)
    

    @property
    def extra_state_attributes(self):
        tariff = self.account.tariff.get("gas") or {}
        return {
            "tariff": tariff.get("displayName"),
            "rate_in_pence": tariff.get("unitRate"),
        }


class GasStandingChargeSensor(EonNextCoordinatorEntity):
    """Daily gas standing charge in pounds"""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Gas Standing Charge"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP/day"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 4
        self._attr_icon = "mdi:calendar-clock"
        self._attr_unique_id = self.account.account_number + "__" + "gas_standing_charge"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def native_value(self):
        tariff = self.account.tariff.get("gas") or {}
        pence = tariff.get("standingCharge")
        if pence == None:
            return None
        return round(pence / 100.0, 4)
    

    @property
    def extra_state_attributes(self):
        tariff = self.account.tariff.get("gas") or {}
        return {
            "tariff": tariff.get("displayName"),
            "standing_charge_in_pence": tariff.get("standingCharge"),
        }


class BillingDocumentSensor(EonNextCoordinatorEntity):
    """The newest billing document (bill or statement) on the account"""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Latest Billing Document"
        self._attr_icon = "mdi:file-document-outline"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_unique_id = self.account.account_number + "__" + "latest_billing_document"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def native_value(self):
        doc = self.account.latest_document
        if doc == None:
            return None
        label = "Bill" if doc.get("document_type") == "PeriodBasedDocumentType" else "Statement"
        return label + " issued " + str(doc.get("issued"))
    

    @property
    def extra_state_attributes(self):
        doc = self.account.latest_document
        if doc == None:
            return {}
        return {
            "document_type": doc.get("document_type"),
            "id": doc.get("id"),
            "period_from": doc.get("from"),
            "period_to": doc.get("to"),
            "issued": doc.get("issued"),
        }


class AnnualEstimatesSensor(EonNextCoordinatorEntity):
    """E.ON's estimate of your annual consumption (medium profile)
    with the low/medium/high spread in attributes"""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)

        self._attr_name = self.account.account_number + " Annual Consumption Estimate"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_icon = "mdi:chart-bar"
        self._attr_unique_id = self.account.account_number + "__" + "annual_estimates"
        self._attr_device_info = _account_device(self.account)
    

    @property
    def native_value(self):
        medium = self.account.annual_estimates.get("medium") or {}
        # Two-register meters: day+night. Single-register: standard.
        day = medium.get("elecAnnualConsumptionDay")
        night = medium.get("elecAnnualConsumptionNight")
        if day != None and night != None:
            return day + night
        return medium.get("elecAnnualConsumptionStandard")
    

    @property
    def extra_state_attributes(self):
        estimates = self.account.annual_estimates
        low = estimates.get("low") or {}
        medium = estimates.get("medium") or {}
        high = estimates.get("high") or {}
        return {
            "annual_kwh_medium": medium,
            "annual_kwh_low": low,
            "annual_kwh_high": high,
            "note": "E.ON's own estimates - annual kWh by band; gas included in gasAnnualConsumption",
        }


class UsageDaySensor(EonNextMeterCoordinatorEntity):
    """Day-register usage for the latest published reading day, with the
    standing-charge-inclusive day/night cost in attributes."""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry, meter)

        self._attr_name = self.meter.get_serial() + " Electricity Usage Day"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:weather-sunny"
        self._attr_unique_id = self.meter.get_serial() + "__" + "usage_day"
        self._attr_device_info = _meter_device(meter)
    

    def _entry(self):
        return self.account.get_latest_usage()
    

    @property
    def native_value(self):
        entry = self._entry()
        if entry == None:
            return None
        return entry.get("day_kwh")
    

    @property
    def extra_state_attributes(self):
        entry = self._entry()
        if entry == None:
            return {}
        return {
            "for_date": entry["date"],
            "cost_gbp": entry.get("cost_gbp"),
            "cost_basis": entry.get("cost_basis"),
        }


class UsageNightSensor(EonNextMeterCoordinatorEntity):
    """Night-register usage for the latest day"""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry, meter)

        self._attr_name = self.meter.get_serial() + " Electricity Usage Night"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:weather-night"
        self._attr_unique_id = self.meter.get_serial() + "__" + "usage_night"
        self._attr_device_info = _meter_device(meter)
    

    @property
    def native_value(self):
        entry = self._entry()
        if entry == None:
            return None
        return entry.get("night_kwh")


class UsageTotalSensor(EonNextMeterCoordinatorEntity):
    """Total usage for the latest published reading day, with the
    standing-charge-inclusive estimated cost"""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry, meter)

        self._attr_name = self.meter.get_serial() + " Electricity Usage Total"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:lightning-bolt-outline"
        self._attr_unique_id = self.meter.get_serial() + "__" + "usage_total"
        self._attr_device_info = _meter_device(meter)
    

    def _entry(self):
        return self.account.get_latest_usage()
    

    @property
    def native_value(self):
        entry = self._entry()
        if entry == None:
            return None
        return entry.get("total_kwh")
    

    @property
    def extra_state_attributes(self):
        entry = self._entry()
        if entry == None:
            return {}
        return {
            "for_date": entry["date"],
            "day_kwh": entry.get("day_kwh"),
            "night_kwh": entry.get("night_kwh"),
            "cost_gbp": entry.get("cost_gbp"),
            "cost_basis": entry.get("cost_basis"),
        }


class CostDaySensor(EonNextMeterCoordinatorEntity):
    """Estimated cost for the latest published reading day: day units x day
    band, night units x night band, plus the daily standing charge.
    Labelled an estimate because the server-side per-day costing query
    (gbrCostOfUsage) is not available to prepay products."""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry, meter)

        self._attr_name = self.meter.get_serial() + " Electricity Cost Day (Est)"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:cash-check"
        self._attr_unique_id = self.meter.get_serial() + "__" + "cost_day_est"
        self._attr_device_info = _meter_device(meter)

    @property
    def native_value(self):
        entry = self.account.get_latest_usage()
        if entry == None:
            return None
        return entry.get("cost_gbp")

    @property
    def extra_state_attributes(self):
        entry = self.account.get_latest_usage()
        if entry == None:
            return {}
        return {
            "for_date": entry["date"],
            "day_kwh": entry.get("day_kwh"),
            "night_kwh": entry.get("night_kwh"),
            "total_kwh": entry.get("total_kwh"),
            "cost_basis": entry.get("cost_basis"),
            "estimate": True,
        }


class PrepayCreditSensor(EonNextCoordinatorEntity):
    """Smart prepay meter credit - the figure the E.ON Next app shows."""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry)
        self.meter = meter

        self._attr_name = self.meter.get_serial() + " " + _fuel_label(meter) + " Prepay Credit"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:lightning-bolt-circle"
        self._attr_unique_id = self.meter.get_serial() + "__" + "prepay_credit"
        self._attr_device_info = _meter_device(meter)
    

    @property
    def native_value(self):
        return self.meter.get_prepay_credit_gbp()
    

    @property
    def extra_state_attributes(self):
        return {
            "source": self.meter.get_prepay_credit_source(),
            "snapshot_credit_pence": self.meter.snapshot_credit_pence,
            "ledger_credit_pence": self.meter.prepay_credit_pence,
            "meter_balance_pence": self.meter.snapshot_meter_balance_pence,
            "as_at": self.meter.snapshot_as_at,
            "device_ids": self.meter.device_ids,
            "payment_mode": self.meter.payment_mode,
        }


class PrepayDebtSensor(EonNextCoordinatorEntity):
    """Smart prepay meter debt"""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry)
        self.meter = meter

        self._attr_name = self.meter.get_serial() + " " + _fuel_label(meter) + " Prepay Debt"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:cash-minus"
        self._attr_unique_id = self.meter.get_serial() + "__" + "prepay_debt"
        self._attr_device_info = _meter_device(meter)
    

    @property
    def native_value(self):
        return self.meter.get_prepay_debt_gbp()


class PrepayEmergencyCreditSensor(EonNextCoordinatorEntity):
    """Emergency credit available on the prepay meter"""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry)
        self.meter = meter

        self._attr_name = self.meter.get_serial() + " " + _fuel_label(meter) + " Emergency Credit"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "GBP"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:flash-alert-outline"
        self._attr_unique_id = self.meter.get_serial() + "__" + "prepay_emergency_credit"
        self._attr_device_info = _meter_device(meter)
    

    @property
    def native_value(self):
        return self.meter.get_prepay_emergency_credit_gbp()


class PrepayCreditUpdatedSensor(EonNextCoordinatorEntity):
    """When the meter last reported its balance (the snapshot's asAt)."""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry)
        self.meter = meter

        self._attr_name = self.meter.get_serial() + " " + _fuel_label(meter) + " Prepay Updated"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:clock-check-outline"
        self._attr_unique_id = self.meter.get_serial() + "__" + "prepay_updated"
        self._attr_device_info = _meter_device(meter)
    

    @property
    def native_value(self):
        raw = self.meter.snapshot_as_at
        if raw == None:
            return None
        
        try:
            return dt.fromisoformat(str(raw))
        except ValueError:
            return None


class LatestReadingDateSensor(EonNextMeterCoordinatorEntity):
    """Date of latest meter reading"""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry, meter)

        self._attr_name = self.meter.get_serial() + " Reading Date"
        self._attr_device_class = SensorDeviceClass.DATE
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:calendar"
        self._attr_unique_id = self.meter.get_serial() + "__" + "reading_date"
        self._attr_device_info = _meter_device(meter)
    

    @property
    def native_value(self):
        return self.meter.latest_reading_date


class LatestElectricKwhSensor(EonNextMeterCoordinatorEntity):
    """Cumulative electricity register. total_increasing makes it usable as
    the "grid import" source on the Energy dashboard."""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry, meter)

        self._attr_name = self.meter.get_serial() + " Electricity"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:meter-electric-outline"
        self._attr_unique_id = self.meter.get_serial() + "__" + "electricity_kwh"
        self._attr_device_info = _meter_device(meter)
    

    @property
    def native_value(self):
        return self.meter.latest_reading


class LatestGasKwhSensor(EonNextMeterCoordinatorEntity):
    """Cumulative gas register converted to kWh. total_increasing makes it
    usable as the "gas consumption" source on the Energy dashboard."""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry, meter)

        self._attr_name = self.meter.get_serial() + " Gas kWh"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:meter-gas-outline"
        self._attr_unique_id = self.meter.get_serial() + "__" + "gas_kwh"
        self._attr_device_info = _meter_device(meter)
    

    @property
    def native_value(self):
        if self.meter.latest_reading == None:
            return None
        
        m3 = self.meter.latest_reading
        kwh = m3 * 1.02264
        kwh = kwh * self.account.gas_calorific_value
        kwh = kwh / 3.6
        return round(kwh, 2)


class LatestGasCubicMetersSensor(EonNextMeterCoordinatorEntity):
    """Latest gas meter reading in cubic meters (raw register)"""

    def __init__(self, coordinator, entry, meter):
        super().__init__(coordinator, entry, meter)

        self._attr_name = self.meter.get_serial() + " Gas"
        self._attr_device_class = SensorDeviceClass.GAS
        self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:meter-gas-outline"
        self._attr_unique_id = self.meter.get_serial() + "__" + "gas_m3"
        self._attr_device_info = _meter_device(meter)
    

    @property
    def native_value(self):
        return self.meter.latest_reading
