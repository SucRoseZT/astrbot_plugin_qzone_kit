# QQ空间Kit（astrbot_plugin_qzone_kit）

QQ 空间精简工具插件：**发说说（带图）、评论、回复评论、点赞、读说说、删说说**。
单进程直连 QQ 空间 Web 接口，无独立 daemon、无额外依赖（复用 AstrBot 自带 aiohttp）。

## WebUI 面板（1.2.0 新增）

入口：AstrBot WebUI → 插件 → 蔗糖的方便QQ空间 → 打开面板（`/api/plug/astrbot_plugin_qzone_kit/panel`）。

- **空间动态**：看任意 QQ 的说说（图片直接显示、评论楼中楼展开）、发说说（可带图）、点赞/取消赞、评论/楼中楼回复、删除（两段确认）
- **设置**：配置按「账号绑定 / Cookie 自动维护 / 功能开关 / 点赞显示 / 权限 / 提示词注入 / 冷却与防抖」分组展示，**改动即时保存生效，不用重载插件**
- **一键取 Cookie**：设置页 Cookie 框下方的「⚡ 一键获取（NapCat）」，直接从运行中的 QQ 取空间 Cookie 并保存（1.3.0 新增，见下方「Cookie 获取」）
- **Cookie 自动维护**：按需补，不是无脑覆盖——启动时和定时点先验一次当前 Cookie，失效或没绑才去 NapCat 取（1.4.0 起，1.5.0 改为「失效才补」）
- **接口自检**：AI空间 → 诊断 → 「开始自检」，逐个端点探活，一眼看出是哪个接口坏了（1.5.0 新增）
- **说明**：Cookie 获取步骤、命令表、冷却与写锁的生效范围

面板操作是人工操作，不走冷却和写锁（那两套刹车只管 AI）。废消息拦截对面板发布同样生效。

## 安装

1. 目录放入 `data/plugins/astrbot_plugin_qzone_kit`
2. WebUI → 插件管理 → 重载/扫描
3. 管理员执行 `/qzone bind <Cookie>` 绑定（或插件配置里填 `cookies_str`，或在面板「设置 → 账号绑定」里粘贴）

## Cookie 获取

**方式零：全自动（1.4.0 起，1.5.0 改成「失效才补」）**

装上什么都不用配。插件加载 20 秒后**先验一次当前 Cookie**：没绑或已经失效，才去 NapCat 取；有效就什么都不做。之后每隔 `auto_cookie_interval_minutes`（默认 240 分钟）再验一次，还是失效才取。

为什么不每次无脑覆盖：有效的 Cookie 换掉没有任何好处，反而会在 NapCat 上登着别的号、或客户端一时没回过神的时候把好好的登录态冲掉。

另外有一条「立刻补」的通道：AI 或面板调用时撞到「登录态已失效」，客户端会回调把后台任务**当场叫醒**，不用等定时点。

| 配置项（面板 → 设置 → Cookie 自动维护） | 默认 | 作用 |
|---|---|---|
| `auto_cookie_on_load` | 开 | 插件启动 20 秒后验一次，缺了才取 |
| `auto_cookie_interval_minutes` | 240 | 每隔多少分钟验一次，`0` = 只做启动那一次检查 |
| `auto_cookie_force_refresh` | 关 | 开 = 无视有效性，每次检查都从 NapCat 覆盖一遍（旧行为） |
| `auto_cookie_retry_minutes` | 15 | 取失败（NapCat 没连上 / QQ 没登录）后的重试间隔 |

几条安全线：

- 取不到、取到残缺（缺 `uin` 或 `p_skey`）时**不会动已有 Cookie**，只重试
- 取到的 QQ 号和当前绑定的不一致时**跳过不覆盖**（NapCat 上可能登的是另一个号），日志里会写明，需要换号请手动点「一键获取」或 `/qzone bind`
- 取到的内容和当前一样就不写盘，不反复刷配置文件
- 校验时网络不通属于「说不准」，这一轮**不动 Cookie**（取了也一样连不上）
- 总开关：`auto_cookie_on_load` 关 + 间隔 `0` = 完全关闭自动维护

## 接口自检（1.5.0 新增）

面板 → **AI空间 → 诊断 → 开始自检**，或者聊天里 `/qzone diag`。

一次跑完 12 项，每项给出状态（正常 / 告警 / 失败 / 跳过）、耗时、返回码和人话解释：

| 探测项 | 怎么探 |
|---|---|
| Cookie 解析 | 本地：`uin`、`g_tk`、字段数、缺哪个关键字段 |
| 说说列表 `emotion_cgi_msglist_v6` | 真读一条 |
| 动态流 `feeds3_html_more` | 真拉一次（点赞明细的数据源） |
| 评论令牌 `qzonetoken` | 真去 H5 页面捞一次 |
| 评论 `emotion_cgi_re_feeds` | 空内容 + 假 ID 探针 |
| 楼中楼 · 主站 / h5 / `addreply_ugc` | 三个端点各一次空内容探针 |
| 点赞 `internal_dolike_app` | 假 ID 探针 |
| 发说说 / 上传图片 / 删说说 | 跳过（有副作用，不探） |

**写接口只发空内容 + 假 ID 的探针，物理上发不出任何东西**。判读逻辑是：服务端先验登录态再验参数，所以返回「参数错误 / 内容为空」= 鉴权这关已经过了，算正常；返回 `-10049` 才是真的被拒。

这张表最有用的地方是分辨这两种情况：

