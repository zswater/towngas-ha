"""港华燃气 API 客户端：微信扫码授权 + 余额查询。

服务端已不再接受匿名的 token=0，账号取数需要 access_token：

1. 取授权地址：GET {OAUTH_BASE}/oauth/authorize2/union?clientid=pe92a8wechat<分公司代码>&redirectUri=...
   （302 到微信授权页，手机微信扫码即）;
2. 用回调地址里的 authCode 换令牌：POST {OAUTH_BASE}/oauth/authorize2/accessToekn?authCode=...
   → access_token / refresh_token / expires_in;
3. 刷新令牌：POST {OAUTH_BASE}/oauth/authorize2/refreshToken?timestamp=..&refreshToken=..&sign=MD5(...).upper();
4. 取数：GET {分公司域名}/openapi/uv1/biz/checkRouters?token=<access_token>&scene=2003&subsCode=..&orgCode=..
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from .const import (
    CLIENT_ID_PREFIX,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_CREATE_TIME,
    CONF_TOKEN_EXPIRES_IN,
    DEFAULT_TOKEN_EXPIRES_IN,
    DIRECT_TIMEOUT,
    FLARESOLVERR_TIMEOUT,
    OAUTH_BASE,
    OAUTH_REDIRECT_URI,
    RESULT_CODE_REFRESH_TOKEN_INVALID,
    RESULT_CODE_TOKEN_EXPIRED,
    RESULT_CODE_TOKEN_MISSING,
    SIGN_SALT,
    TOKEN_EXPIRY_MARGIN,
    VCC_CBS_BASE,
)

_LOGGER = logging.getLogger(__name__)

# 账号数据接口：余额/抄表/账单都在 preCheck 的 datas 里
API_PATH = "/charge/preCheck"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}

# 微信内浏览器的 UA（OAuth 相关接口用它更接近真机）
WECHAT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 NetType/WIFI "
        "MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090c33) XWEB/14315 Flue"
    ),
    "Accept": "application/json, text/plain, */*",
}


class TowngasApiError(Exception):
    """接口返回错误，但属于可重试/业务错误。"""


class TowngasAuthError(Exception):
    """认证不可恢复，需要用户重新授权。"""


def sign_params(params: dict[str, Any]) -> str:
    """按接口要求签名：键排序后拼 key+value，末尾加盐取 MD5 大写。"""
    keys = sorted(
        k for k, v in params.items() if k != "sign" and v is not None and v != ""
    )
    raw = "".join(f"{k}{params[k]}" for k in keys)
    return hashlib.md5((raw + SIGN_SALT).encode()).hexdigest().upper()


def client_id_for_org(org_code: str) -> str:
    """分公司代码 -> OAuth clientid，例如 ZS0105 -> pe92a8wechatZS0105。"""
    return CLIENT_ID_PREFIX + org_code.split("_")[-1]


class TowngasApi:
    """一个户号对应一个实例。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        org_code: str,
        subs_code: str,
        *,
        subs_id: str | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        token_create_time: float = 0.0,
        token_expires_in: int = DEFAULT_TOKEN_EXPIRES_IN,
        flaresolverr_url: str | None = None,
    ) -> None:
        self._session = session
        self._host = host.rstrip("/")
        self._org_code = org_code
        self._subs_code = subs_code
        self._subs_id = subs_id
        self._flaresolverr_url = flaresolverr_url

        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_create_time = token_create_time
        self.token_expires_in = token_expires_in

        self.last_updated: datetime | None = None
        self.use_flaresolverr = False
        self._flaresolverr_session_id: str | None = None

    # ------------------------------------------------------------------
    #  令牌
    # ------------------------------------------------------------------
    @property
    def subs_code(self) -> str:
        """户号。"""
        return self._subs_code

    @property
    def org_code(self) -> str:
        """分公司代码。"""
        return self._org_code

    @property
    def token_valid(self) -> bool:
        """access_token 是否仍在有效期内（留 5 分钟余量）。"""
        if not self.access_token or not self.token_create_time:
            return False
        return (
            self.token_create_time + self.token_expires_in - TOKEN_EXPIRY_MARGIN
            > time.time()
        )

    @property
    def token_remain(self) -> int:
        """access_token 剩余秒数。"""
        if not self.token_valid:
            return 0
        return int(self.token_create_time + self.token_expires_in - time.time())

    def token_data(self) -> dict[str, Any]:
        """需要持久化到 config entry 的令牌字段。"""
        return {
            CONF_ACCESS_TOKEN: self.access_token,
            CONF_REFRESH_TOKEN: self.refresh_token,
            CONF_TOKEN_CREATE_TIME: self.token_create_time,
            CONF_TOKEN_EXPIRES_IN: self.token_expires_in,
        }

    def _store_tokens(self, data: dict[str, Any]) -> None:
        self.access_token = data["access_token"]
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]
        try:
            self.token_expires_in = int(data.get("expires_in") or DEFAULT_TOKEN_EXPIRES_IN)
        except (TypeError, ValueError):
            self.token_expires_in = DEFAULT_TOKEN_EXPIRES_IN
        self.token_create_time = time.time()
        _LOGGER.debug(
            "已保存令牌, expires_in=%s, refresh_token=%s",
            self.token_expires_in,
            "有" if self.refresh_token else "无",
        )

    # ------------------------------------------------------------------
    #  OAuth
    # ------------------------------------------------------------------
    async def async_get_oauth_url(self) -> str:
        """取手机微信需要打开的授权地址（由前端渲染成二维码）。"""
        url = (
            f"{OAUTH_BASE}/oauth/authorize2/union"
            f"?clientid={client_id_for_org(self._org_code)}"
            f"&redirectUri={quote(OAUTH_REDIRECT_URI, safe='')}"
        )
        async with asyncio.timeout(30), self._session.get(
            url, headers=WECHAT_HEADERS, allow_redirects=False
        ) as resp:
            location = resp.headers.get("Location")
            if not location:
                raise TowngasApiError(
                    f"未取到微信授权地址，HTTP {resp.status}（该分公司代码可能不支持微信授权）"
                )
            _LOGGER.debug("OAuth URL: %s", location[:150])
            return location

    async def async_exchange_token(self, auth_code: str) -> None:
        """用 authCode 换 access_token + refresh_token。"""
        url = f"{OAUTH_BASE}/oauth/authorize2/accessToekn?authCode={quote(auth_code.strip(), safe='')}"
        async with asyncio.timeout(30), self._session.post(
            url, headers=WECHAT_HEADERS
        ) as resp:
            data = await self._json_or_error(resp, "换取令牌")

        if not data.get("access_token"):
            message = data.get("resultMsg") or data.get("result_code") or data
            raise TowngasApiError(f"授权码换取令牌失败：{message}")
        self._store_tokens(data)

    async def async_refresh_token(self) -> bool:
        """用 refresh_token 换新的 access_token。"""
        if not self.refresh_token:
            raise TowngasAuthError("没有可用的 refresh_token，需要重新授权")

        params: dict[str, Any] = {
            "timestamp": int(time.time() * 1000),
            "refreshToken": self.refresh_token,
        }
        params["sign"] = sign_params(params)
        url = f"{OAUTH_BASE}/oauth/authorize2/refreshToken?{urlencode(params)}"

        try:
            async with asyncio.timeout(30), self._session.post(
                url, headers=WECHAT_HEADERS
            ) as resp:
                data = await self._json_or_error(resp, "刷新令牌")
        except TowngasApiError as err:
            # 网络类问题不算认证失败，保留旧令牌等下次重试
            _LOGGER.warning("刷新令牌失败（可重试）：%s", err)
            return False

        if data.get("access_token"):
            self._store_tokens(data)
            return True

        code = str(data.get("resultCode") or data.get("result_code") or "")
        message = data.get("resultMsg") or data.get("result_code") or data
        if code.startswith("901") or code == RESULT_CODE_REFRESH_TOKEN_INVALID:
            raise TowngasAuthError(f"refresh_token 已失效：{message}")
        _LOGGER.warning("刷新令牌返回异常：%s", data)
        return False

    async def async_ensure_token(self) -> None:
        """确保有可用的 access_token。"""
        if self.token_valid:
            return
        await self.async_refresh_token()

    # ------------------------------------------------------------------
    #  取数
    # ------------------------------------------------------------------
    async def async_fetch_data(self) -> dict[str, Any]:
        """拉取户号数据（余额/抄表/账单）；令牌失效时抛 TowngasAuthError 由调用方刷新重试。"""
        if not self._subs_id:
            raise TowngasApiError("缺少 subsId（气户标识），请在集成选项里补齐或重新添加集成")

        params: dict[str, Any] = {
            "subsId": self._subs_id,
            "timestamp": int(time.time() * 1000),
        }
        params["sign"] = sign_params(params)
        url = f"{VCC_CBS_BASE}{API_PATH}?{urlencode(params)}"

        status, content_type, text = await self._request(url)
        data = self._decode_json(status, content_type, text)

        code = str(data.get("resultCode") or data.get("code") or "")
        if code in (RESULT_CODE_TOKEN_EXPIRED, RESULT_CODE_TOKEN_MISSING):
            raise TowngasAuthError(
                f"access_token 未被接受（resultCode={code}）：{data.get('resultMsg')}"
            )

        datas = data.get("datas")
        if not isinstance(datas, dict):
            message = data.get("resultMsg") or data.get("msg") or data.get("message") or ""
            raise TowngasApiError(
                f"接口返回异常：resultCode={code or '无'}，message={message or '无'}，keys={list(data)}"
            )

        error_code = str(datas.get("errorCode") or "")
        if error_code not in ("", "0"):
            raise TowngasApiError(
                f"接口错误 {error_code}：{datas.get('errorMsg') or ''}"
            )

        # 余额字段统一转 float，供传感器直接使用
        for key in ("savingSum", "totalFee", "lastReading", "currReading"):
            value = datas.get(key)
            if value is None or isinstance(value, (int, float)):
                continue
            try:
                datas[key] = float(value)
            except (TypeError, ValueError):
                pass

        gas_fee = datas.get("gasFee")
        if isinstance(gas_fee, dict):
            for key in ("totalFee", "totalAmount", "lastReading", "currReading"):
                value = gas_fee.get(key)
                if isinstance(value, str):
                    try:
                        gas_fee[key] = float(value)
                    except ValueError:
                        pass

        self.last_updated = datetime.now().astimezone()
        return datas

    # ------------------------------------------------------------------
    #  底层请求
    # ------------------------------------------------------------------
    async def _request(self, url: str) -> tuple[int, str, str]:
        """先直连，遇到反爬自动切 FlareSolverr（只切一次）。"""
        if not self.use_flaresolverr:
            try:
                async with asyncio.timeout(DIRECT_TIMEOUT):
                    status, content_type, text = await self._direct_request(url)
                if status in (202, 403, 429) or "html" in content_type.lower():
                    _LOGGER.info(
                        "检测到反爬 (status=%s, content-type=%s)，切换 FlareSolverr 模式",
                        status,
                        content_type,
                    )
                    self.use_flaresolverr = True
                else:
                    return status, content_type, text
            except TimeoutError:
                _LOGGER.warning("直连超时，切换 FlareSolverr 模式")
                self.use_flaresolverr = True
            except aiohttp.ClientError as err:
                _LOGGER.warning("直连失败，切换 FlareSolverr 模式：%s", err)
                self.use_flaresolverr = True

        try:
            async with asyncio.timeout(FLARESOLVERR_TIMEOUT):
                return await self._flaresolverr_request(url)
        except Exception as err:  # noqa: BLE001
            if "session" in str(err).lower():
                await self._destroy_flaresolverr_session()
            raise TowngasApiError(f"FlareSolverr 请求失败：{err}") from err

    async def _direct_request(self, url: str) -> tuple[int, str, str]:
        headers = dict(BROWSER_HEADERS, Referer=f"{self._host}/")
        if self.access_token:
            # 账号数据网关靠这个头认证，缺了会返回 40058「token不能为空」
            headers["Authorization"] = f"Bearer {self.access_token}"
        async with self._session.get(url, headers=headers) as resp:
            text = await resp.text()
            return resp.status, resp.headers.get("Content-Type", ""), text

    async def _flaresolverr_request(self, url: str) -> tuple[int, str, str]:
        if not self._flaresolverr_url:
            raise TowngasApiError(
                "该地区需要 FlareSolverr 才能取数，请在集成选项中填写 FlareSolverr 地址"
            )

        if self._flaresolverr_session_id is None:
            async with asyncio.timeout(30), self._session.post(
                self._flaresolverr_url, json={"cmd": "sessions.create"}
            ) as resp:
                result = await resp.json(content_type=None)
            if result.get("status") == "ok":
                self._flaresolverr_session_id = result.get("session")
                _LOGGER.info("已创建 FlareSolverr 会话：%s", self._flaresolverr_session_id)
            else:
                _LOGGER.warning("创建 FlareSolverr 会话失败，改用无会话模式")

        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 60000,
        }
        if self.access_token:
            # 部分 FlareSolverr 版本支持自定义请求头，不支持时会被忽略
            payload["headers"] = {"Authorization": f"Bearer {self.access_token}"}
        if self._flaresolverr_session_id:
            payload["session"] = self._flaresolverr_session_id

        async with asyncio.timeout(70), self._session.post(
            self._flaresolverr_url, json=payload
        ) as resp:
            result = await resp.json(content_type=None)

        if result.get("status") != "ok":
            raise TowngasApiError(f"FlareSolverr 返回错误：{result.get('message')}")

        solution = result.get("solution") or {}
        status = solution.get("status", 0)
        content_type = (solution.get("headers") or {}).get("Content-Type", "")
        text = solution.get("response", "")

        if "text/html" in content_type and "<pre>" in text:
            match = re.search(r"<pre>(.*?)</pre>", text, re.DOTALL)
            if match:
                text = match.group(1).strip()
                content_type = "application/json"

        return status, content_type, text

    async def _destroy_flaresolverr_session(self) -> None:
        if not self._flaresolverr_session_id:
            return
        session_id = self._flaresolverr_session_id
        self._flaresolverr_session_id = None
        try:
            async with asyncio.timeout(10), self._session.post(
                self._flaresolverr_url,
                json={"cmd": "sessions.destroy", "session": session_id},
            ):
                _LOGGER.debug("已释放 FlareSolverr 会话：%s", session_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("释放 FlareSolverr 会话失败：%s", err)

    async def async_close(self) -> None:
        """卸载集成时释放资源。"""
        await self._destroy_flaresolverr_session()

    # ------------------------------------------------------------------
    #  辅助
    # ------------------------------------------------------------------
    @staticmethod
    async def _json_or_error(resp: aiohttp.ClientResponse, action: str) -> dict[str, Any]:
        try:
            data = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001
            raise TowngasApiError(f"{action}返回非 JSON（HTTP {resp.status}）") from err
        if not isinstance(data, dict):
            raise TowngasApiError(f"{action}返回格式异常：{type(data)}")
        return data

    def _decode_json(self, status: int, content_type: str, text: str) -> dict[str, Any]:
        if status >= 400:
            raise TowngasApiError(f"HTTP {status}")

        if "json" not in content_type.lower():
            match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
            if not match:
                _LOGGER.error("响应不是 JSON，前 300 字符：%s", text[:300])
                raise TowngasApiError(f"响应不是 JSON（content-type={content_type}）")
            text = match.group(1)

        if text.startswith("callback(") and text.endswith(")"):
            text = text[8:-1]

        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise TowngasApiError(f"JSON 解析失败：{err}") from err

        if not isinstance(data, dict):
            raise TowngasApiError(f"响应不是 JSON 对象：{type(data)}")
        return data
