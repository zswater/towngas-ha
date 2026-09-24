# 港华燃气Home Assistant集成
通过Home Assistant获取港华燃气余额数据，代码由AI生成

# 安装
1. 通过HACS添加自定义仓库
2. 搜索"Towngas" 
3. 点击安装

# 使用

港华的服务端已不再接受匿名查询（`token=0` 会返回 `access token 过期`），所以从 2.1.0 起必须先做一次微信授权：

1. **选择燃气分公司**：下拉列表里可以搜「中山」这类关键字；
2. **微信扫码授权**：配置向导会显示一个二维码，用**手机微信**扫一扫并确认授权（在电脑浏览器里直接打开这个链接会被微信拦截）。授权后页面会跳到 `https://weixin.towngasvcc.com/h5-gas/`，复制跳转地址里 `authCode=` 后面那一串；
3. **填写户号与授权码**：户号（subsCode）见下一节；授权码粘贴上一步拿到的那串；可选填刷新间隔（默认 30 分钟）和 FlareSolverr 地址。

集成会把 `refresh_token` 保存在配置条目里并定时保活；令牌彻底失效时会自动弹出「重新授权」，重新扫码即可，不需要重新添加集成。

配置中的 updatetime 为更新间隔（默认 30 分钟，单位分钟）。

<img width="879" height="589" alt="ScreenShot_2026-01-06_082825_840" src="https://github.com/user-attachments/assets/311e9f8d-1746-44fc-b635-fc5ff3bad2a1" />

<img width="890" height="651" alt="ScreenShot_2026-01-06_082848_870" src="https://github.com/user-attachments/assets/028576fd-0ba6-4aae-a82a-8d8f11d9db8a" />


# 获取用户号、区域码
打开港华燃气网址：https://www.towngasvcc.com/
选择自己的港华分公司，登录自己的燃气账号，打开业务办理>账单缴费，网址加载完后为：https://xxxxxx.towngasvcc.com/business/pay/owe/QYXXX/16XXXXX
16XXXXX为用户号（也就是账单缴费网址的最后一段）

# 关于部分地区防爬
部分地区的服务器存在防爬机制，需要调用外部工具FlareSolverr才能正常获取到数据，已知地区：山东，其他地区自行测试
FlareSolverr需自行部署

# 常见问题

- **添加集成时提示「无法加载配置向导 / Invalid handler specified」**：这是历史版本源文件编码不是 UTF-8 造成的（Python 导入即失败）。请删除 `/config/custom_components/towngas` 后通过 HACS 重新安装最新版，不要用手工改过的旧目录或论坛附件里的旧 zip。
- **实体显示未知/不可用**：先用诊断脚本看服务端的原始返回：
  ```
  python tools/towngas_login.py --org-code ZS0105 --subs-code 你的户号
  ```
  返回 `{"resultCode":"20001","resultMsg":"access token 过期"}` 说明授权已失效，点集成卡片上的「重新授权」再扫码一次。
