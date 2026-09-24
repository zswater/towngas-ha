"""Config flow for Towngas integration.

流程：选分公司 → 微信扫码授权（拿 authCode）→ 填户号 + authCode → 校验并创建。
账号取数需要令牌，服务端已不接受匿名的 token=0，所以授权是必要步骤。
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
    value = raw.strip()
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

    def _qr_markdown(self) -> str:
        """把微信授权地址渲染成 Markdown 内联图片。"""
        return render_qr_markdown(self._oauth_url)

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
                return await self.async_step_oauth()
            errors["base"] = "invalid_org"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required("org_code"): vol.In(self._org_options())}
            ),
            errors=errors,
        )

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """第二步：微信扫码授权。"""
        if user_input is not None:
            return await self.async_step_account()

        if not self._oauth_url:
            api = self._new_api()
            try:
                self._oauth_url = await api.async_get_oauth_url()
            except (TowngasApiError, TowngasAuthError) as err:
                _LOGGER.error("获取微信授权地址失败：%s", err)
                return self.async_abort(reason="oauth_url_failed")

        return self.async_show_form(
            step_id="oauth",
            data_schema=vol.Schema({}),
            description_placeholders={
                "oauth_qr_markdown": self._qr_markdown(),
                "oauth_url": self._oauth_url,
                "oauth_redirect": OAUTH_REDIRECT_URI,
                "client_id": client_id_for_org(self.selected_org["orgCode"]),
            },
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """第三步：填户号 + 授权码，换取令牌。"""
        if self.selected_org is None:
            return self.async_abort(reason="no_orgs")

        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {
            "oauth_qr_markdown": self._qr_markdown(),
            "oauth_redirect": OAUTH_REDIRECT_URI,
            "error_detail": "",
        }

        if user_input is not None:
            self._subs_code = user_input[CONF_SUBS_CODE]
            self._subs_id = user_input[CONF_SUBS_ID]
            auth_code = _parse_auth_code(user_input.get(CONF_AUTH_CODE, ""))
            api = self._new_api(
                flaresolverr_url=user_input.get(CONF_FLARESOLVERR_URL)
            )

            token_ok = False
            if not auth_code:
                errors["base"] = "auth_code_required"
            else:
                try:
                    await api.async_exchange_token(auth_code)
                    token_ok = True
                except TowngasApiError as err:
                    _LOGGER.warning("授权码换取令牌失败：%s", err)
                    errors["base"] = "auth_failed"
                    description_placeholders["error_detail"] = str(err)

            if token_ok:
                # 顺手验证一次取数，失败不阻塞创建（实体状态会显示真实原因）
                try:
                    await api.async_fetch_data()
                    _LOGGER.info("首次取数校验通过")
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("首次取数校验未通过（仍会创建条目）：%s", err)
                    description_placeholders["error_detail"] = f"（取数校验未通过：{err}）"

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
            data_schema=vol.Schema(
                {
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
            ),
            errors=errors,
            description_placeholders=description_placeholders,
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
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """重新授权第一步：重新扫码。"""
        if user_input is not None:
            return await self.async_step_reauth_token()

        self._oauth_url = ""
        api = self._new_api()
        try:
            self._oauth_url = await api.async_get_oauth_url()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("重新授权：获取微信授权地址失败")
            return self.async_abort(reason="oauth_url_failed")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "oauth_qr_markdown": self._qr_markdown(),
                "oauth_url": self._oauth_url,
                "oauth_redirect": OAUTH_REDIRECT_URI,
                "subs_code": self._subs_code,
            },
        )

    async def async_step_reauth_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """重新授权第二步：粘贴新的 authCode，更新令牌。"""
        errors: dict[str, str] = {}

        if user_input is not None:
            auth_code = _parse_auth_code(user_input.get(CONF_AUTH_CODE, ""))
            api = self._new_api()
            try:
                await api.async_exchange_token(auth_code)
            except TowngasApiError as err:
                _LOGGER.warning("重新授权失败：%s", err)
                errors["base"] = "auth_failed"
            else:
                entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **api.token_data()}
                )

        return self.async_show_form(
            step_id="reauth_token",
            data_schema=vol.Schema({vol.Required(CONF_AUTH_CODE): str}),
            errors=errors,
            description_placeholders={
                "oauth_qr_markdown": self._qr_markdown(),
                "oauth_redirect": OAUTH_REDIRECT_URI,
            },
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
