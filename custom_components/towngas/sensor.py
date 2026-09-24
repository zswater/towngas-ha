"""Sensor platform for Towngas integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import TowngasApi, TowngasApiError, TowngasAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_FLARESOLVERR_URL,
    CONF_HOST,
    CONF_ORG_CODE,
    CONF_REFRESH_TOKEN,
    CONF_SUBS_CODE,
    CONF_TOKEN_CREATE_TIME,
    CONF_TOKEN_EXPIRES_IN,
    CONF_TOKEN_REFRESH_INTERVAL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_FLARESOLVERR_URL,
    DEFAULT_TOKEN_EXPIRES_IN,
    DEFAULT_TOKEN_REFRESH_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Towngas sensor from a config entry."""
    config = entry.data
    options = entry.options

    update_interval = int(
        options.get(
            CONF_UPDATE_INTERVAL,
            config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )
    )
    token_refresh_interval = int(
        options.get(
            CONF_TOKEN_REFRESH_INTERVAL,
            config.get(CONF_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL),
        )
    )
    flaresolverr_url = (
        options.get(CONF_FLARESOLVERR_URL)
        or config.get(CONF_FLARESOLVERR_URL)
        or DEFAULT_FLARESOLVERR_URL
    )

    api = TowngasApi(
        async_get_clientsession(hass),
        config[CONF_HOST],
        config[CONF_ORG_CODE],
        config[CONF_SUBS_CODE],
        access_token=config.get(CONF_ACCESS_TOKEN),
        refresh_token=config.get(CONF_REFRESH_TOKEN),
        token_create_time=config.get(CONF_TOKEN_CREATE_TIME, 0.0),
        token_expires_in=config.get(CONF_TOKEN_EXPIRES_IN, DEFAULT_TOKEN_EXPIRES_IN),
        flaresolverr_url=flaresolverr_url,
    )

    def _persist_tokens() -> None:
        """令牌会轮换，写回 config entry 才能在重启后继续用。"""
        new_data = {**entry.data, **api.token_data()}
        if new_data != entry.data:
            hass.config_entries.async_update_entry(entry, data=new_data)

    coordinator = TowngasCoordinator(hass, entry, api, update_interval, _persist_tokens)
    # 首次拉取失败时让 HA 进入重试/重新授权，而不是留下一个永远不可用的实体
    await coordinator.async_config_entry_first_refresh()
    async_add_entities([TowngasSensor(coordinator, config, entry.entry_id)])

    # ---- refresh_token 保活：固定间隔刷新一次，避免长期不用失效 ----
    async def _async_token_keepalive(_now=None) -> None:
        try:
            refreshed = await api.async_refresh_token()
        except TowngasAuthError as err:
            _LOGGER.warning("令牌保活失败，需要重新授权：%s", err)
            entry.async_start_reauth(hass)
            return
        except TowngasApiError as err:
            _LOGGER.warning("令牌保活异常（稍后重试）：%s", err)
            refreshed = False
        if refreshed:
            _persist_tokens()
            _LOGGER.debug("令牌保活成功，剩余 %ss", api.token_remain)
        _schedule_token_keepalive()

    def _schedule_token_keepalive() -> None:
        delay = min(token_refresh_interval, max(api.token_remain - 60, 60))
        entry.async_on_unload(async_call_later(hass, delay, _async_token_keepalive))

    _schedule_token_keepalive()

    # ---- 选项变更才重载；令牌写回 data 不触发重载（否则会循环重载）----
    snapshot_options = dict(entry.options)

    async def _async_entry_updated(
        _hass: HomeAssistant, updated_entry: ConfigEntry
    ) -> None:
        if updated_entry.options == snapshot_options:
            return
        await _hass.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    entry.async_on_unload(api.async_close)


class TowngasCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """数据协调器：保证令牌可用后取余额，令牌失效自动刷新并重试一次。"""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: TowngasApi,
        update_interval_minutes: int,
        persist_tokens,
    ) -> None:
        self.entry = entry
        self.api = api
        self._persist_tokens = persist_tokens
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{api.subs_code}_{api.org_code}",
            update_interval=timedelta(minutes=update_interval_minutes),
        )

    @property
    def last_updated(self):
        """最近一次成功取数的本地时间。"""
        return self.api.last_updated

    @property
    def using_flaresolverr(self) -> bool:
        return self.api.use_flaresolverr

    async def _async_update_data(self) -> dict[str, Any]:
        """取余额数据。"""
        try:
            await self.api.async_ensure_token()
        except TowngasAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TowngasApiError as err:
            raise UpdateFailed(str(err)) from err

        try:
            data = await self.api.async_fetch_balance()
        except TowngasAuthError:
            # 服务端认为令牌已失效：刷新后重试一次
            _LOGGER.info("access_token 被服务端判定失效，尝试刷新后重试")
            try:
                await self.api.async_refresh_token()
            except TowngasAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except TowngasApiError as err:
                raise UpdateFailed(str(err)) from err
            try:
                data = await self.api.async_fetch_balance()
            except TowngasAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except TowngasApiError as err:
                raise UpdateFailed(str(err)) from err
        except TowngasApiError as err:
            raise UpdateFailed(str(err)) from err

        self._persist_tokens()
        return data


class TowngasSensor(CoordinatorEntity[TowngasCoordinator], SensorEntity):
    """燃气余额传感器实体。"""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "CNY"
    _attr_icon = "mdi:currency-cny"
    _attr_should_poll = False

    def __init__(
        self, coordinator: TowngasCoordinator, config: dict[str, Any], entry_id: str
    ) -> None:
        super().__init__(coordinator)
        self._subs_code = config[CONF_SUBS_CODE]
        self._org_code = config[CONF_ORG_CODE]
        self._host = config[CONF_HOST]
        self._entry_id = entry_id

        self._attr_name = f"Towngas Balance {self._subs_code}"
        self._attr_unique_id = f"towngas_balance_{self._subs_code}_{self._org_code}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._attr_unique_id)},
            "name": self._attr_name,
            "manufacturer": "Towngas",
            "configuration_url": self._host,
        }

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("savingSum")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self.coordinator.data:
            return None
        attrs: dict[str, Any] = {
            "subs_code": self._subs_code,
            "org_code": self._org_code,
            "host": self._host,
            "using_flaresolverr": self.coordinator.using_flaresolverr,
            "token_remain": self.coordinator.api.token_remain,
        }
        if self.coordinator.last_updated:
            attrs["last_update"] = self.coordinator.last_updated.isoformat()
        return attrs
