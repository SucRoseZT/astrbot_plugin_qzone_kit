# 更新日志

本文件记录 `astrbot_plugin_qzone_kit` 的版本变更。版本号遵循语义化版本（主版本.次版本.修订号）。

## 1.5.1

**主题：首次公开发布，无功能变更。**

- 发布到 <https://github.com/SucRoseZT/astrbot_plugin_qzone_kit>，`metadata.yaml` 的 `repo` 字段指向该仓库，AstrBot 插件页可直接按仓库地址安装/更新
- `.gitignore` 增补 `*.bak*` / `*.orig` / `*.rej`，避免本地改代码留下的备份文件被误提交
- 版本号在 `main.py` 的 `@register`、`metadata.yaml`、`README.md` 三处对齐
- 代码逻辑与 1.5.0 完全一致，从 1.5.0 升级不需要做任何事

## 1.5.0

**主题：出问题先自检；Cookie 改成「失效才补」。**

### 新增：接口自检

- 面板 → **AI空间 → 诊断 → 开始自检**，或聊天里 `/qzone diag`，一次跑完 12 项，每项给状态（正常/告警/失败/跳过）、耗时、返回码和人话解释
- 覆盖：Cookie 解析、说说列表、动态流（点赞明细）、`qzonetoken`、评论、楼中楼（主站 / h5 / `addreply_ugc` 三个端点各一项）、点赞；发说说 / 上传 / 删说说标「跳过」（有副作用）
- **写接口只发空内容 + 假 ID 的探针**，物理上发不出任何内容；判读依据是「服务端先验登录态再验参数」——回参数错误 = 鉴权已过 = 正常，回 `-10049` 才是真被拒
- 新方法 `QzoneClient.probe_raw()`（不抛业务异常，回原始 HTTP/返回码）、`QzoneClient.check_alive()`、`QzoneClient.diagnose()`
- 新增 `-10005`（您未输入内容）的错误码解释：自检里拿到它说明写链路是通的

### 改动：Cookie 自动维护从「定时覆盖」改为「失效才补」

- 启动检查（20 秒后）和定时检查（默认 240 分钟）都**先验一次当前 Cookie**（一次读请求），有效就什么都不做，缺了或失效才去 NapCat 取
- 新增配置 `auto_cookie_force_refresh`（默认关）：开 = 无视有效性、每次检查都覆盖，行为退回 1.4.0
- 新增「撞到失效立刻补」通道：`QzoneClient` 增加 `on_auth_error` 回调，任何一次调用撞到登录态失效（`-3000` / 跳登录页 / 缺 `p_skey`）都会把后台任务当场叫醒，不用等定时点
- 校验时网络不通归为「说不准」，这一轮**不动 Cookie**（取了也连不上，反而可能把好 Cookie 冲掉）
- `_cookie_state` 拆成「上次检查时间 + 结论」和「上次真正取到的时间」，面板与 `/qzone status` 都显示
- 面板 AI空间 状态卡下方新增一行：`Cookie 自动维护：每 240 分钟检查 · 失效才补 · 上次 11-05 14:50（有效）`

## 1.4.0

**主题：Cookie 不用管了；楼中楼的 `-10049` 也不再是黑盒。**

### 新增：Cookie 全自动维护

- **装上就生效**：`initialize()` 里起一个后台任务，插件加载 20 秒后自动向 NapCat 取一次空间 Cookie 并写入配置，不用再点按钮
- **定时刷新**：`auto_cookie_interval_minutes`（默认 240 分钟）到点重取，避免 `p_skey` 过期导致的批量「登录态已失效」；`0` = 只取启动那一次
- **失败重试**：`auto_cookie_retry_minutes`（默认 15 分钟），NapCat 没连上、QQ 没登录、没在客户端点开过空间，都会按这个间隔再试
- 配置分组新增「Cookie 自动维护」（3 项），面板状态卡显示「自动刷新间隔 + 上次获取时间」
- 安全线：取失败/取到残缺不覆盖已有 Cookie；取到的 QQ 号与当前绑定的不一致时**跳过不覆盖**并在日志里写明；内容没变化就不写盘

### 修复：楼中楼 `-10049`

