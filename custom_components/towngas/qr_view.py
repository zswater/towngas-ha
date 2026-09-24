"""微信授权二维码：内联 Markdown + 可单独打开的图片接口。

配置向导的描述文字来自前端翻译（`component.towngas.config.step.<id>.description`），
当浏览器缓存了旧的翻译包时描述可能不显示，二维码也就看不到。所以这里额外提供一个
图片接口 `/api/towngas/qr`：在浏览器里打开它就能看到二维码，不依赖任何翻译。
该图片内容只是「微信授权地址」，本身是公开信息（任何人都能取到同一个地址），
所以不需要登录即可访问，方便在手机浏览器里打开。
"""
from __future__ import annotations

import base64
import logging
from io import BytesIO

import qrcode
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

QR_URL = "/api/towngas/qr"


def render_qr_png(data: str) -> bytes:
    """把地址渲染成 PNG 字节。"""
    qr = qrcode.QRCode(border=2, box_size=6)
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_qr_markdown(data: str) -> str:
    """内联 Markdown 二维码（配置向导描述里显示用）。"""
    if not data:
        return ""
    encoded = base64.b64encode(render_qr_png(data)).decode("ascii")
    return f"![oauth_qr](data:image/png;base64,{encoded})"


class TowngasQrView(HomeAssistantView):
    """返回当前微信授权地址的二维码图片。"""

    url = QR_URL
    name = "api:towngas:qr"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """返回 PNG。"""
        hass: HomeAssistant = request.app["hass"]
        png = (hass.data.get(DOMAIN) or {}).get("oauth_qr_png")
        if not png:
            return web.Response(
                status=404,
                text="当前没有待授权的二维码：请先在 Home Assistant 里打开「添加集成」向导。",
            )
        return web.Response(
            body=png,
            content_type="image/png",
            headers={"Cache-Control": "no-store"},
        )


def async_register_qr_view(hass: HomeAssistant) -> None:
    """注册二维码接口（只注册一次）。"""
    store = hass.data.setdefault(DOMAIN, {})
    if store.get("view_registered") or getattr(hass, "http", None) is None:
        return
    hass.http.register_view(TowngasQrView())
    store["view_registered"] = True
    _LOGGER.debug("已注册二维码接口 %s", QR_URL)


def async_store_oauth_url(hass: HomeAssistant, url: str) -> None:
    """记住当前授权地址，供二维码接口渲染。"""
    store = hass.data.setdefault(DOMAIN, {})
    if url and store.get("oauth_url") == url:
        return
    store["oauth_url"] = url
    try:
        store["oauth_qr_png"] = render_qr_png(url) if url else None
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("生成二维码失败：%s", err)
        store["oauth_qr_png"] = None
