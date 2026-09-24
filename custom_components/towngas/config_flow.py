"""Config flow for Towngas integration.

流程：选分公司 → 同一个表单里「扫码授权 + 填户号信息」。

两个刻意的设计（都是为了「不依赖前端翻译也能用」）：
1. 不使用「没有输入框、内容全在描述里」的步骤——HA 前端的步骤标题/描述来自
   `component.towngas.config.step.<id>.description` 的翻译查找，查不到就是空白窗口；
2. 字段名本身就写成「怎么填、从哪取」的中文说明。前端翻译失效时字段名会原样显示，
   用户照样能看懂；翻译正常时则由 translations 里的短标签覆盖。
   提交时再把这些字段映射成稳定的内部键（subsCode/subsId/auth_code...）存储。
"""
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TowngasApi, TowngasApiError, TowngasAuthError, client_id_for_org
from .const import (
    CONF_AUTH_CODE,
    CONF_FLARESOLVERR_URL,
    CONF_HOST,
    CONF_ORG_CODE,
    CONF_SUBS_CODE,
    CONF_SUBS_ID,
    CONF_TOKEN_REFRESH_INTERVAL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_FLARESOLVERR_URL,
    DEFAULT_TOKEN_REFRESH_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    OAUTH_REDIRECT_URI,
)
from .qr_view import (
    QR_URL,
    async_register_qr_view,
    async_store_oauth_url,
    render_qr_markdown,
)

_LOGGER = logging.getLogger(__name__)

ORG_LIST_FILE = Path(__file__).with_name("orglist.json")

# 字段名即说明（翻译失效时原样显示，翻译正常时用 translations 里的短标签）
FIELD_QR = (
    "① 微信扫码授权：浏览器打开 <你的HA地址>" + QR_URL + " 用手机微信扫一扫"
    "（也可以复制本框里的链接，发到手机微信里点开）"
)
FIELD_SUBS_CODE = (
    "② 户号 subsCode：账单缴费网址最后一段，"
    "例如 .../ZS0105/1700075442 就填 1700075442"
)
FIELD_SUBS_ID = (
    "③ 气户标识 subsId：浏览器登录 www.towngasvcc.com/?login=true（手机号+短信验证码）后，"
    "打开 www.towngasvcc.com/user/querySubsList，复制其中 32 位的 subsId"
)
FIELD_AUTH_CODE = (
    "④ 授权码 authCode：扫码授权后，从跳转地址里复制 authCode= 后面那一串"
    "（约 5 分钟内有效）"
)
FIELD_INTERVAL = "⑤ 刷新间隔（分钟）"
FIELD_FLARESOLVERR = "⑥ FlareSolverr 地址（可选，仅个别地区需要）"


def load_org_list() -> list[dict[str, Any]]:
    """从内置 JSON 读取分公司列表。"""
    try:
        with ORG_LIST_FILE.open(encoding="utf-8") as file:
            org_list = json.load(file).get("orgList", [])
    except (OSError, ValueError) as err:
        _LOGGER.error("读取分公司列表失败：%s", err)
        return []

    # 只保留构建下拉框和发请求都需要的字段齐全的分公司
    return [org for org in org_list if org.get("orgCode") and org.get("host")]


def _parse_auth_code(raw: str) -> str:
    """允许用户直接粘贴回调地址，自动截出 authCode。"""
    value = (raw or "").strip()
    for sep in ("authCode=", "auth_code=", "code="):
        if sep in value:
            return value.split(sep, 1)[1].split("&", 1)[0].strip()
    return value


class TowngasConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Towngas."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        self.org_list: list[dict[str, Any]] = []
        self.selected_org: dict[str, Any] | None = None
        self._oauth_url = ""
        self._subs_code = ""
        self._subs_id = ""

    # ------------------------------------------------------------------
    #  工具
    # ------------------------------------------------------------------
    def _org_options(self) -> dict[str, str]:
        return {
            org["orgCode"]: (
                f"{org.get('shortName') or org.get('orgName') or org['orgCode']}"
                f" ({org.get('desc', '')})"
            )
            for org in self.org_list
        }

    def _new_api(self, *, flaresolverr_url: str | None = None) -> TowngasApi:
        org = self.selected_org or {}
        return TowngasApi(
            async_get_clientsession(self.hass),
            org.get("host", ""),
            org.get("orgCode", ""),
            self._subs_code or "unknown",
            subs_id=self._subs_id or None,
            flaresolverr_url=flaresolverr_url,
        )

    async def _async_ensure_oauth_url(self) -> str | None:
        """取一次微信授权地址（同一流程内复用），并让二维码接口能渲染它。"""
        if not self._oauth_url:
            try:
                self._oauth_url = await self._new_api().async_get_oauth_url()
            except (TowngasApiError, TowngasAuthError) as err:
                _LOGGER.error("获取微信授权地址失败：%s", err)
                return None
        # 注册图片接口并缓存二维码，界面上随时可用 /api/towngas/qr 打开
        async_register_qr_view(self.hass)
        async_store_oauth_url(self.hass, self._oauth_url)
        return self._oauth_url

    def _qr_placeholders(self) -> dict[str, str]:
        """描述里的二维码/链接占位符。"""
        return {
            "oauth_qr_markdown": render_qr_markdown(self._oauth_url),
            "oauth_url": self._oauth_url,
            "oauth_redirect": OAUTH_REDIRECT_URI,
            "qr_url": QR_URL,
            "client_id": client_id_for_org((self.selected_org or {}).get("orgCode", "")),
            "subs_code": self._subs_code,
            "error_detail": "",
        }

    # ------------------------------------------------------------------
    #  配置流程
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """第一步：选择燃气分公司。"""
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

    def _account_schema(self) -> vol.Schema:
        """账号表单：字段名自带说明，界面永远不会是空白。"""
        return vol.Schema(
            {
                # 展示用：默认值就是微信授权地址，可复制到手机微信里打开
                vol.Optional(FIELD_QR, default=self._oauth_url): str,
                vol.Required(FIELD_SUBS_CODE): vol.All(
                    str, vol.Strip, vol.Length(min=1)
                ),
                vol.Required(FIELD_SUBS_ID): vol.All(str, vol.Strip, vol.Length(min=8)),
                vol.Required(FIELD_AUTH_CODE): str,
                vol.Optional(
                    FIELD_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                ): vol.All(vol.Coerce(int), vol.Range(min=5)),
                vol.Optional(FIELD_FLARESOLVERR): str,
            }
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """第二步：扫码授权 + 填户号信息。"""
        errors: dict[str, str] = {}

        if self.selected_org is None:
            return self.async_abort(reason="no_orgs")

        # 先拿到授权地址，再构建占位符/表单默认值（顺序反了链接会为空）
        url_ok = await self._async_ensure_oauth_url() is not None
        if not url_ok:
            errors["base"] = "oauth_url_failed"
        placeholders = self._qr_placeholders()
        if not url_ok:
            placeholders["error_detail"] = "（获取微信授权地址失败，请检查网络后重试）"

        if user_input is not None and not errors:
            self._subs_code = user_input[FIELD_SUBS_CODE]
            self._subs_id = user_input[FIELD_SUBS_ID]
            auth_code = _parse_auth_code(user_input.get(FIELD_AUTH_CODE, ""))
            api = self._new_api(flaresolverr_url=user_input.get(FIELD_FLARESOLVERR))

            if not auth_code:
                errors["base"] = "auth_code_required"
            else:
                try:
                    await api.async_exchange_token(auth_code)
                except TowngasApiError as err:
                    _LOGGER.warning("授权码换取令牌失败：%s", err)
                    errors["base"] = "auth_failed"
                    placeholders["error_detail"] = f"（{err}）"

            if not errors:
                # 顺手验证一次取数，失败不阻塞创建（实体状态会显示真实原因）
                try:
                    await api.async_fetch_data()
                    _LOGGER.info("首次取数校验通过")
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("首次取数校验未通过（仍会创建条目）：%s", err)

                await self.async_set_unique_id(
                    f"{self._subs_code}_{self.selected_org['orgCode']}"
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=(
                        f"Towngas "
                        f"{self.selected_org.get('shortName') or self.selected_org['orgCode']} "
                        f"{self._subs_code}"
                    ),
                    data={
                        # 存储用稳定的内部键，与界面上的说明性字段名解耦
                        CONF_SUBS_CODE: self._subs_code,
                        CONF_SUBS_ID: self._subs_id,
                        CONF_ORG_CODE: self.selected_org["orgCode"],
                        CONF_HOST: self.selected_org["host"],
                        CONF_UPDATE_INTERVAL: user_input[FIELD_INTERVAL],
                        CONF_FLARESOLVERR_URL: (
                            user_input.get(FIELD_FLARESOLVERR) or ""
                        ),
                        **api.token_data(),
                    },
                )

        return self.async_show_form(
            step_id="account",
            data_schema=self._account_schema(),
            errors=errors,
            description_placeholders=placeholders,
        )

    # ------------------------------------------------------------------
    #  重新授权
    # ------------------------------------------------------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """令牌彻底失效后由 HA 拉起重新授权。"""
        self.org_list = await self.hass.async_add_executor_job(load_org_list)
        org_code = entry_data.get(CONF_ORG_CODE)
        self.selected_org = next(
            (org for org in self.org_list if org["orgCode"] == org_code), None
        )
        if self.selected_org is None:
            return self.async_abort(reason="no_orgs")
        self._subs_code = entry_data.get(CONF_SUBS_CODE, "")
        self._subs_id = entry_data.get(CONF_SUBS_ID, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """重新授权：扫码 + 填新授权码（同一个表单，避免空白窗口）。"""
        errors: dict[str, str] = {}

        url_ok = await self._async_ensure_oauth_url() is not None
        if not url_ok:
            errors["base"] = "oauth_url_failed"
        placeholders = self._qr_placeholders()
        if not url_ok:
            placeholders["error_detail"] = "（获取微信授权地址失败，请检查网络后重试）"

        if user_input is not None and not errors:
            auth_code = _parse_auth_code(user_input.get(FIELD_AUTH_CODE, ""))
            api = self._new_api()
            if not auth_code:
                errors["base"] = "auth_code_required"
            else:
                try:
                    await api.async_exchange_token(auth_code)
                except (TowngasApiError, TowngasAuthError) as err:
                    _LOGGER.warning("重新授权失败：%s", err)
                    errors["base"] = "auth_failed"
                    placeholders["error_detail"] = f"（{err}）"

            if not errors:
                entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **api.token_data()}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(FIELD_QR, default=self._oauth_url): str,
                    vol.Required(FIELD_AUTH_CODE): str,
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
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
                    ): vol.All(vol.Coerce(int), vol.Range(min=5)),
                    vol.Optional(
                        CONF_TOKEN_REFRESH_INTERVAL,
                        default=options.get(
                            CONF_TOKEN_REFRESH_INTERVAL,
                            data.get(
                                CONF_TOKEN_REFRESH_INTERVAL,
                                DEFAULT_TOKEN_REFRESH_INTERVAL,
                            ),
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=300)),
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
