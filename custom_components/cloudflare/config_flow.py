"""Config flow for Cloudflare integration supporting multiple zones and record selection."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import pycfdns
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_TOKEN, CONF_ZONE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_DOMAINS, DOMAIN
from .helpers import get_configured_domains, get_zone_id

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema({vol.Required(CONF_API_TOKEN): str})


def _zone_schema(zones: list[pycfdns.ZoneModel] | None = None) -> vol.Schema:
    """Zone selection schema."""
    zones_list = []

    if zones is not None:
        zones_list = [zones["name"] for zones in zones]

    return vol.Schema({vol.Required(CONF_ZONE): vol.In(zones_list)})


def _records_schema(records: list[pycfdns.RecordModel] | None = None) -> vol.Schema:
    """Schema for selecting existing A records (checkbox multi-select)."""
    records_dict: dict[str, str] = {}
    if records:
        records_dict = {item["name"]: item["name"] for item in records}
    return vol.Schema({vol.Required(CONF_DOMAINS): cv.multi_select(records_dict)})


def _options_schema(
    records: list[pycfdns.RecordModel] | None,
    configured_domains: list[str],
) -> vol.Schema:
    """Schema for the options flow: existing A records plus new subdomains."""
    records_dict: dict[str, str] = {}
    if records:
        records_dict = {item["name"]: item["name"] for item in records}
    # Keep currently configured domains visible even if their record does not exist yet.
    for domain in configured_domains:
        records_dict.setdefault(domain, domain)

    return vol.Schema(
        {
            vol.Required(CONF_DOMAINS, default=configured_domains): cv.multi_select(
                records_dict
            ),
            vol.Optional("new_domains", default=""): str,
        }
    )


def _normalize_domains(domains: list[str]) -> list[str]:
    """Normalize and deduplicate a list of domains."""
    normalized: list[str] = []
    for domain in domains:
        domain = domain.strip().lower().rstrip(".")
        if domain and domain not in normalized:
            normalized.append(domain)
    return normalized


def _split_new_domains(value: str) -> list[str]:
    """Split a comma-separated string into individual domains."""
    return _normalize_domains(value.split(","))


async def _validate_input(
    hass: HomeAssistant,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from DATA_SCHEMA with values provided by the user.
    """
    zone = data.get(CONF_ZONE)
    # We no longer pre-select records; domains will be free-form
    records: list[pycfdns.RecordModel] = []

    client = pycfdns.Client(
        api_token=data[CONF_API_TOKEN],
        client_session=async_get_clientsession(hass),
    )

    zones = await client.list_zones()
    if zone and (zone_id := get_zone_id(zone, zones)) is not None:
        records = await client.list_dns_records(zone_id=zone_id, type="A")

    return {"zones": zones, "records": records}


class CloudflareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Cloudflare."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> CloudflareOptionsFlowHandler:
        """Get the options flow for this handler."""
        return CloudflareOptionsFlowHandler()

    def __init__(self) -> None:
        """Initialize the Cloudflare config flow."""
        self.cloudflare_config: dict[str, Any] = {}
        self.zones: list[pycfdns.ZoneModel] | None = None
        self.records: list[pycfdns.RecordModel] | None = None

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle initiation of re-authentication with Cloudflare."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle re-authentication with Cloudflare."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _, errors = await self._async_validate_or_error(user_input)

            if not errors:
                reauth_entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={
                        **reauth_entry.data,
                        CONF_API_TOKEN: user_input[CONF_API_TOKEN],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user."""
        errors: dict[str, str] = {}

        if user_input is not None:
            info, errors = await self._async_validate_or_error(user_input)

            if not errors:
                self.cloudflare_config.update(user_input)
                self.zones = info["zones"]
                return await self.async_step_zone()

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the picking the zone."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.cloudflare_config.update(user_input)
            info, errors = await self._async_validate_or_error(self.cloudflare_config)

            if not errors:
                await self.async_set_unique_id(user_input[CONF_ZONE])
                # Proceed to records selection step (existing A records)
                self.records = info["records"]
                return await self.async_step_records()

        return self.async_show_form(
            step_id="zone",
            data_schema=_zone_schema(self.zones),
            errors=errors,
        )

    async def async_step_records(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select existing A records (checkbox list)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            domains_list: list[str] = user_input.get(CONF_DOMAINS, [])
            if not domains_list:
                errors["base"] = "no_domains"
            else:
                self.cloudflare_config[CONF_DOMAINS] = domains_list
                title = self.cloudflare_config[CONF_ZONE]
                return self.async_create_entry(title=title, data=self.cloudflare_config)

        return self.async_show_form(
            step_id="records", data_schema=_records_schema(self.records), errors=errors
        )

    async def _async_validate_or_error(
        self, config: dict[str, Any]
    ) -> tuple[dict[str, list[Any]], dict[str, str]]:
        errors: dict[str, str] = {}
        info = {}

        try:
            info = await _validate_input(self.hass, config)
        except pycfdns.ComunicationException:
            errors["base"] = "cannot_connect"
        except pycfdns.AuthenticationException:
            errors["base"] = "invalid_auth"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return info, errors


class CloudflareOptionsFlowHandler(OptionsFlow):
    """Handle options for the Cloudflare integration."""

    def __init__(self) -> None:
        """Initialize the options flow.

        Home Assistant injects the config entry through ``self.config_entry``;
        it must not be passed to the constructor (removed in HA 2025.12).
        """
        self.records: list[pycfdns.RecordModel] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the domains (and subdomains) managed by the integration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_DOMAINS, [])
            new_domains = _split_new_domains(user_input.get("new_domains", ""))
            domains = _normalize_domains([*selected, *new_domains])
            if not domains:
                errors["base"] = "no_domains"
            else:
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_DOMAINS: domains},
                )

        if self.records is None:
            try:
                self.records = await self._async_get_records()
            except pycfdns.AuthenticationException:
                return self.async_abort(reason="invalid_auth")
            except pycfdns.ComunicationException:
                return self.async_abort(reason="cannot_connect")

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                self.records, get_configured_domains(self.config_entry)
            ),
            errors=errors,
        )

    async def _async_get_records(self) -> list[pycfdns.RecordModel]:
        """Fetch the existing A records for the configured zone."""
        entry = self.config_entry
        client = pycfdns.Client(
            api_token=entry.data[CONF_API_TOKEN],
            client_session=async_get_clientsession(self.hass),
        )
        zones = await client.list_zones()
        zone_id = get_zone_id(entry.data[CONF_ZONE], zones)
        if zone_id is None:
            return []
        return await client.list_dns_records(zone_id=zone_id, type="A")


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