- **读全通、写全挂** → 账号写操作被风控（看有没有 `needVerify=1`），不是 Cookie 过期，换 Cookie 没用
- **某一层挂** → 直接看是哪一行，比如只有 `addreply_ugc` 挂、主站通，那楼中楼就会自动走兜底

**方式一：一键获取（1.3.0 新增，需要 NapCat）**

面板 → 设置 → 账号绑定 → Cookie 输入框下面点 **「⚡ 一键获取（NapCat）」**。插件会通过 AstrBot 的 aiocqhttp 连接向 NapCat 发 `get_cookies`，直接从运行中的 QQ 客户端取出空间 Cookie（`uin`/`skey`/`p_skey`），校验通过后**自动保存**，不用重载插件。

- 前提：平台是 aiocqhttp 且接入的是 **NapCat**（`get_cookies` 是 NapCat 扩展接口，go-cqhttp / Lagrange 之类没有）
- NapCat 里需要先建立过空间会话，否则取不到 `p_skey`：在 QQ 客户端里点开一次「空间」面板再点按钮
- 其他实现或没连上时，面板会直接给出对应原因，退回手动粘贴即可

**方式二：手动粘贴**

浏览器登录 `user.qzone.qq.com` → F12 → Network → 任意请求 → 复制 Cookie 请求头（需含 `p_skey` 和 `uin`）。Cookie 过期后（数周~数月）重新绑定一次，所有工具返回的错误会明确提示 Cookie 失效。聊天里也可以 `/qzone bind <Cookie>`，或 `/qzone bind` 不带参数（= 从 NapCat 一键取）。

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
- `/qzone bind` — 不带参数：直接从 NapCat 取 Cookie 并绑定（1.3.0 新增）
- `/qzone ck` — 只打印从 NapCat 取到的 Cookie（含字段数、`g_tk`、有无 `p_skey`），方便贴到别处（1.3.0 新增）
- `/qzone diag` — 逐个接口探活并输出报告，排查「能读不能写」专用（1.5.0 新增）
- `/qzone status` — 查看绑定状态和登录态
- `/qzone unbind` — 解绑
- `/qzone set` — 查看所有开关状态
- `/qzone set <开关> <on|off>` — 口头切换开关，省略 on/off 则翻转。开关名：`list`/`post`/`comment`/`reply`/`like`/`delete`（六个功能各自独立）、`admin`（仅主人可用）、`prompt`（提示词注入）

## 配置分组（1.2.0 起）

`_conf_schema.json` 按 `type=object` 分成七组（由 `gen_schema.py` 从 `main.py` 的 `CFG_GROUPS/CFG_ITEMS/CFG_DEFAULTS` 生成，改文案后跑一次 `python gen_schema.py` 保持同步）：

| 分组 | 字段 |
|---|---|
| 账号绑定 | `cookies_str` |
| Cookie 自动维护 | `auto_cookie_on_load`、`auto_cookie_interval_minutes`、`auto_cookie_force_refresh`、`auto_cookie_retry_minutes` |
| 功能开关 | `enable_list/post/comment/reply/like/delete` |
| 点赞显示 | `show_likes_self/other`、`like_names_max` |
| 权限 | `admin_only` |
| 提示词注入 | `inject_prompt`、`custom_prompt` |
| 冷却与防抖 | 四个动作冷却秒数 + `write_lock_seconds` |

老配置里的扁平键仍然认（读取时分组优先），升级不用迁移数据。

## 排错：楼中楼报 `-10049`

`-10049` 是空间侧「这次写操作我不认」的通用拒绝码，读接口不校验它，所以典型表现是**能读不能写**。按概率排：

1. **`comment_uin` 不是被回复评论的真实作者**（填成自己 / 说说主人 / 编的号）。空间只认评论作者本人，填错直接判非法目标。1.4.0 起插件会用服务端数据自动校正，这个原因基本没了。
2. **对方说说限制了评论**：评论区权限、评论审核、非好友、被拉黑。
3. **这个号被风控**：响应里带 `needVerify=1`。让号主在手机 QQ 或网页版手动评一条、过掉验证码，再跑机器人。
4. 顶层评论也 `-10049` → 基本都是第 3 条（账号级），换号或等风控解除。

1.4.0 起报错信息会带上 `subcode` / `needVerify` 和每个端点的返回码，方便往上反馈。1.5.0 起可以先跑 `/qzone diag` 或面板的「接口自检」，把上面这几种情况直接分辨出来：读全通写全挂 = 第 3 条，某个端点单独挂 = 看那一行。

## 防滥用设计

- 所有写操作仅管理员可用
- 内容冷却：发说说 600s / 评论 120s / 点赞 60s / 删除 600s（可配置，0 关闭），冷却只在**发送成功后**记账，失败可重试；**只对 AI 调工具生效，面板人工操作不受限**
- 废消息拦截：含"请忽略/误发/占位/测试消息"类内容直接拒绝，不占冷却
- 工具 docstring 内置软提示词：仅主人明确要求时调用、一次请求最多一次

## 更新日志

见 [CHANGELOG.md](./CHANGELOG.md)。当前版本 **1.5.1**（首次公开发布，无功能变更）。

- **1.5.0**：新增接口自检（面板按钮 + `/qzone diag`，写接口只发空内容探针）；Cookie 自动维护改成「失效才补」（启动与定时都先验，撞到失效立刻补）
- **1.4.0**：Cookie 全自动维护（启动即取 + 定时刷新）；楼中楼回复修 `-10049`（自动校正 `comment_uin`、三端点兜底、错误码人话解释）
- **1.3.0**：一键从 NapCat 获取空间 Cookie
