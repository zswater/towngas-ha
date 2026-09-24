"""Config flow for Towngas integration.

流程：选分公司 → 扫码授权 + 填户号信息（同一个表单）。

设计要点：步骤的标题/描述来自前端按 step_id 查翻译（`component.towngas.config.step.<id>.description`）。
如果这一步的翻译没被前端加载（例如浏览器缓存了旧的翻译包），**没有表单字段的步骤就会显示成空白窗口**。
所以这里不再使用「空 schema + 只有描述」的步骤：二维码放在有字段的表单描述里，
授权链接同时作为一个可复制的文本框默认值，保证任何情况下界面上都有可见内容。
"""
from __future__ import annotations

import base64
import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

import qrcode
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

_LOGGER = logging.getLogger(__name__)

ORG_LIST_FILE = Path(__file__).with_name("orglist.json")

# 授权链接的展示字段名（提交时忽略它的值）
FIELD_OAUTH_URL = "oauth_url"


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


def render_qr_markdown(data: str) -> str:
    """把地址渲染成 Markdown 内联二维码图片（配置向导里显示用）。"""
    if not data:
        return ""
    qr = qrcode.QRCode(border=2, box_size=5)
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"![oauth_qr](data:image/png;base64,{encoded})"


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
        """取一次微信授权地址（同一流程内复用；失败返回 None）。"""
        if self._oauth_url:
            return self._oauth_url
        try:
            self._oauth_url = await self._new_api().async_get_oauth_url()
        except (TowngasApiError, TowngasAuthError) as err:
            _LOGGER.error("获取微信授权地址失败：%s", err)
            return None
        return self._oauth_url

    def _qr_placeholders(self) -> dict[str, str]:
        """描述里的二维码/链接占位符。"""
        return {
            "oauth_qr_markdown": render_qr_markdown(self._oauth_url),
            "oauth_url": self._oauth_url,
            "oauth_redirect": OAUTH_REDIRECT_URI,
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
        """带二维码链接的账号表单（有字段，界面不会空白）。"""
        return vol.Schema(
            {
                # 展示用：默认值就是微信授权地址，方便复制到手机微信里打开
                vol.Optional(FIELD_OAUTH_URL, default=self._oauth_url): str,
                vol.Required(CONF_SUBS_CODE): vol.All(
                    str, vol.Strip, vol.Length(min=1)
                ),
                vol.Required(CONF_SUBS_ID): vol.All(
                    str, vol.Strip, vol.Length(min=8)
                ),
                vol.Required(CONF_AUTH_CODE): str,
                vol.Optional(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                ): vol.All(vol.Coerce(int), vol.Range(min=5)),
                vol.Optional(CONF_FLARESOLVERR_URL): str,
            }
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """第二步：扫码授权 + 填户号信息。"""
        errors: dict[str, str] = {}
        placeholders = self._qr_placeholders()

        if self.selected_org is None:
            return self.async_abort(reason="no_orgs")

        if await self._async_ensure_oauth_url() is None:
            errors["base"] = "oauth_url_failed"
            placeholders["error_detail"] = "（获取微信授权地址失败，请检查网络后重试）"
        else:
            placeholders["oauth_qr_markdown"] = render_qr_markdown(self._oauth_url)

        if user_input is not None and not errors:
            self._subs_code = user_input[CONF_SUBS_CODE]
            self._subs_id = user_input[CONF_SUBS_ID]
            auth_code = _parse_auth_code(user_input.get(CONF_AUTH_CODE, ""))
            api = self._new_api(flaresolverr_url=user_input.get(CONF_FLARESOLVERR_URL))

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
                        CONF_SUBS_CODE: self._subs_code,
                        CONF_SUBS_ID: self._subs_id,
                        CONF_ORG_CODE: self.selected_org["orgCode"],
                        CONF_HOST: self.selected_org["host"],
                        CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                        CONF_FLARESOLVERR_URL: user_input.get(CONF_FLARESOLVERR_URL, ""),
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
        """重新授权：扫码 + 填新 authCode（同一表单，避免空白窗口）。"""
        errors: dict[str, str] = {}
        placeholders = self._qr_placeholders()

        if await self._async_ensure_oauth_url() is None:
            errors["base"] = "oauth_url_failed"
        else:
            placeholders["oauth_qr_markdown"] = render_qr_markdown(self._oauth_url)

        if user_input is not None and not errors:
            auth_code = _parse_auth_code(user_input.get(CONF_AUTH_CODE, ""))
            api = self._new_api()
            if not auth_code:
                errors["base"] = "auth_code_required"
            else:
                try:
                    await api.async_exchange_token(auth_code)
                except TowngasApiError as err:
                    _LOGGER.warning("重新授权失败：%s", err)
                    errors["base"] = "auth_failed"
                    placeholders["error_detail"] = f"（{err}）"
                except TowngasAuthError as err:
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
                    vol.Optional(FIELD_OAUTH_URL, default=self._oauth_url): str,
                    vol.Required(CONF_AUTH_CODE): str,
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
