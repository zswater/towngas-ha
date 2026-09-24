"""Sensor platform for Towngas integration.

一个户号下会生成多个实体：余额、燃气表读数、上期读数、最近账单金额、未缴金额。
字段来源是数据网关 `charge/preCheck` 的 datas：

    datas.savingSum                        余额
    datas.readingRptList[0].currReading    本期表读数（fallback datas.gasFee.currReading）
    datas.gasFee.lastReading               上期读数
    datas.gasFee.totalAmount               最近账单金额（fallback gasFeeList[0].amount / chrgSum / datas.totalFee）
    datas.gasFeeList[].unpaidFee           未缴金额（欠费，求和；fallback datas.totalFee）
    datas.readingRptList[0].{recordDate,resId,amount}、gasFeeList[0].{yrMonth,price,chrgSum,paidSum,...} 等见属性

不同地区返回的字段可能略有差异，所以取值都做了候选链与类型转换，取不到就是 unknown，不会报错。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
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
    CONF_SUBS_ID,
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


# ----------------------------------------------------------------------
#  取值工具
# ----------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    """接口里数字常是字符串，这里统一转 float。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_item(container: Any) -> dict[str, Any]:
    """列表的第一个字典元素（接口返回的 <xxx>List 都是这个形状）。"""
    if isinstance(container, list) and container and isinstance(container[0], dict):
        return container[0]
    return {}


def _balance(datas: dict[str, Any]) -> float | None:
    """余额。"""
    return _to_float(datas.get("savingSum"))


def _meter_reading(datas: dict[str, Any]) -> float | None:
    """本期燃气表读数。"""
    value = _first_item(datas.get("readingRptList")).get("currReading")
    if value is None:
        value = (datas.get("gasFee") or {}).get("currReading")
    return _to_float(value)


def _last_reading(datas: dict[str, Any]) -> float | None:
    """上期读数。"""
    gas_fee = datas.get("gasFee") or {}
    for candidate in (
        gas_fee.get("lastReading"),
        _first_item(datas.get("readingRptList")).get("lastReading"),
        _first_item(gas_fee.get("gasFeeList")).get("lastReading"),
    ):
        value = _to_float(candidate)
        if value is not None:
            return value
    return None


def _bill_amount(datas: dict[str, Any]) -> float | None:
    """最近一期账单金额。"""
    gas_fee = datas.get("gasFee") or {}
    bill = _first_item(gas_fee.get("gasFeeList"))
    for candidate in (
        gas_fee.get("totalAmount"),
        bill.get("amount"),
        bill.get("chrgSum"),
        datas.get("totalFee"),
    ):
        value = _to_float(candidate)
        if value is not None:
            return value
    return None


def _unpaid_amount(datas: dict[str, Any]) -> float | None:
    """未缴金额（欠费）：账单列表里 unpaidFee 求和。"""
    gas_fee = datas.get("gasFee") or {}
    for candidate in (gas_fee.get("unpaidFee"), gas_fee.get("totalUnpaidFee")):
        value = _to_float(candidate)
        if value is not None:
            return value

    bills = gas_fee.get("gasFeeList")
    if isinstance(bills, list):
        total = 0.0
        found = False
        for bill in bills:
            value = _to_float(bill.get("unpaidFee")) if isinstance(bill, dict) else None
            if value:
                total += value
                found = True
        if found:
            return total

    # 兜底：接口顶层的 totalFee（应缴金额）
    return _to_float(datas.get("totalFee"))


