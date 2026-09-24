"""The Towngas integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .qr_view import async_register_qr_view

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Towngas from a config entry."""
    # 二维码图片接口（/api/towngas/qr）：配置向导里也会按需注册，这里保证重启后依然可用
    async_register_qr_view(hass)
    # 注意：选项变更的重载监听器在 sensor.py 里注册——令牌刷新会写回 entry.data，
    # 那里需要「仅选项变化才重载」的判断，否则会循环重载。
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
