# QQ空间Kit（astrbot_plugin_qzone_kit）

QQ 空间精简工具插件：**发说说（带图）、评论、回复评论、点赞、读说说、删说说**。
单进程直连 QQ 空间 Web 接口，无独立 daemon、无额外依赖（复用 AstrBot 自带 aiohttp）。

## WebUI 面板（1.2.0 新增）

入口：AstrBot WebUI → 插件 → 蔗糖的方便QQ空间 → 打开面板（`/api/plug/astrbot_plugin_qzone_kit/panel`）。

- **空间动态**：看任意 QQ 的说说（图片直接显示、评论楼中楼展开）、发说说（可带图）、点赞/取消赞、评论/楼中楼回复、删除（两段确认）
- **设置**：配置按「账号绑定 / 功能开关 / 权限 / 提示词注入 / 冷却与防抖」五个分组展示，**改动即时保存生效，不用重载插件**
- **说明**：Cookie 获取步骤、命令表、冷却与写锁的生效范围

面板操作是人工操作，不走冷却和写锁（那两套刹车只管 AI）。废消息拦截对面板发布同样生效。

## 安装

1. 目录放入 `data/plugins/astrbot_plugin_qzone_kit`
2. WebUI → 插件管理 → 重载/扫描
3. 管理员执行 `/qzone bind <Cookie>` 绑定（或插件配置里填 `cookies_str`，或在面板「设置 → 账号绑定」里粘贴）

## Cookie 获取

浏览器登录 `user.qzone.qq.com` → F12 → Network → 任意请求 → 复制 Cookie 请求头（需含 `p_skey` 和 `uin`）。Cookie 过期后（数周~数月）重新绑定一次，所有工具返回的错误会明确提示 Cookie 失效。

## LLM 工具（给机器人用，仅管理员）

| 工具 | 说明 |
|---|---|
| `qzone_list` | 读指定 QQ 的最近说说（fid/时间/内容/评论列表），评论点赞前必须先调它 |
| `qzone_post` | 发说说，images 参数可带本地路径或 http 链接（最多 9 张） |
| `qzone_comment` | 给说说发评论 |
| `qzone_reply` | 回复说说下的某条评论（楼中楼） |
| `qzone_like` | 点赞 / 取消赞 |
| `qzone_delete` | 删除自己的说说 |

## 管理命令

- `/qzone bind <Cookie>` — 绑定并即时校验
- `/qzone status` — 查看绑定状态和登录态
- `/qzone unbind` — 解绑
- `/qzone set` — 查看所有开关状态
- `/qzone set <开关> <on|off>` — 口头切换开关，省略 on/off 则翻转。开关名：`list`/`post`/`comment`/`reply`/`like`/`delete`（六个功能各自独立）、`admin`（仅主人可用）、`prompt`（提示词注入）

## 配置分组（1.2.0 起）

`_conf_schema.json` 按 `type=object` 分成五组（由 `gen_schema.py` 从 `main.py` 的 `CFG_GROUPS/CFG_ITEMS/CFG_DEFAULTS` 生成，改文案后跑一次 `python gen_schema.py` 保持同步）：

| 分组 | 字段 |
|---|---|
| 账号绑定 | `cookies_str` |
| 功能开关 | `enable_list/post/comment/reply/like/delete` |
| 权限 | `admin_only` |
| 提示词注入 | `inject_prompt`、`custom_prompt` |
| 冷却与防抖 | 四个动作冷却秒数 + `write_lock_seconds` |

老配置里的扁平键仍然认（读取时分组优先），升级不用迁移数据。

## 防滥用设计

- 所有写操作仅管理员可用
- 内容冷却：发说说 600s / 评论 120s / 点赞 60s / 删除 600s（可配置，0 关闭），冷却只在**发送成功后**记账，失败可重试；**只对 AI 调工具生效，面板人工操作不受限**
- 废消息拦截：含"请忽略/误发/占位/测试消息"类内容直接拒绝，不占冷却
- 工具 docstring 内置软提示词：仅主人明确要求时调用、一次请求最多一次
