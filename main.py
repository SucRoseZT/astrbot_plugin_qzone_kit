import asyncio
import re
import time
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from quart import send_file

from .qzone_api import (
    MAX_IMAGES,
    QzoneAuthError,
    QzoneClient,
    QzoneError,
    _strip_html,
)


PLUGIN_NAME = "astrbot_plugin_qzone_kit"
PANEL_PREFIX = f"{PLUGIN_NAME}/panel"
API_PREFIX = f"{PLUGIN_NAME}/api"

# 配置分组：schema key、显示名、这一组是干什么的。
# AstrBot 官方配置页拿它当大分类（type=object），组说明显示在组标题下面；
# gen_schema.py 依据这两张表生成分组版 _conf_schema.json，面板「设置」页也按它渲染。
CFG_GROUPS = [
    ("account", "账号绑定",
     "QQ 空间的登录凭证。Cookie 失效了整个插件就只剩报错，先来这里换。"),
    ("switch", "功能开关",
     "六个动作一个个开关。关掉的动作 AI 调用会被直接拒绝，提示词里也会声明它停用。"),
    ("perm", "权限",
     "谁能用这套空间功能。默认只有管理员，放开给所有人之前想清楚。"),
    ("prompt", "提示词注入",
     "每轮对话往 AI 提示词里塞的使用规范，防止 AI 乱调工具、发废消息。"),
    ("cool", "冷却与防抖",
     "AI 侧的频率刹车：动作冷却 + 写后全局锁。只在 AI 调工具时生效，面板里人工操作不受限。"),
]

# 字段表：key、短名、类型、分组 key、详细说明。
# 短名要短到能在一行里放下（官方配置页拿它当标题），所有解释都写在最后一个字段。
CFG_ITEMS = [
    # ---- 账号 ----
    ("cookies_str", "空间 Cookie", "str", "account",
     "浏览器登录 user.qzone.qq.com 后 F12 → Network → 任意请求 → 复制 Cookie 头，需含 p_skey 和 uin。"
     "也可以在聊天里 /qzone bind <Cookie> 绑定，或在面板里粘贴保存"),
    # ---- 开关 ----
    ("enable_list", "查说说", "bool", "switch",
     "AI 可以查看指定 QQ 的说说列表（内容、图片数、评论）"),
    ("enable_post", "发说说", "bool", "switch",
     "AI 可以发布说说，可带图，一次最多 9 张"),
    ("enable_comment", "评论", "bool", "switch",
     "AI 可以给说说发表评论"),
    ("enable_reply", "回复评论", "bool", "switch",
     "AI 可以楼中楼回复某条评论"),
    ("enable_like", "点赞", "bool", "switch",
     "AI 可以点赞，也可以取消赞"),
    ("enable_delete", "删说说", "bool", "switch",
     "AI 可以删除主人自己发的说说。不可恢复，删除前 AI 会先向你确认"),
    # ---- 权限 ----
    ("admin_only", "仅管理员可用", "bool", "perm",
     "开启后所有 Qzone 功能（聊天里的 LLM 工具和 /qzone 命令）只有管理员能用。"
     "关闭后任何人可用，不建议"),
    # ---- 提示词 ----
    ("inject_prompt", "注入使用规范", "bool", "prompt",
     "每次 LLM 请求自动附加一段 Qzone 工具使用规范：什么时候该调/不该调、写操作成功后立刻停手、"
     "参数必须照抄列表输出等"),
    ("custom_prompt", "自定义追加提示词", "str", "prompt",
     "写在注入内容末尾的额外要求，留空则不加"),
    # ---- 冷却 ----
    ("post_cooldown_seconds", "发说说冷却（秒）", "int", "cool",
     "同一会话两次成功发布的最小间隔，0 = 关闭"),
    ("comment_cooldown_seconds", "评论冷却（秒）", "int", "cool",
     "对同一条说说的评论类操作（评论/回复）最小间隔，0 = 关闭"),
    ("like_cooldown_seconds", "点赞冷却（秒）", "int", "cool",
     "对同一条说说的点赞类操作最小间隔，0 = 关闭"),
    ("delete_cooldown_seconds", "删说说冷却（秒）", "int", "cool",
     "删除操作的冷却时间，0 = 关闭"),
    ("write_lock_seconds", "写后全局锁（秒）", "int", "cool",
     "发/评/赞/删成功后锁定全部 Qzone 工具（含读取），期间 AI 只能用文字汇报结果，"
     "防止它成功后又调工具去验证。0 = 关闭"),
]
CFG_KEYS = {k for k, *_ in CFG_ITEMS}
CFG_GROUP_OF = {k: g for k, _l, _t, g, _h in CFG_ITEMS}
CFG_GROUP_LABEL = {k: label for k, label, _h in CFG_GROUPS}
CFG_GROUP_HINT = {k: hint for k, _label, hint in CFG_GROUPS}
CFG_TYPE_OF = {k: t for k, _l, t, _g, _h in CFG_ITEMS}
CFG_ITEM_HINT = {k: h for k, _l, _t, _g, h in CFG_ITEMS}
CFG_DEFAULTS = {
    "cookies_str": "",
    "enable_list": True, "enable_post": True, "enable_comment": True,
    "enable_reply": True, "enable_like": True, "enable_delete": True,
    "admin_only": True,
    "inject_prompt": True, "custom_prompt": "",
    "post_cooldown_seconds": 600, "comment_cooldown_seconds": 120,
    "like_cooldown_seconds": 60, "delete_cooldown_seconds": 600,
    "write_lock_seconds": 60,
}

