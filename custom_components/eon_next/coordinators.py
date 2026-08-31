"""Data update coordinators for the E.ON Next integration.

One coordinator per data domain (balance, tariff, rates, prepay, readings).
Each owns its refresh interval and calls the client with force=True so the
client-side time gates never double-gate a coordinator-driven cycle.
Entities observe coordinator data instead of polling the API themselves.
"""

import logging
from datetime import datetime, timedelta, timezone

import homeassistant.helpers.issue_registry as ir
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    PREPAY_STALE_AFTER_HOURS,
    PREPAY_MAX_SNAPSHOT_MISSES,
)
from .eonnext import EonNextApiError

_LOGGER = logging.getLogger(__name__)


class AccountDomainCoordinator(DataUpdateCoordinator):
    """Runs one force-refresh per cycle over an EnergyAccount object.

    coordinator.data is the account itself, refreshed in place - so the
    value-shaping helpers on the account stay the single source of truth
    and entities remain thin observers.
    """

    def __init__(self, hass: HomeAssistant, account, refresh_coro, name: str, config_entry=None):
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=None,  # set by setup once options are known
            config_entry=config_entry,
        )
        self.account = account
        self._refresh_coro = refresh_coro

    def set_interval_seconds(self, seconds) -> None:
        if seconds == None or seconds <= 0:
            return
        self.update_interval = timedelta(seconds=int(seconds))

    async def _async_update_data(self):
        try:
            await self._refresh_coro()
        except EonNextApiError as exc:
            raise UpdateFailed(str(exc)) from exc
        except Exception as exc:
            raise UpdateFailed("Unexpected error: " + str(exc)) from exc
        return self.account

    async def async_force_refresh_now(self) -> bool:
        """Run the refresh immediately regardless of interval (refresh service)."""
        try:
            await self._refresh_coro()
        except EonNextApiError as exc:
            _LOGGER.warning("Forced refresh failed for %s: %s", self.name, exc)
            self.last_update_success = False
            self.async_update_listeners()
            return False

        self.last_update_success = True
        self.last_updated = datetime.now(tz=timezone.utc)
        self.async_update_listeners()
        return True


class PrepayCoordinator(AccountDomainCoordinator):
    """Prepay domain coordinator with smart-meter staleness repairs.

    A smart PAYG meter that stops reporting its balance is an operational
    failure (top-ups stop landing), so missing or aged snapshots raise a
    Home Assistant repair issue rather than just sitting as old data.
    """

    def __init__(self, hass: HomeAssistant, account, refresh_coro, name: str, entry_id: str, config_entry=None):
        super().__init__(hass, account, refresh_coro, name, config_entry=config_entry)
        self._entry_id = entry_id
        self._missing_counts = {}

    async def _async_update_data(self):
        data = await super()._async_update_data()
        self._evaluate_prepay_health()
        return data

    def _evaluate_prepay_health(self) -> None:
        now = datetime.now(tz=timezone.utc)

        for meter in self.account.meters:
            if len(meter.device_ids) == 0:
                continue

            issue_key = "prepay_stale_" + meter.get_serial()
            stale_reason = None

            if meter.snapshot_as_at == None:
                count = self._missing_counts.get(issue_key, 0) + 1
                self._missing_counts[issue_key] = count

                if meter.has_prepay_snapshot_had_value() == True and count >= PREPAY_MAX_SNAPSHOT_MISSES:
                    stale_reason = "stopped reporting"
            else:
                self._missing_counts[issue_key] = 0

                try:
                    as_at = datetime.fromisoformat(str(meter.snapshot_as_at))
                    if as_at.tzinfo == None:
                        as_at = as_at.replace(tzinfo=timezone.utc)

                    if (now - as_at).total_seconds() > PREPAY_STALE_AFTER_HOURS * 3600:
                        stale_reason = "last reported " + str(meter.snapshot_as_at)
                except ValueError:
                    stale_reason = "reported an unreadable timestamp"

            if stale_reason != None:
                ir.async_create_issue(
                    self.hass,
                    "eon_next",
                    issue_key,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="prepay_stale",
                    translation_placeholders={
                        "serial": meter.get_serial(),
                        "reason": stale_reason,
                    },
                )
            else:
                ir.async_delete_issue(self.hass, "eon_next", issue_key)
