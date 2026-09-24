# 港华燃气Home Assistant集成
通过Home Assistant获取港华燃气余额数据，代码由AI生成

# 安装
1. 通过HACS添加自定义仓库
2. 搜索"Towngas" 
3. 点击安装

# 使用

港华的服务端已不再接受匿名查询（`token=0` 会返回 `access token 过期`），所以从 2.1.0 起必须先做一次微信授权：

1. **选择燃气分公司**：下拉列表里可以按公司名/地区搜索关键字；
2. **微信扫码授权**：配置向导会显示一个二维码，用**手机微信**扫一扫并确认授权（在电脑浏览器里直接打开这个链接会被微信拦截）。授权后页面会跳到 `https://weixin.towngasvcc.com/h5-gas/`，复制跳转地址里 `authCode=` 后面那一串；
3. **填写户号信息与授权码**（下表三个值，字段名里也写了取法）；可选填刷新间隔（默认 30 分钟）和 FlareSolverr 地址。

| 字段 | 从哪里获取 |
| --- | --- |
| **户号 subsCode** | 打开「业务办理 → 账单缴费」，网址最后一段就是，例如 `https://xxx.towngasvcc.com/business/pay/owe/`**`XXXXXX`**`/`**`16XXXXX`** |
| **气户标识 subsId** | 浏览器登录 <https://www.towngasvcc.com/?login=true>（手机号 + 图形验证码 + 短信验证码），再打开 <https://www.towngasvcc.com/user/querySubsList>，返回 JSON 里 32 位的 `subsId` |
| **授权码 authCode** | 上一步微信扫码授权后，浏览器跳转到 `https://weixin.towngasvcc.com/h5-gas/?authCode=`**`xxxx`**`&state=...`，复制 `authCode=` 后面那一串（约 5 分钟内有效） |

> 看到的二维码不明显？配置向导里还有个固定地址：**`http://你的HA地址:8123/api/towngas/qr`**，直接在浏览器打开就是当前要扫的二维码（不依赖前端翻译，任何情况下都能用）。

集成会把 `refresh_token` 保存在配置条目里并定时保活；令牌彻底失效时会自动弹出「重新授权」，重新扫码即可，不需要重新添加集成。

配置中的 updatetime 为更新间隔（默认 30 分钟，单位分钟）。

<img width="879" height="589" alt="ScreenShot_2026-01-06_082825_840" src="https://github.com/user-attachments/assets/311e9f8d-1746-44fc-b635-fc5ff3bad2a1" />

<img width="890" height="651" alt="ScreenShot_2026-01-06_082848_870" src="https://github.com/user-attachments/assets/028576fd-0ba6-4aae-a82a-8d8f11d9db8a" />


# 获取用户号（subsCode）、区域码
打开港华燃气网址：https://www.towngasvcc.com/
选择自己的港华分公司，登录自己的燃气账号，打开业务办理>账单缴费，网址加载完后为：https://xxxxxx.towngasvcc.com/business/pay/owe/QYXXX/16XXXXX
16XXXXX为用户号（也就是账单缴费网址的最后一段），QYXXX 为区域码（对应配置里的分公司）

# 获取气户标识（subsId）
账号数据接口需要 32 位的 `subsId`（接口里叫「气户标识」），取法：

1. 浏览器打开 https://www.towngasvcc.com/?login=true ，用**手机号 + 图形验证码 + 短信验证码**登录（或页面上的微信扫码登录）；
2. 登录成功后，在同一个浏览器里打开 https://www.towngasvcc.com/user/querySubsList ；
3. 页面会返回一段 JSON，形如：
   ```json
   {"datas":[{"subsId":"0123456789abcdef0123456789abcdef","subsCode":"16XXXXX","subsName":"...","subsAddr":"..."}]}
   ```
   其中 `subsId` 就是配置集成时要填的值（名下有多块表时会有多条，按 subsCode 或地址区分）。

> 一个户号只需要抄一次，填进集成后就不用再管了。

# 生成的实体与可用数据

一个户号会在同一个设备下生成 5 个实体：

| 实体 | 单位/类别 | 数据来源（接口 `charge/preCheck` 的 `datas`） |
| --- | --- | --- |
| **燃气余额** | CNY | `savingSum` |
| **燃气表读数** | m³（气体、累计，可直接接入能源面板） | `readingRptList[0].currReading`，缺省回退 `gasFee.currReading` |
| **上期读数** | m³ | `gasFee.lastReading`，回退 `readingRptList[0].lastReading` / `gasFeeList[0].lastReading` |
| **最近账单金额** | CNY | `gasFee.totalAmount`，回退 `gasFeeList[0].amount` / `chrgSum` / `datas.totalFee` |
| **未缴金额（欠费）** | CNY | `gasFee.unpaidFee`，否则 `gasFeeList[].unpaidFee` 求和，兜底 `datas.totalFee` |

> 各地区返回字段可能略有差异，取不到就是 `unknown`，不会报错；接口给的细节都会挂在余额实体的属性里。

余额实体上的属性（原样来自接口，方便核对）：

| 属性 | 含义 |
| --- | --- |
| `datas_keys` | 接口返回的**全部顶层字段名**——想知道还有什么数据可读，看它就够了 |
| `reading_recordDate` / `reading_resId` / `reading_amount` | 抄表日期、表号、抄表金额 |
| `gasFee_lastReading` / `gasFee_currReading` / `gasFee_totalAmount` / `gasFee_unpaidFee` / `gasFee_paidSum` / `gasFee_recordDate` | 读数与费用明细 |
| `bill_yrMonth` / `bill_amount` / `bill_price` / `bill_chrgSum` / `bill_unpaidFee` / `bill_paidSum` / `bill_lateFeeDate` | 最近一期账单：账期、金额、单价、应收、未缴、已缴、滞纳金日期 |
| `chargeType` / `subsName` / `subsAddr` / `orgCode` | 计费类型、户名、地址、分公司 |
| `token_remain` / `using_flaresolverr` / `last_update` | 令牌剩余秒数、是否走 FlareSolverr、最近取数时间 |

想看更全的原始字段：打开集成的调试日志（设置 → 设备与服务 → 港华燃气 → 启用调试日志），每次取数会把字段名写进日志。

# 关于部分地区防爬
部分地区的服务器存在防爬机制，需要调用外部工具FlareSolverr才能正常获取到数据，已知地区：山东，其他地区自行测试
FlareSolverr需自行部署

# 常见问题

- **添加集成时提示「无法加载配置向导 / Invalid handler specified」**：这是历史版本源文件编码不是 UTF-8 造成的（Python 导入即失败）。请删除 `/config/custom_components/towngas` 后通过 HACS 重新安装最新版，不要用手工改过的旧目录或论坛附件里的旧 zip。
- **实体显示未知/不可用**：先用诊断脚本看服务端的原始返回：
  ```
  python tools/towngas_login.py --org-code <分公司代码> --subs-code <户号> --subs-id <气户标识>
  ```
  返回 `{"resultCode":"20001","resultMsg":"access token 过期"}` 说明授权已失效，点集成卡片上的「重新授权」再扫码一次。