# ----------------------------------------------------------------------
#  实体定义
# ----------------------------------------------------------------------
SENSOR_TYPES: dict[str, dict[str, Any]] = {
    "balance": {
        "value": _balance,
        "device_class": SensorDeviceClass.MONETARY,
        "unit": "CNY",
        "icon": "mdi:currency-cny",
        "state_class": SensorStateClass.TOTAL,
    },
    "meter_reading": {
        "value": _meter_reading,
        "device_class": SensorDeviceClass.GAS,
        "unit": UnitOfVolume.CUBIC_METERS,
        "icon": "mdi:meter-gas",
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "precision": 0,
    },
    "last_reading": {
        "value": _last_reading,
        "device_class": SensorDeviceClass.GAS,
        "unit": UnitOfVolume.CUBIC_METERS,
        "icon": "mdi:meter-gas",
        "precision": 0,
    },
    "bill_amount": {
        "value": _bill_amount,
        "device_class": SensorDeviceClass.MONETARY,
        "unit": "CNY",
        "icon": "mdi:receipt-text-outline",
    },
    "unpaid_amount": {
        "value": _unpaid_amount,
        "device_class": SensorDeviceClass.MONETARY,
        "unit": "CNY",
        "icon": "mdi:cash-alert",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Towngas sensors from a config entry."""
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
        subs_id=config.get(CONF_SUBS_ID),
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
    # 首次拉取失败时让 HA 进入重试/重新授权，而不是留下永远不可用的实体
    await coordinator.async_config_entry_first_refresh()
    async_add_entities(
        TowngasSensor(coordinator, entry, config, sensor_type)
        for sensor_type in SENSOR_TYPES
    )

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
    """数据协调器：保证令牌可用后取数，令牌失效自动刷新并重试一次。"""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: TowngasApi,
        update_interval_minutes: int,
        persist_tokens: Callable[[], None],
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
        """取户号数据。"""
        try:
            await self.api.async_ensure_token()
        except TowngasAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TowngasApiError as err:
            raise UpdateFailed(str(err)) from err

        try:
            data = await self.api.async_fetch_data()
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
                data = await self.api.async_fetch_data()
            except TowngasAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except TowngasApiError as err:
                raise UpdateFailed(str(err)) from err
        except TowngasApiError as err:
            raise UpdateFailed(str(err)) from err

        self._persist_tokens()
        _LOGGER.debug("取数成功，字段：%s", sorted(data))
        return data


class TowngasSensor(CoordinatorEntity[TowngasCoordinator], SensorEntity):
    """港华燃气实体（余额 / 表读数 / 上期读数 / 账单金额 / 未缴金额）。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: TowngasCoordinator,
        entry: ConfigEntry,
        config: dict[str, Any],
        sensor_type: str,
    ) -> None:
        super().__init__(coordinator)
        spec = SENSOR_TYPES[sensor_type]

        self._subs_code = config[CONF_SUBS_CODE]
        self._org_code = config[CONF_ORG_CODE]
        self._host = config[CONF_HOST]
        self._sensor_type = sensor_type
        self._value_fn = spec["value"]

        self._attr_device_class = spec.get("device_class")
        self._attr_native_unit_of_measurement = spec.get("unit")
        self._attr_icon = spec.get("icon")
        self._attr_state_class = spec.get("state_class")
        self._attr_suggested_display_precision = spec.get("precision")
        self._attr_translation_key = sensor_type

        self._attr_unique_id = f"towngas_{sensor_type}_{self._subs_code}_{self._org_code}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"towngas_{self._subs_code}_{self._org_code}")},
            "name": entry.title,
            "manufacturer": "Towngas",
            "configuration_url": self._host,
        }

    @property
    def native_value(self) -> float | None:
        datas = self.coordinator.data or {}
        if not datas:
            return None
        return self._value_fn(datas)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """只在余额实体上挂详细属性，便于核对每一项数据。"""
        datas = self.coordinator.data or {}
        if self._sensor_type != "balance" or not datas:
            return None

        attrs: dict[str, Any] = {
            "subs_code": self._subs_code,
            "org_code": self._org_code,
            "using_flaresolverr": self.coordinator.using_flaresolverr,
            "token_remain": self.coordinator.api.token_remain,
            # 接口所有顶层字段名，方便查看还有哪些数据可取
            "datas_keys": sorted(datas),
        }
        if self.coordinator.last_updated:
            attrs["last_update"] = self.coordinator.last_updated.isoformat()

        for key, value in datas.items():
            if key in ("savingSum", "readingRptList", "gasFee") or isinstance(value, (dict, list)):
                continue
            attrs[key] = value

        reading = _first_item(datas.get("readingRptList"))
        for key in ("recordDate", "resId", "amount", "currReading", "lastReading"):
            if reading.get(key) is not None:
                attrs[f"reading_{key}"] = reading[key]

        gas_fee = datas.get("gasFee") or {}
        for key in (
            "lastReading",
            "currReading",
            "totalAmount",
            "totalFee",
            "recordDate",
            "chrgSum",
            "unpaidFee",
            "paidSum",
        ):
            if gas_fee.get(key) is not None:
                attrs[f"gasFee_{key}"] = gas_fee[key]

        bill = _first_item(gas_fee.get("gasFeeList"))
        for key in (
            "yrMonth",
            "amount",
            "price",
            "chrgSum",
            "unpaidFee",
            "paidSum",
            "lateFeeDate",
            "lastReading",
            "currReading",
        ):
            if bill.get(key) is not None:
                attrs[f"bill_{key}"] = bill[key]

        return attrs
