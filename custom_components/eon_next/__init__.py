#!/usr/bin/env python3

import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import (
    DOMAIN,
    OPTION_BALANCE_MINUTES,
    OPTION_PREPAY_MINUTES,
    OPTION_RATES_MINUTES,
    MIN_BALANCE_REFRESH_MINUTES,
    MIN_PREPAY_REFRESH_MINUTES,
    MIN_RATES_REFRESH_MINUTES,
)
from .coordinators import AccountDomainCoordinator, PrepayCoordinator
from .eonnext import EonNext, EonNextApiError

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "calendar"]

CONF_EMAIL = "email"
CONF_PASSWORD = "password"


def _option_minutes(entry, key: str, default: int, minimum: int) -> int:
    """Read an interval option, clamped to its configured minimum."""
    try:
        value = int(entry.options.get(key, default))
    except (TypeError, ValueError):
        value = default
    
    if value < minimum:
        value = minimum
    
    return value


def _build_account_coordinators(hass: HomeAssistant, entry, account):
    """Create the per-account coordinator set from the entry's options."""
    balance_minutes = _option_minutes(entry, OPTION_BALANCE_MINUTES, 15, MIN_BALANCE_REFRESH_MINUTES)
    rates_minutes = _option_minutes(entry, OPTION_RATES_MINUTES, 60, MIN_RATES_REFRESH_MINUTES)
    prepay_minutes = _option_minutes(entry, OPTION_PREPAY_MINUTES, 5, MIN_PREPAY_REFRESH_MINUTES)

    balance = AccountDomainCoordinator(
        hass, account,
        lambda: account.refresh_balance(force=True),
        name="eon_next_balance_" + account.account_number,
        config_entry=entry,
    )
    balance.set_interval_seconds(balance_minutes * 60)

    rates = AccountDomainCoordinator(
        hass, account,
        lambda: account.refresh_rates(force=True),
        name="eon_next_rates_" + account.account_number,
        config_entry=entry,
    )
    rates.set_interval_seconds(rates_minutes * 60)

    tariff = AccountDomainCoordinator(
        hass, account,
        lambda: account.refresh_tariff(force=True),
        name="eon_next_tariff_" + account.account_number,
        config_entry=entry,
    )
    # Tariff details change rarely; 6 hours matches the client gate
    tariff.set_interval_seconds(6 * 3600)

    readings = AccountDomainCoordinator(
        hass, account,
        lambda: account.refresh_readings(force=False),
        name="eon_next_readings_" + account.account_number,
        config_entry=entry,
    )
    # Hourly cadence: the meters themselves only fetch when the day changes,
    # so this is cheap. Hourly polling keeps register history accurate.
    readings.set_interval_seconds(3600)

    prepay = PrepayCoordinator(
        hass, account,
        lambda: account.refresh_prepay_balances(force=True),
        name="eon_next_prepay_" + account.account_number,
        entry_id=entry.entry_id,
        config_entry=entry,
    )
    prepay.set_interval_seconds(prepay_minutes * 60)

    return {
        "balance": balance,
        "rates": rates,
        "tariff": tariff,
        "readings": readings,
        "prepay": prepay,
    }


async def async_setup(hass: HomeAssistant, config):
    """Component-level setup, runs once at boot before any config entry.

    Registering the service here (in addition to the entry path's guarded
    registration) guarantees eon_next.refresh exists even while an entry
    is disabled, mid-retry, or re-authenticating.
    """
    _register_refresh_service(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry):
    """Set up platform from a ConfigEntry."""
    hass.data.setdefault(DOMAIN, {})

    api = EonNext()

    try:
        success = await api.login_with_username_and_password(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    except EonNextApiError as exc:
        await api.close()
        raise ConfigEntryNotReady("Could not reach the E.ON Next API") from exc

    if success == False:
        await api.close()
        raise ConfigEntryAuthFailed("Credentials rejected by the E.ON Next API")

    entry_store = {
        "api": api,
        "entry_id": entry.entry_id,
        "coordinators": {},
    }

    for account in api.accounts:
        account.apply_options(dict(entry.options))

        coordinators = _build_account_coordinators(hass, entry, account)
        entry_store["coordinators"][account.account_number] = coordinators

        # Populate each domain before the platforms load, so entities appear
        # with real values. A domain that cannot fill (API down) retries via
        # ConfigEntryNotReady rather than half-working.
        try:
            for name in ("balance", "prepay", "rates", "tariff", "readings"):
                await coordinators[name].async_config_entry_first_refresh()
        except Exception as exc:
            for coordinator in coordinators.values():
                await coordinator.async_shutdown()
            await api.close()
            raise ConfigEntryNotReady(str(exc)) from exc

    hass.data[DOMAIN][entry.entry_id] = entry_store

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Domain-level service: force every coordinator refresh now (registered
    # once; the handler sweeps every loaded entry)
    if hass.services.has_service(DOMAIN, "refresh") == False:
        _register_refresh_service(hass)

    return True


def _register_refresh_service(hass: HomeAssistant):
    """Register the eon_next.refresh service."""
    async def async_handle_refresh(call):
        for store in hass.data.get(DOMAIN, {}).values():
            if isinstance(store, dict) and "coordinators" in store:
                for coordinators in store["coordinators"].values():
                    for coordinator in coordinators.values():
                        await coordinator.async_force_refresh_now()

    hass.services.async_register(DOMAIN, "refresh", async_handle_refresh)

    return True


async def _async_update_listener(hass, entry):
    """Reload the entry when its options change so new intervals apply."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry):
    """Unload a config entry."""
    store = hass.data[DOMAIN].get(entry.entry_id)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok == False or store == None:
        return unload_ok

    for coordinators in store["coordinators"].values():
        for coordinator in coordinators.values():
            await coordinator.async_shutdown()

    api = store.get("api")
    if api != None:
        await api.close()

    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