- **`comment_uin` 自动校正**：`qzone_list` / 面板列表会把每条评论的 `comment_id → 作者 uin` 记进内存索引，`qzone_reply` 发请求前用服务端数据校正调用方传的 uin。填成自己、填成说说主人、AI 瞎编的号，都会被纠回来——这是 `-10049` 最常见的原因
- **三端点兜底**：楼中楼先走主站 `emotion_cgi_re_feeds`（带 `commentId`/`commentUin` 时服务端确实按楼中楼处理），失败退 `h5.qzone` 同名 CGI，再退 `emotion_cgi_addreply_ugc`；全挂才报错，并列出每个端点的返回码
- **错误码人话化**：`-10049` / `-10052` / `-10004` / `-10013` / `-8` 都带排查顺序，并输出 `subcode`、`needVerify`
- **`get_qzonetoken` 修了两个坑**：① 以前请求 H5 页面**没带 Cookie**（会话又是 DummyCookieJar），等于匿名访问，`h5.qzone.qq.com/mqzone/index` 直接 302，永远取不到 token —— 现在补上 Cookie 并跟随跳转；② 桌面版首页的 `window.g_qzonetoken` 现在是空串，取不到属正常，不再当致命错误
- token 缓存加 30 分钟 TTL，评论失败时清缓存换新 token 重试一次
- `qzone_reply` 的 `comment_uin` 允许填不准（留空或填错都能跑），查不到评论作者时才要求重新 `qzone_list`

## 1.3.0

**主题：不用再去浏览器 F12 抄 Cookie 了。**

新增「从 NapCat 直接取空间 Cookie」的完整链路：靠 AstrBot 已有的 aiocqhttp 连接向 NapCat 发 `get_cookies`，从运行中的 QQ 客户端里把 `uin` / `skey` / `p_skey` 取出来，算好 `g_tk` 后直接写入配置。

### 新增

- **WebUI 面板一键获取**：面板 → 设置 → 账号绑定 → Cookie 输入框下方新增「⚡ 一键获取（NapCat）」按钮。
  - 点击后自动取 Cookie、校验 `uin` 与 `g_tk`、**直接保存生效**，无需重载插件
  - 成功后回填输入框，并显示 `QQ 号 / 字段数 / g_tk / 是否含 p_skey`，同时刷新首页状态卡
  - 对应后端接口：`POST /api/plug/astrbot_plugin_qzone_kit/api/cookie/fetch`（管理员面板内调用）
- **聊天命令 `/qzone bind`（不带参数）**：等同于一键获取 —— 从 NapCat 取 Cookie 后走原有的校验与保存流程
- **聊天命令 `/qzone ck`**：只取不绑，打印 Cookie 原文以及字段数、`g_tk`、有无 `p_skey`，方便复制到其他地方（仅管理员）
- 新增 `_cookie_from_bot()` / `_cookie_from_platform()` / `_aiocqhttp_platform()` 三个内部方法，聊天侧与 WebUI 侧共用同一套取 Cookie 逻辑

### 改进

- Cookie 获取失败时按原因给出可操作的提示，不再只回一句「失败」：
  - 没有接入 aiocqhttp / NapCat → 说明本功能只能从运行中的 QQ 取，请手动粘贴
  - 平台在但 NapCat 没连上 → 提示确认 NapCat 已启动并连上 AstrBot
  - OneBot 实现不支持 `get_cookies`（go-cqhttp / Lagrange 等）→ 明确指出这接口只有 NapCat 有
  - 返回空或缺 `p_skey` → 提示先到 QQ 客户端点开一次「空间」面板再试
  - 调用超时（8 秒）单独提示，并依次尝试 `user.qzone.qq.com` → `qzone.qq.com` → `h5.qzone.qq.com`
- Cookie 返回结构做了兼容解析：`data.cookies` 字符串、纯字符串、`cookies` 字典三种形态都能吃下
- `_conf_schema.json` 的 Cookie 字段说明补充了「去面板点一键获取」的指引

### 兼容性

- 行为与配置项**完全向后兼容**，无新增配置项，老配置文件直接可用
- 手动粘贴 Cookie 的流程保留不变；非 NapCat 环境下功能不受影响
- 需要重载一次插件使新接口与面板按钮生效

## 1.2.x 及更早

1.2.0 引入 WebUI 面板与按 `type=object` 分组的配置（由 `gen_schema.py` 生成 `_conf_schema.json`）；1.2.1 为其后的修订版本。早期版本的具体变更请查阅提交历史与 README。
