"""Constants for the Towngas integration."""

DOMAIN = "towngas"

CONF_HOST = "host"
CONF_SUBS_CODE = "subsCode"
CONF_SUBS_ID = "subsId"
CONF_ORG_CODE = "orgCode"
CONF_UPDATE_INTERVAL = "updatetime"
CONF_FLARESOLVERR_URL = "flaresolverr_url"

# 微信 OAuth 令牌：账号取数必须带 access_token（服务端已不接受匿名 token=0）
CONF_AUTH_CODE = "auth_code"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN_CREATE_TIME = "token_create_time"
CONF_TOKEN_EXPIRES_IN = "token_expires_in"
CONF_TOKEN_REFRESH_INTERVAL = "token_refresh_interval"

DEFAULT_UPDATE_INTERVAL = 30
DEFAULT_FLARESOLVERR_URL = "http://127.0.0.1:8191/v1"

# 港华统一网关：clientid = CLIENT_ID_PREFIX + 分公司代码（如 XXXXXX -> pe92a8wechat<分公司代码>）
OAUTH_BASE = "https://weixin.towngasvcc.com/vcc-oauth"
# 账号数据网关（Bearer 令牌 + 签名），余额/抄表/账单都走这里
VCC_CBS_BASE = "https://weixin.towngasvcc.com/nv1/vcc-cbs"
CLIENT_ID_PREFIX = "pe92a8wechat"
OAUTH_REDIRECT_URI = "https://weixin.towngasvcc.com/h5-gas/"
SIGN_SALT = "hbasesoft.com-prod"

DEFAULT_TOKEN_EXPIRES_IN = 7200
DEFAULT_TOKEN_REFRESH_INTERVAL = 1800  # 30 分钟保活刷新，避免 refresh_token 长期不用失效
TOKEN_EXPIRY_MARGIN = 300  # 提前 5 分钟视为过期

DIRECT_TIMEOUT = 20
FLARESOLVERR_TIMEOUT = 75

# 服务端把 access_token 失效统一返回该码
RESULT_CODE_TOKEN_EXPIRED = "20001"
# 网关没收到/不认令牌
RESULT_CODE_TOKEN_MISSING = "40058"
# refresh_token 失效
RESULT_CODE_REFRESH_TOKEN_INVALID = "90143"