_IMAGE_MAGIC = (
    b"\xff\xd8\xff",  # jpg
    b"\x89PNG",  # png
    b"GIF8",  # gif
    b"RIFF",  # webp
)


@register(
    "astrbot_plugin_qzone_kit",
    "QingYu",
    "QQ空间精简工具：发说说（带图）、评论、回复评论、点赞、读说说，LLM tools + 管理命令 + WebUI 面板",
    "1.2.0",
)
class QzoneKit(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._client: QzoneClient | None = None
        self._client_cookie: str = ""
        # 记录：slot -> 上次成功时间戳（只有成功才记账）
        self._last_action: dict[str, float] = {}
        self._write_lock_until = 0.0
        self._register_web()

    async def terminate(self):
        if self._client:
            await self._client.close()
            self._client = None

    # ---------- 配置读写 ----------

    def _cfg(self, key, default=None):
        """读配置。真值在分组里（schema 用了 object 嵌套），但老用户的扁平配置也要认。

        顺序：所属分组 → 顶层扁平 → 任意分组。分组优先，这样升级后旧扁平键
        还残留在文件里也不会盖掉分组里的新值。
        """
        try:
            group = CFG_GROUP_OF.get(key)
            if group:
                box = self.config.get(group)
                if isinstance(box, dict) and key in box:
                    value = box.get(key)
                    if value is not None:
                        return value
            if key in self.config:
                value = self.config.get(key, default)
                if value is not None:
                    return value
            for value in self.config.values():
                if isinstance(value, dict) and key in value:
                    got = value.get(key)
                    if got is not None:
                        return got
        except Exception:
            return default
        return default

    def _cfg_items(self) -> list[dict]:
        """给面板「设置」用的字段清单（带当前值）。"""
        items = []
        for key, label, typ, group, hint in CFG_ITEMS:
            default = CFG_DEFAULTS.get(key, "")
            value = self._cfg(key, default)
            if typ == "bool":
                value = bool(value)
            elif typ == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    value = int(default)
            else:
                value = str(value or "")
            items.append({"key": key, "label": label, "type": typ,
                          "group": CFG_GROUP_LABEL.get(group, group),
                          "group_key": group,
                          "group_hint": CFG_GROUP_HINT.get(group, ""),
                          "hint": hint, "value": value, "default": default})
        return items

    def _save_cfg(self, patch: dict) -> tuple[bool, str]:
        """写回插件配置文件并立刻生效（内存 + 磁盘）。"""
        clean: dict = {}
        for key, value in (patch or {}).items():
            if key not in CFG_KEYS:
                continue
            typ = CFG_TYPE_OF.get(key, "str")
            if typ == "bool":
                value = bool(value)
            elif typ == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            else:
                value = str(value or "")
            clean[key] = value
        if not clean:
            return False, "没有可保存的字段"
        for key, value in clean.items():
            group = CFG_GROUP_OF.get(key)
            placed = False
            if group:
                try:
                    box = self.config.get(group)
                    if not isinstance(box, dict):
                        box = {}
                        self.config[group] = box
                    box[key] = value
                    placed = True
                except Exception:
                    logger.warning(f"[QzoneKit] 写入分组 {group} 失败，退回扁平", exc_info=True)
            if not placed:
                try:
                    self.config[key] = value
                except Exception:
                    pass
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                saver()
                return True, "saved"
            except Exception:
                logger.warning("[QzoneKit] save_config 失败，改走直接写文件", exc_info=True)
        try:
            import json
            cfg_path = Path(__file__).parent / "_config_fallback.json"
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(dict(self.config), f, ensure_ascii=False, indent=2)
            return True, "file"
        except Exception as e:
            return False, f"写入失败：{e}"

    # ---------- 基础设施 ----------

    def _get_client(self) -> QzoneClient:
        cookie = str(self._cfg("cookies_str", "") or "").strip()
        if not cookie:
            raise QzoneAuthError("尚未绑定 Cookie：请管理员执行 /qzone bind <Cookie>，或在插件配置里填写 cookies_str")
        if self._client is None or cookie != self._client_cookie:
            if self._client:
                asyncio.ensure_future(self._client.close())
            self._client = QzoneClient(cookie)
            self._client_cookie = cookie
        return self._client

    def _cooldown_check(self, event: AstrMessageEvent, action: str, target: str = "") -> str:
        cooldown = float(self._cfg(f"{action}_cooldown_seconds", 0) or 0)
        if cooldown <= 0:
            return ""
        now = time.time()
        slot = f"{event.unified_msg_origin}|{action}|{target}"
        last = self._last_action.get(slot)
        if last is not None and now - last < cooldown:
            return f"操作太频繁：该动作冷却中（还剩 {cooldown - (now - last):.0f} 秒），本次未执行，请直接回复用户。"
        return ""

    def _cooldown_mark(self, event: AstrMessageEvent, action: str, target: str = "") -> None:
        slot = f"{event.unified_msg_origin}|{action}|{target}"
        self._last_action[slot] = time.time()
        self._write_lock_engage()

    def _write_lock_engage(self) -> None:
        secs = float(self._cfg("write_lock_seconds", 60) or 0)
        if secs > 0:
            self._write_lock_until = time.time() + secs

    def _write_lock_check(self) -> str:
        remain = self._write_lock_until - time.time()
        if remain > 0:
            return (
                f"写操作冷却中（还剩 {remain:.0f} 秒）：上一次写操作刚刚成功，结果已确认有效，"
                "本轮不要再调用任何 Qzone 工具（包括读取类），也不要尝试验证刚完成的操作，"
                "请直接用文字向用户汇报结果。"
            )
        return ""

    _JUNK_STRONG_RE = re.compile(r"忽略|误发|误触|ignore", re.IGNORECASE)
    _JUNK_ONLY_RE = re.compile(
        r"^[\s（(【\[](?:测试|test|ignore|忽略|误发)[\s）)】\]]*[.。!！~]*$",
        re.IGNORECASE,
    )
    _PLACEHOLDER_WRAP_RE = re.compile(r"^[\s（(【\[].+[\s）)】\]][.。!！~]*$", re.DOTALL)
    _JUNK_WEAK_RE = re.compile(
        r"测试消息|测试一下|结束标记|开始标记|test\s*message|placeholder|占位",
        re.IGNORECASE,
    )

    @classmethod
    def _is_junk_message(cls, message: str) -> bool:
        if cls._JUNK_STRONG_RE.search(message):
            return True
        if cls._JUNK_ONLY_RE.match(message):
            return True
        return bool(
            len(message) <= 30
            and cls._PLACEHOLDER_WRAP_RE.match(message)
            and cls._JUNK_WEAK_RE.search(message)
        )

    @staticmethod
    def _check_content(content: str) -> str:
        content = str(content or "").strip()
        if not content:
            return "内容为空，已取消发布。请提供要发布/评论的实际内容。"
        if QzoneKit._is_junk_message(content):
            return "检测到无意义占位内容（请忽略/测试/误发类），已拦截未发布。请直接回复用户，不要调用本工具。"
        return ""

    async def _load_image_bytes(self, source: str) -> tuple[bytes, str]:
        s = str(source).strip().strip('"').strip("'")
        if s.startswith("file:///"):
            s = s[len("file://"):]
        if s.startswith("http://") or s.startswith("https://"):
            client = self._get_client()
            session = await client._ensure_session()
            async with session.get(
                s, headers={"User-Agent": client.UA}, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    raise QzoneError(f"图片下载失败 HTTP {resp.status}: {s[:100]}")
                data = await resp.read()
            name = Path(s.split("?")[0]).name or "img.jpg"
        else:
            p = Path(s)
            if not p.is_file():
                raise QzoneError(f"图片文件不存在: {s}")
            data = p.read_bytes()
            name = p.name
        if not data:
            raise QzoneError(f"图片内容为空: {s}")
        if not any(data.startswith(magic) for magic in _IMAGE_MAGIC):
            raise QzoneError(f"不是可上传的图片文件（需 jpg/png/gif/webp）: {s}")
        return data, name

    def _require_admin(self, event: AstrMessageEvent) -> str:
        if not self._cfg("admin_only", True):
            return ""
        if not event.is_admin():
            return "权限拒绝：只有管理员可以使用 QQ 空间功能。"
        return ""

    _FEATURES = ("list", "post", "comment", "reply", "like", "delete")

    def _feature_enabled(self, action: str) -> bool:
        return bool(self._cfg(f"enable_{action}", True))

    def _feature_check(self, action: str) -> str:
        lock = self._write_lock_check()
        if lock:
            return lock
        if self._feature_enabled(action):
            return ""
        return f"该功能已关闭（{action}）：Qzone 的 {action} 功能当前在插件配置中停用，请直接告知用户此功能未开启，不要尝试其他方式绕过。"

    def _enabled_features(self) -> list[str]:
        return [f for f in self._FEATURES if self._feature_enabled(f)]

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="qzone_list")
    async def qzone_list(self, event: AstrMessageEvent, qq_number: str = "", page: int = 1):
        """查看指定 QQ 号的最近说说列表（每条含 tid、时间、内容摘要和评论列表）。评论、回复、点赞之前必须先调用本工具拿到 tid（fid）和评论 id。

        仅管理员可用；仅在主人明确要求查看或操作 QQ 空间时才调用。
        Args:
            qq_number(string): 目标 QQ 号；留空表示绑定 Cookie 的账号本人
            page(number): 页码，从 1 开始，每页 10 条
        """
        denied = self._require_admin(event)
        if denied:
            return denied
        blocked = self._feature_check("list")
        if blocked:
            return blocked
        try:
            client = self._get_client()
        except QzoneError as exc:
            return str(exc)
        hostuin = 0
        if str(qq_number or "").strip():
            cleaned = str(qq_number).strip().lstrip("oO")
            if not cleaned.isdigit():
                return f"QQ 号格式不对：{qq_number}"
            hostuin = int(cleaned)
        page = max(1, int(page or 1))
        try:
            msgs = await client.list_moods(hostuin=hostuin, pos=(page - 1) * 10, num=10)
        except QzoneAuthError as exc:
            return f"Cookie 失效：{exc} 请提醒管理员重新绑定。"
        except QzoneError as exc:
            return f"读取说说失败：{exc}"
        if not msgs:
            return "该 QQ 没有读到说说（可能没有说说、没有权限或已翻到最后一页）。"
        target_uin = hostuin or client.uin
        lines = [f"QQ {target_uin} 的说说（第 {page} 页，共 {len(msgs)} 条）："]
        for i, m in enumerate(msgs, 1):
            tid = str(m.get("tid") or "")
            created = int(m.get("created_time") or 0)
            time_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(created)) if created else "未知时间"
            content = _strip_html(m.get("content") or "")
            pic_count = len(m.get("pic") or [])
            pic_text = f" [图片x{pic_count}]" if pic_count else ""
            lines.append(f"#{i} fid={tid} | {time_text}{pic_text}")
            preview = content[:100] + ("…" if len(content) > 100 else "")
            if preview:
                lines.append(f"   内容：{preview}")
            comments = m.get("commentlist") or []
            if comments:
                lines.append(f"   共 {m.get('commentnum') or m.get('reply_num') or len(comments)} 条评论：")
                for j, c in enumerate(comments[:10], 1):
                    cid = str(c.get("commentid") or c.get("commentId") or c.get("tid") or "")
                    cuin = str(c.get("uin") or "")
                    ctext = _strip_html(c.get("content") or "")[:50]
                    lines.append(f"   评论{j} comment_id={cid} uin={cuin}: {ctext}")
                    replies = c.get("replylist") or c.get("list_3") or []
                    for k, r in enumerate(replies, 1):
                        rid = str(r.get("commentid") or r.get("commentId") or r.get("tid") or "")
                        ruin = str(r.get("uin") or "")
                        rtext = _strip_html(r.get("content") or "")[:50]
                        lines.append(f"     ↳ 回复{k} comment_id={rid} uin={ruin}: {rtext}")
        lines.append("提示：把 fid 传给点赞/评论工具；回复评论需要评论的 comment_id 和 uin。")
        return "\n".join(lines)

    @filter.llm_tool(name="qzone_post")
    async def qzone_post(self, event: AstrMessageEvent, content: str, images: str = ""):
        """发布一条 QQ 空间说说，可带图片。内容会公开出现在主人自己的 QQ 空间，所有人可见。

        仅管理员可用；仅当主人明确要求发说说/发空间时才调用，一次请求最多调用一次。
        严禁发布"请忽略/测试/占位"类无意义内容。
        Args:
            content(string): 说说文字内容
            images(string): 可选。图片来源，逗号或空格分隔，可以是本地文件路径或 http(s) 图片链接，最多 9 张；不带图则留空
        """
        denied = self._require_admin(event)
        if denied:
            return denied
        blocked = self._feature_check("post")
        if blocked:
            return blocked
        bad = self._check_content(content)
        if bad:
            return bad
        cooldown_hit = self._cooldown_check(event, "post")
        if cooldown_hit:
            return cooldown_hit
        try:
            client = self._get_client()
        except QzoneError as exc:
            return str(exc)
        photos = []
        sources = [s for s in re.split(r"[,，\s]+", str(images or "").strip()) if s]
        if len(sources) > MAX_IMAGES:
            return f"最多只能带 {MAX_IMAGES} 张图片。"
        try:
            for src in sources:
                data, name = await self._load_image_bytes(src)
                photos.append(await client.upload_image(data, name))
            payload = await client.publish_mood(content, photos=photos)
        except QzoneAuthError as exc:
            return f"Cookie 失效，未发布：{exc} 请提醒管理员重新绑定。"
        except QzoneError as exc:
            return f"发布失败：{exc}"
        self._cooldown_mark(event, "post")
        tid = str(payload.get("tid") or payload.get("tId") or "")
        pic_text = f"，带 {len(photos)} 张图" if photos else ""
        return f"说说已发布成功{pic_text}" + (f"（fid={tid}）" if tid else "") + "。到此为止，不要重复发布。"

    @filter.llm_tool(name="qzone_like")
    async def qzone_like(self, event: AstrMessageEvent, qq_number: str, fid: str, unlike: bool = False):
        """给指定说说点赞（或取消赞）。fid 必须来自 qzone_list 的返回结果。

        仅管理员可用；仅当主人明确要求点赞时才调用，一次请求最多调用一次。
        Args:
            qq_number(string): 说说主人的 QQ 号
            fid(string): 说说 ID（qzone_list 返回的 fid/tid）
            unlike(boolean): true 表示取消赞，默认 false 表示点赞
        """
        denied = self._require_admin(event)
        if denied:
            return denied
        blocked = self._feature_check("like")
        if blocked:
            return blocked
        fid = str(fid or "").strip()
        if not fid:
            return "缺少说说 fid。请先调用 qzone_list 查说说列表拿到 fid。"
        cleaned = str(qq_number or "").strip().lstrip("oO")
        if not cleaned.isdigit():
            return f"QQ 号格式不对：{qq_number}（也可以先调 qzone_list 拿到）"
        cooldown_hit = self._cooldown_check(event, "like", fid)
        if cooldown_hit:
            return cooldown_hit
        try:
            client = self._get_client()
            await client.like(int(cleaned), fid, like=not unlike)
        except QzoneAuthError as exc:
            return f"Cookie 失效，未点赞：{exc} 请提醒管理员重新绑定。"
        except QzoneError as exc:
            return f"点赞失败：{exc}"
        self._cooldown_mark(event, "like", fid)
        return ("已取消赞" if unlike else "已点赞") + "。到此为止，不要重复调用。"

    @filter.llm_tool(name="qzone_comment")
    async def qzone_comment(self, event: AstrMessageEvent, qq_number: str, fid: str, content: str):
        """给指定说说发表评论。fid 必须来自 qzone_list 的返回结果。

        仅管理员可用；仅当主人明确要求评论时才调用，一次请求最多调用一次。
        严禁发布"请忽略/测试/占位"类无意义内容。
        Args:
            qq_number(string): 说说主人的 QQ 号
            fid(string): 说说 ID（qzone_list 返回的 fid/tid）
            content(string): 评论内容
        """
        denied = self._require_admin(event)
        if denied:
            return denied
        blocked = self._feature_check("comment")
        if blocked:
            return blocked
        bad = self._check_content(content)
        if bad:
            return bad
        fid = str(fid or "").strip()
        if not fid:
            return "缺少说说 fid。请先调用 qzone_list 查说说列表拿到 fid。"
        cleaned = str(qq_number or "").strip().lstrip("oO")
        if not cleaned.isdigit():
            return f"QQ 号格式不对：{qq_number}（也可以先调 qzone_list 拿到）"
        cooldown_hit = self._cooldown_check(event, "comment", fid)
        if cooldown_hit:
            return cooldown_hit
        try:
            client = self._get_client()
            await client.add_comment(int(cleaned), fid, content)
        except QzoneAuthError as exc:
            return f"Cookie 失效，未评论：{exc} 请提醒管理员重新绑定。"
        except QzoneError as exc:
            return f"评论失败：{exc}"
        self._cooldown_mark(event, "comment", fid)
        return "评论已发表成功。到此为止，不要重复评论。"

    @filter.llm_tool(name="qzone_reply")
    async def qzone_reply(
        self,
        event: AstrMessageEvent,
        qq_number: str,
        fid: str,
        comment_id: str,
        comment_uin: str,
        content: str,
    ):
        """回复某条说说下的某条评论（楼中楼回复）。fid、comment_id、comment_uin 都来自 qzone_list 的返回结果。

        仅管理员可用；仅当主人明确要求回复某条评论时才调用，一次请求最多调用一次。
        严禁发布"请忽略/测试/占位"类无意义内容。
        Args:
            qq_number(string): 说说主人的 QQ 号
            fid(string): 说说 ID
            comment_id(string): 要回复的评论 ID（qzone_list 返回的 comment_id）
            comment_uin(string): 被回复评论作者的 QQ 号
            content(string): 回复内容
        """
        denied = self._require_admin(event)
        if denied:
            return denied
        blocked = self._feature_check("reply")
        if blocked:
            return blocked
        bad = self._check_content(content)
        if bad:
            return bad
        fid = str(fid or "").strip()
        comment_id = str(comment_id or "").strip()
        comment_uin = str(comment_uin or "").strip().lstrip("oO")
        if not fid or not comment_id or not comment_uin.isdigit():
            return "参数不全或格式不对：需要 fid、comment_id 和评论作者的 comment_uin（数字）。请先调用 qzone_list 获取。"
        cleaned = str(qq_number or "").strip().lstrip("oO")
        if not cleaned.isdigit():
            return f"QQ 号格式不对：{qq_number}（也可以先调 qzone_list 拿到）"
        cooldown_hit = self._cooldown_check(event, "comment", fid)
        if cooldown_hit:
            return cooldown_hit
        try:
            client = self._get_client()
            await client.reply_comment(int(cleaned), fid, comment_id, int(comment_uin), content)
        except QzoneAuthError as exc:
            return f"Cookie 失效，未回复：{exc} 请提醒管理员重新绑定。"
        except QzoneError as exc:
            return f"回复失败：{exc}"
        self._cooldown_mark(event, "comment", fid)
        return "回复已发送成功。到此为止，不要重复回复。"

    @filter.llm_tool(name="qzone_delete")
    async def qzone_delete(self, event: AstrMessageEvent, fid: str, created_time: int = 0):
        """删除主人自己发布的一条说说。fid 必须来自 qzone_list 的返回结果。

        仅管理员可用；仅当主人明确要求删除说说时才调用。这是不可恢复的删除操作，调用前应向主人确认。
        Args:
            fid(string): 要删除的说说 ID
            created_time(number): 该说说的发布时间戳（秒，qzone_list 时间的 Unix 时间戳），不知道就传 0
        """
        denied = self._require_admin(event)
        if denied:
            return denied
        blocked = self._feature_check("delete")
        if blocked:
            return blocked
        fid = str(fid or "").strip()
        if not fid:
            return "缺少说说 fid。请先调用 qzone_list 查自己的说说列表拿到 fid。"
        cooldown_hit = self._cooldown_check(event, "delete", fid)
        if cooldown_hit:
            return cooldown_hit
        try:
            client = self._get_client()
            await client.delete_mood(fid, int(created_time or 0))
        except QzoneAuthError as exc:
            return f"Cookie 失效，未删除：{exc} 请提醒管理员重新绑定。"
        except QzoneError as exc:
            return f"删除失败：{exc}"
        self._cooldown_mark(event, "delete", fid)
        return f"说说 {fid} 已删除。"

    # ---------- LLM 提示词注入 ----------

    @filter.on_llm_request()
    async def inject_qzone_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._cfg("inject_prompt", True):
            return
        enabled = self._enabled_features()
        if not enabled:
            return
        name_map = {
            "list": "查说说列表(qzone_list)",
            "post": "发说说(qzone_post)",
            "comment": "评论(qzone_comment)",
            "reply": "回复评论(qzone_reply)",
            "like": "点赞(qzone_like)",
            "delete": "删说说(qzone_delete)",
        }
        enabled_text = "、".join(name_map[f] for f in enabled)
        disabled_text = "、".join(name_map[f] for f in self._FEATURES if f not in enabled)
        lines = [
            "[QQ空间工具使用规范]",
            f"当前可用的 Qzone 工具：{enabled_text}。",
        ]
        if disabled_text:
            lines.append(f"以下工具已停用，绝对不要调用：{disabled_text}。")
        lines += [
            "只有主人明确提出与 QQ 空间相关的请求（看说说/发说说/评论/回复/点赞/删除）时才调用对应工具；闲聊、其他话题一律不调。",
            "同一轮对话中对同一工具最多调用一次；写操作（发/评/赞/删）成功后立即停止，不要为了确认结果而再次调用。",
            "严禁发布或提交「请忽略/测试/占位」类无意义内容；评论回复前必须先 qzone_list 拿 fid；删除不可恢复，操作前先向主人确认。",
            "填写 fid/comment_id/uin 时必须逐字符照抄最近一次 qzone_list 输出中对应楼层/评论的值，禁止凭记忆或感觉填写；发出前默读一遍参数，确认 fid 对应的就是你嘴上说要回复的那条说说。",
        ]
        custom = str(self._cfg("custom_prompt", "") or "").strip()
        if custom:
            lines.append(custom)
        req.system_prompt = (req.system_prompt or "") + "\n" + "\n".join(lines)

    # ---------- 管理命令 ----------

    _SET_KEY_MAP = {
        "list": "enable_list",
        "post": "enable_post",
        "comment": "enable_comment",
        "reply": "enable_reply",
        "like": "enable_like",
        "delete": "enable_delete",
        "admin": "admin_only",
        "主人": "admin_only",
        "仅主人": "admin_only",
        "prompt": "inject_prompt",
        "提示词": "inject_prompt",
    }

    def _switch_summary(self) -> str:
        def on(v):
            return "开" if v else "关"
        return (
            "当前开关状态：\n"
            f"仅主人可用(admin)：{on(self._cfg('admin_only', True))}\n"
            f"提示词注入(prompt)：{on(self._cfg('inject_prompt', True))}\n"
            f"查说说(list)：{on(self._feature_enabled('list'))} | "
            f"发说说(post)：{on(self._feature_enabled('post'))} | "
            f"评论(comment)：{on(self._feature_enabled('comment'))}\n"
            f"回复评论(reply)：{on(self._feature_enabled('reply'))} | "
            f"点赞(like)：{on(self._feature_enabled('like'))} | "
            f"删说说(delete)：{on(self._feature_enabled('delete'))}\n"
            "用法：/qzone set <开关> <on|off>，如 /qzone set post off；省略 on/off 则切换当前状态\n"
            f"面板入口：/api/plug/{PANEL_PREFIX}（发说说/评论/点赞/改配置都在面板里，改完立刻生效）"
        )

    @filter.command("qzone")
    async def qzone_cmd(self, event: AstrMessageEvent):
        """QQ 空间管理命令：/qzone bind <Cookie> | /qzone status | /qzone unbind | /qzone set <开关> <on|off>"""
        denied = self._require_admin(event)
        if denied:
            yield event.plain_result(denied)
            return
        parts = str(event.message_str or "").strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub == "bind":
            if len(parts) < 3:
                yield event.plain_result("用法：/qzone bind <Cookie 字符串>（浏览器登录 user.qzone.qq.com 后 F12 → Network → 复制请求里的 Cookie，需含 p_skey 和 uin）")
                return
            raw = parts[2].strip()
            test = QzoneClient(raw)
            if not test.uin:
                yield event.plain_result("绑定失败：Cookie 里没解析到 uin，确认复制的是登录后的完整 Cookie。")
                return
            if not test.gtk:
                yield event.plain_result("绑定失败：Cookie 里缺少 p_skey 或 skey，无法计算 g_tk。")
                return
            try:
                msgs = await test.list_moods(num=1)
                probe = "Cookie 校验通过"
            except QzoneError as exc:
                probe = f"警告：Cookie 试连未通过（{exc}），仍已保存，可稍后再试 /qzone status"
            await test.close()
            ok, mode = self._save_cfg({"cookies_str": raw})
            if not ok:
                logger.warning(f"[QzoneKit] Cookie 保存失败：{mode}，仅本次会话生效")
            self._client_cookie = ""
            yield event.plain_result(f"已绑定 QQ {test.uin} 的空间 Cookie。{probe}。")
        elif sub == "set":
            arg = parts[2].strip() if len(parts) > 2 else ""
            if not arg:
                yield event.plain_result(self._switch_summary())
                return
            tokens = arg.split()
            key_raw = tokens[0].lower()
            cfg_key = self._SET_KEY_MAP.get(key_raw)
            if cfg_key is None:
                yield event.plain_result(f"不认识的开关：{key_raw}。可用：list/post/comment/reply/like/delete/admin/prompt。{self._switch_summary()}")
                return
            val_raw = tokens[1].lower() if len(tokens) > 1 else ""
            if val_raw in ("on", "开", "true", "1", "启用", "开启"):
                new_val = True
            elif val_raw in ("off", "关", "false", "0", "停用", "关闭"):
                new_val = False
            elif not val_raw:
                new_val = not bool(self._cfg(cfg_key, True))
            else:
                yield event.plain_result(f"不认识的状态：{val_raw}，用 on 或 off。")
                return
            ok, mode = self._save_cfg({cfg_key: new_val})
            if not ok:
                logger.warning(f"[QzoneKit] 开关保存失败：{mode}，仅本次会话生效")
            state = "开启" if new_val else "关闭"
            yield event.plain_result(f"已{state} {key_raw}（{cfg_key}={str(new_val).lower()}）。")
        elif sub == "unbind":
            self._save_cfg({"cookies_str": ""})
            self._client_cookie = ""
            yield event.plain_result("已解绑空间 Cookie。")
        else:
            try:
                client = self._get_client()
            except QzoneError as exc:
                yield event.plain_result(f"状态：{exc}")
                return
            yield event.plain_result(
                f"已绑定账号：{client.uin}\nCookie 字段数：{len(client.cookies)}\ng_tk：{client.gtk}\n正在校验登录态…"
            )
            try:
                msgs = await client.list_moods(num=1)
                yield event.plain_result(f"登录态有效（本人最新说说 {len(msgs)} 条可读）。")
            except QzoneError as exc:
                yield event.plain_result(f"登录态异常：{exc}\n请重新执行 /qzone bind 绑定新 Cookie。")

    # ---------- WebUI 面板 ----------

    def _register_web(self):
        try:
            register_api = self.context.register_web_api
        except Exception:
            logger.warning("[QzoneKit] register_web_api 不可用，WebUI 面板未启用")
            return
        register_api(f"{PANEL_PREFIX}", self._panel_page, ["GET"], "QQ空间面板")
        register_api(f"{API_PREFIX}/overview", self._api_overview, ["GET"], "总览")
        register_api(f"{API_PREFIX}/moods", self._api_moods, ["GET"], "说说列表")
        register_api(f"{API_PREFIX}/post", self._api_post, ["POST"], "发说说")
        register_api(f"{API_PREFIX}/like", self._api_like, ["POST"], "点赞/取消赞")
        register_api(f"{API_PREFIX}/comment", self._api_comment, ["POST"], "评论说说")
        register_api(f"{API_PREFIX}/reply", self._api_reply, ["POST"], "回复评论")
        register_api(f"{API_PREFIX}/delete", self._api_delete, ["POST"], "删说说")
        register_api(f"{API_PREFIX}/config", self._api_config, ["GET"], "总配置读取")
        register_api(f"{API_PREFIX}/config/set", self._api_config_set, ["POST"], "总配置保存")

    async def _panel_page(self):
        page = Path(__file__).parent / "pages" / "空间面板" / "index.html"
        return await send_file(str(page))

    def _ok(self, data=None) -> dict:
        return {"status": "ok", "data": data or {}}

    def _err(self, msg: str) -> dict:
        return {"status": "error", "message": msg}

    def _overview(self) -> dict:
        cookie = str(self._cfg("cookies_str", "") or "").strip()
        client = None
        if cookie:
            try:
                client = self._get_client()
            except Exception:
                client = None
        remain = self._write_lock_until - time.time()
        return {
            "bound": bool(cookie),
            "uin": client.uin if client else 0,
            "cookie_fields": len(client.cookies) if client else 0,
            "gtk": client.gtk if client else 0,
            "features": {f: self._feature_enabled(f) for f in self._FEATURES},
            "admin_only": bool(self._cfg("admin_only", True)),
            "inject_prompt": bool(self._cfg("inject_prompt", True)),
            "write_lock_remain": max(0.0, remain),
            "cooldowns": {
                a: int(self._cfg(f"{a}_cooldown_seconds", 0) or 0)
                for a in ("post", "comment", "like", "delete")
            },
            "panel_url": f"/api/plug/{PANEL_PREFIX}",
        }

    async def _api_overview(self):
        return self._ok(self._overview())

    async def _api_moods(self):
        from quart import request as quart_request
        qq = str(quart_request.args.get("qq") or "").strip().lstrip("oO")
        try:
            page = max(1, int(quart_request.args.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        try:
            client = self._get_client()
        except QzoneError as exc:
            return self._err(str(exc))
        hostuin = client.uin
        if qq:
            if not qq.isdigit():
                return self._err(f"QQ 号格式不对：{qq}")
            hostuin = int(qq)
        try:
            msgs, total = await client.list_moods_page(hostuin=hostuin, pos=(page - 1) * 10, num=10)
        except QzoneAuthError as exc:
            return self._err(f"Cookie 失效：{exc} 请重新绑定。")
        except QzoneError as exc:
            return self._err(f"读取说说失败：{exc}")
        per_page = 10
        if total > 0:
            pages = max(1, (total + per_page - 1) // per_page)
        else:
            # 接口没给总数时别急着说"到底了"，留一页余地让前端继续翻
            pages = page + 1
        items = []
        for m in msgs or []:
            pics = []
            for p in (m.get("pic") or []):
                u = p.get("url3") or p.get("url2") or p.get("url1") or p.get("url4") or ""
                if u:
                    pics.append(u)
            comments = []
            for c in (m.get("commentlist") or [])[:30]:
                replies = []
                for r in (c.get("list_3") or c.get("replylist") or [])[:10]:
                    replies.append({
                        "cid": str(r.get("commentid") or r.get("commentId") or r.get("tid") or ""),
                        "uin": str(r.get("uin") or ""),
                        "content": _strip_html(r.get("content") or ""),
                    })
                comments.append({
                    "cid": str(c.get("commentid") or c.get("commentId") or c.get("tid") or ""),
                    "uin": str(c.get("uin") or ""),
                    "content": _strip_html(c.get("content") or ""),
                    "time": int(c.get("time") or c.get("create_time") or 0),
                    "replies": replies,
                })
            mood_uin = str(m.get("uin") or hostuin)
            items.append({
                "tid": str(m.get("tid") or ""),
                "uin": mood_uin,
                "created_time": int(m.get("created_time") or 0),
                "content": _strip_html(m.get("content") or ""),
                "pics": pics,
                "isliked": bool(m.get("isliked")),
                "commentnum": int(m.get("commentnum") or m.get("reply_num") or len(comments)),
                "comments": comments,
                "own": mood_uin == str(client.uin),
            })
        return self._ok({
            "page": page,
            "pages": pages,
            "total": total,
            "per_page": per_page,
            "uin": hostuin,
            "items": items,
        })

    async def _api_post(self):
        from quart import request as quart_request
        payload = await quart_request.get_json(silent=True) or {}
        content = str(payload.get("content") or "").strip()
        bad = self._check_content(content)
        if bad:
            return self._err(bad)
        try:
            client = self._get_client()
        except QzoneError as exc:
            return self._err(str(exc))
        photos = []
        sources = [s for s in re.split(r"[,，\s]+", str(payload.get("images") or "").strip()) if s]
        if len(sources) > MAX_IMAGES:
            return self._err(f"最多只能带 {MAX_IMAGES} 张图片。")
        try:
            for src in sources:
                data, name = await self._load_image_bytes(src)
                photos.append(await client.upload_image(data, name))
            result = await client.publish_mood(content, photos=photos)
        except QzoneAuthError as exc:
            return self._err(f"Cookie 失效，未发布：{exc}")
        except QzoneError as exc:
            return self._err(f"发布失败：{exc}")
        tid = str(result.get("tid") or result.get("tId") or "")
        return self._ok({"tid": tid, "pics": len(photos)})

    async def _api_like(self):
        from quart import request as quart_request
        payload = await quart_request.get_json(silent=True) or {}
        fid = str(payload.get("fid") or "").strip()
        qq = str(payload.get("qq") or "").strip().lstrip("oO")
        unlike = bool(payload.get("unlike"))
        if not fid or not qq.isdigit():
            return self._err("缺少 fid 或 QQ 号格式不对")
        try:
            client = self._get_client()
            await client.like(int(qq), fid, like=not unlike)
        except QzoneAuthError as exc:
            return self._err(f"Cookie 失效：{exc}")
        except QzoneError as exc:
            return self._err(f"点赞失败：{exc}")
        return self._ok({"liked": not unlike})

    async def _api_comment(self):
        from quart import request as quart_request
        payload = await quart_request.get_json(silent=True) or {}
        fid = str(payload.get("fid") or "").strip()
        qq = str(payload.get("qq") or "").strip().lstrip("oO")
        content = str(payload.get("content") or "").strip()
        if not fid or not qq.isdigit():
            return self._err("缺少 fid 或 QQ 号格式不对")
        if not content:
            return self._err("评论内容为空")
        try:
            client = self._get_client()
            await client.add_comment(int(qq), fid, content)
        except QzoneAuthError as exc:
            return self._err(f"Cookie 失效：{exc}")
        except QzoneError as exc:
            return self._err(f"评论失败：{exc}")
        return self._ok()

    async def _api_reply(self):
        from quart import request as quart_request
        payload = await quart_request.get_json(silent=True) or {}
        fid = str(payload.get("fid") or "").strip()
        qq = str(payload.get("qq") or "").strip().lstrip("oO")
        cid = str(payload.get("cid") or "").strip()
        cuin = str(payload.get("cuin") or "").strip().lstrip("oO")
        content = str(payload.get("content") or "").strip()
        if not fid or not cid or not cuin.isdigit() or not qq.isdigit():
            return self._err("参数不全：需要 fid、cid、cuin（数字）和 qq")
        if not content:
            return self._err("回复内容为空")
        try:
            client = self._get_client()
            await client.reply_comment(int(qq), fid, cid, int(cuin), content)
        except QzoneAuthError as exc:
            return self._err(f"Cookie 失效：{exc}")
        except QzoneError as exc:
            return self._err(f"回复失败：{exc}")
        return self._ok()

    async def _api_delete(self):
        from quart import request as quart_request
        payload = await quart_request.get_json(silent=True) or {}
        fid = str(payload.get("fid") or "").strip()
        try:
            created = int(payload.get("created_time") or 0)
        except (TypeError, ValueError):
            created = 0
        if not fid:
            return self._err("缺少 fid")
        try:
            client = self._get_client()
            await client.delete_mood(fid, created)
        except QzoneAuthError as exc:
            return self._err(f"Cookie 失效：{exc}")
        except QzoneError as exc:
            return self._err(f"删除失败：{exc}")
        return self._ok({"deleted": fid})

    async def _api_config(self):
        return self._ok({
            "items": self._cfg_items(),
            "overview": self._overview(),
        })

    async def _api_config_set(self):
        from quart import request as quart_request
        data = await quart_request.get_json(silent=True) or {}
        patch = data.get("patch") if isinstance(data.get("patch"), dict) else data
        ok, info = self._save_cfg(patch)
        if not ok:
            return self._err(info)
        return self._ok({
            "saved": True, "mode": info,
            "items": self._cfg_items(), "overview": self._overview(),
        })
