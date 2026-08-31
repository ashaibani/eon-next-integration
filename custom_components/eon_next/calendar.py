#!/usr/bin/env python3

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Expose the Electricity unit-rate windows as a calendar."""

    store = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for account_number, domain_coordinators in store["coordinators"].items():
        account = domain_coordinators["rates"].account

        if account.electricity_mpan == None:
            continue

        entities.append(RatesCalendarEntity(domain_coordinators["rates"], config_entry))

    async_add_entities(entities)


class RatesCalendarEntity(CoordinatorEntity, CalendarEntity):
    """Calendar of merged unit-rate windows. Day/night tariffs appear as
    long blocks rather than one event per half hour."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._entry = entry

        self._attr_name = self.account.account_number + " Electricity Rates"
        self._attr_unique_id = self.account.account_number + "__" + "rates_calendar"
    

    @property
    def account(self):
        return self.coordinator.data
    

    @property
    def event(self):
        """The current rate block, or the next one starting in the future."""
        windows = self.account.get_merged_rate_windows()
        if len(windows) == 0:
            return None

        now = datetime.now(tz=timezone.utc)

        for window in windows:
            start = self._parse(window["from"])
            end = self._parse_end(window)
            if start <= now and now < end:
                return self._to_event(window, start, end)

        for window in windows:
            start = self._parse(window["from"])
            if start > now:
                end = self._parse_end(window)
                return self._to_event(window, start, end)

        return None
    

    def _parse(self, iso: str) -> datetime:
        parsed = datetime.fromisoformat(str(iso))
        if parsed.tzinfo == None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    

    def _parse_end(self, window) -> datetime:
        if window.get("to") != None:
            return self._parse(window["to"])
        return self._parse(window["from"]) + timedelta(hours=24)
    

    def _to_event(self, window, start, end) -> CalendarEvent:
        tariff = (self.account.tariff.get("electricity") or {}).get("displayName")

        summary = str(window["pence"]) + "p/kWh"
        description = "E.ON Next electricity unit rate"
        if tariff != None:
            description = tariff + " - " + str(window["pence"]) + " p/kWh"

        return CalendarEvent(
            start=start,
            end=end,
            summary=summary,
            description=description,
        )
    

    async def async_get_events(self, hass, start_date, end_date):
        """Return rate windows overlapping the requested range."""
        events = []

        for window in self.account.get_merged_rate_windows():
            start = self._parse(window["from"])
            end = self._parse_end(window)

            if start < end_date and end > start_date:
                events.append(self._to_event(window, start, end))

        return events
