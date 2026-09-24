"""Config flow for Towngas integration."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_FLARESOLVERR_URL,
    CONF_HOST,
    CONF_ORG_CODE,
    CONF_SUBS_CODE,
    CONF_UPDATE_INTERVAL,
    DEFAULT_FLARESOLVERR_URL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

ORG_LIST_FILE = Path(__file__).with_name("orglist.json")


def load_org_list() -> list[dict[str, Any]]:
    """Load organization list from the bundled JSON file."""
    try:
        with ORG_LIST_FILE.open(encoding="utf-8") as file:
            org_list = json.load(file).get("orgList", [])
    except (OSError, ValueError) as err:
        _LOGGER.error("Failed to load organization list: %s", err)
        return []

    # 只保留构建下拉框和发请求都需要的字段齐全的分公司
    return [org for org in org_list if org.get("orgCode") and org.get("host")]


class TowngasConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Towngas."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.org_list: list[dict[str, Any]] = []
        self.selected_org: dict[str, Any] | None = None

    def _org_options(self) -> dict[str, str]:
        """Return the orgCode -> label mapping used by the picker."""
        return {
            org["orgCode"]: (
                f"{org.get('shortName') or org.get('orgName') or org['orgCode']}"
                f" ({org.get('desc', '')})"
            )
            for org in self.org_list
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if not self.org_list:
            self.org_list = await self.hass.async_add_executor_job(load_org_list)
            if not self.org_list:
                return self.async_abort(reason="no_orgs")

        if user_input is not None:
            self.selected_org = next(
                (
                    org
                    for org in self.org_list
                    if org["orgCode"] == user_input["org_code"]
                ),
                None,
            )
            if self.selected_org is not None:
                return await self.async_step_account()
            errors["base"] = "invalid_org"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required("org_code"): vol.In(self._org_options())}
            ),
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the account setup step."""
        if self.selected_org is None:
            # 正常流程不会走到这里（user 步骤一定先选中分公司）
            return self.async_abort(reason="no_orgs")

        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_SUBS_CODE]}_{self.selected_org['orgCode']}"
            )
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=(
                    f"Towngas "
                    f"{self.selected_org.get('shortName') or self.selected_org['orgCode']} "
                    f"{user_input[CONF_SUBS_CODE]}"
                ),
                data={
                    CONF_SUBS_CODE: user_input[CONF_SUBS_CODE],
                    CONF_ORG_CODE: self.selected_org["orgCode"],
                    CONF_HOST: self.selected_org["host"],
                    CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                    CONF_FLARESOLVERR_URL: user_input[CONF_FLARESOLVERR_URL],
                },
            )

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SUBS_CODE): vol.All(
                        str, vol.Strip, vol.Length(min=1)
                    ),
                    vol.Optional(
                        CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                    ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                    vol.Optional(
                        CONF_FLARESOLVERR_URL, default=DEFAULT_FLARESOLVERR_URL
                    ): str,
                }
            ),
            errors={},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return TowngasOptionsFlowHandler()


class TowngasOptionsFlowHandler(OptionsFlow):
    """Handle an options flow for Towngas."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        data = self.config_entry.data
        options = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_UPDATE_INTERVAL,
                        default=options.get(
                            CONF_UPDATE_INTERVAL,
                            data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                    vol.Optional(
                        CONF_FLARESOLVERR_URL,
                        default=options.get(
                            CONF_FLARESOLVERR_URL,
                            data.get(CONF_FLARESOLVERR_URL, DEFAULT_FLARESOLVERR_URL),
                        ),
                    ): str,
                }
            ),
        )
