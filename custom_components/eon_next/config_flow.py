"""Config and options flows for the E.ON Next integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    OPTION_SHOW_BALANCE,
    OPTION_SHOW_PREPAY,
    OPTION_SHOW_RATES,
    OPTION_SHOW_USAGE,
    OPTION_LOW_CREDIT_PENCE,
    OPTION_GAS_CALORIFIC_VALUE,
    OPTION_BALANCE_MINUTES,
    OPTION_RATES_MINUTES,
    OPTION_PREPAY_MINUTES,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_LOW_CREDIT_PENCE,
)
from .eonnext import EonNext, EonNextApiError

_LOGGER = logging.getLogger(__name__)

CONF_EMAIL = "email"
CONF_PASSWORD = "password"


async def validate_credentials(hass, data: dict) -> dict:
    """Validate the login against the E.ON Next API.

    Raises ValueError("invalid_auth") or ValueError("cannot_connect").
    """
    api = EonNext()

    try:
        success = await api.login_with_username_and_password(
            data[CONF_EMAIL], data[CONF_PASSWORD], False)
    except EonNextApiError as exc:
        raise ValueError("cannot_connect") from exc
    finally:
        await api.close()

    if success == False:
        raise ValueError("invalid_auth")

    return {"title": "E.ON Next"}


class EonNextConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial E.ON Next config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Provide the options flow handler for this entry."""
        return EonNextOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Invoked when a user initiates the flow via the UI."""
        errors = {}

        if user_input is not None:
            try:
                info = await validate_credentials(self.hass, user_input)
            except ValueError as exc:
                errors["base"] = str(exc)
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        schema = vol.Schema({
            vol.Required(CONF_EMAIL): cv.string,
            vol.Required(CONF_PASSWORD): cv.string,
        })

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)



class EonNextOptionsFlow(config_entries.OptionsFlow):
    """Options flow: entity visibility, thresholds and refresh intervals."""

    def __init__(self, config_entry):
        # The framework owns the config_entry attribute on newer HA versions,
        # so keep our own reference under a private name.
        self._eon_entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._eon_entry.options

        schema = vol.Schema({
            vol.Optional(OPTION_SHOW_RATES,
                         default=bool(current.get(OPTION_SHOW_RATES, True))): cv.boolean,
            vol.Optional(OPTION_SHOW_BALANCE,
                         default=bool(current.get(OPTION_SHOW_BALANCE, True))): cv.boolean,
            vol.Optional(OPTION_SHOW_USAGE,
                         default=bool(current.get(OPTION_SHOW_USAGE, True))): cv.boolean,
            vol.Optional(OPTION_SHOW_PREPAY,
                         default=bool(current.get(OPTION_SHOW_PREPAY, True))): cv.boolean,
            vol.Optional(OPTION_LOW_CREDIT_PENCE,
                         default=int(current.get(OPTION_LOW_CREDIT_PENCE, DEFAULT_LOW_CREDIT_PENCE))): cv.positive_int,
            vol.Optional(OPTION_GAS_CALORIFIC_VALUE,
                         default=float(current.get(OPTION_GAS_CALORIFIC_VALUE, DEFAULT_GAS_CALORIFIC_VALUE))): vol.Coerce(float),
            vol.Optional(OPTION_BALANCE_MINUTES,
                         default=int(current.get(OPTION_BALANCE_MINUTES, 15))): cv.positive_int,
            vol.Optional(OPTION_RATES_MINUTES,
                         default=int(current.get(OPTION_RATES_MINUTES, 60))): cv.positive_int,
            vol.Optional(OPTION_PREPAY_MINUTES,
                         default=int(current.get(OPTION_PREPAY_MINUTES, 5))): cv.positive_int,
        })

        return self.async_show_form(step_id="init", data_schema=schema)
