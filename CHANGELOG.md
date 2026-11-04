# 更新日志

本文件记录 `astrbot_plugin_qzone_kit` 的版本变更。版本号遵循语义化版本（主版本.次版本.修订号）。

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
