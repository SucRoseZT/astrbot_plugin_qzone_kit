"""QQ 空间精简 API 客户端（aiohttp，无额外依赖）。

端点与参数提炼自 astrbot_plugin_qzone_ultra 的 qzone_bridge.client，
只保留发说说（带图）、读说说、评论、回复评论、点赞、删除六个动作。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import unicodedata

import aiohttp

IMAGE_UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
MSGLIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
# 楼中楼：先走 re_feeds（带 commentId/commentUin 时它确实会按楼中楼处理，
# 假的 fid 会回 -10004 参数错误并原样回显 t2_uin/t2_tid，说明参数是被认的），
# 再退 h5 域名的同一个 CGI，最后退 emotion_cgi_addreply_ugc（UGC 回复接口，
# 2026-11-05 实测存在：假 fid 返回 {"code":-8,"message":"对不起，原文已经被删除"}）。
REPLY_URLS = (
    COMMENT_URL,
    "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds",
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_addreply_ugc",
)
H5_INDEX_URL = "https://h5.qzone.qq.com/mqzone/index"
H5_PROFILE_URL = "https://h5.qzone.qq.com/mqzone/profile"
_SHINE_TOKEN_RE = re.compile(r'window\.shine0callback.*?return\s*"([0-9a-f]+?)"\s*;', re.S)
_G_QZONETOKEN_RE = re.compile(r'window\.g_qzonetoken\s*=\s*["\']([0-9a-zA-Z]+)["\']')
DELETE_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
LIKE_PROXY_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
UNLIKE_PROXY_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_unlike_app"
LIKE_DIRECT_URL = "https://w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
UNLIKE_DIRECT_URL = "https://w.qzone.qq.com/cgi-bin/likes/internal_unlike_app"
# 点赞明细唯一还活着的来源（get_like_list_app 已稳定 500，2026-11-03 实测）
FEEDS_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"

MAX_IMAGES = 9
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_AUTH_KEYWORDS = ("登录", "失效", "请先登录", "expired", "not login", "未登录")
AUTH_ERROR_CODES = {-3000}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CALLBACK_RE = re.compile(r"^\s*[\w$.]+\s*\((.*)\)\s*;?\s*$", re.S)


class QzoneError(Exception):
    """QQ 空间接口错误。"""


class QzoneAuthError(QzoneError):
    """登录态失效，需要重新绑定 Cookie。"""


def hash33(key: str, phash: int = 0) -> int:
    for ch in key:
        phash += (phash << 5) + ord(ch)
    return 0x7FFFFFFF & phash


def gtk_from_skey(skey: str) -> int:
    if not skey:
        return 0
    return hash33(skey, 5381)


def parse_cookie_str(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in str(raw or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip()
        if key and value:
            cookies.setdefault(key, value)
    if "uin" in cookies and "p_uin" not in cookies:
        cookies["p_uin"] = cookies["uin"]
    if "p_uin" in cookies and "uin" not in cookies:
        cookies["uin"] = cookies["p_uin"]
    return cookies


def uin_from_cookies(cookies: dict[str, str]) -> int:
    for key in ("uin", "p_uin", "ptui_loginuin", "luin"):
        value = cookies.get(key)
        if not value:
            continue
        cleaned = str(value).strip().lstrip("oO")
        if cleaned.isdigit():
            return int(cleaned)
    return 0


def _balanced_json_end(text: str, start: int) -> int:
    """从 text[start]（应为 '{' 或 '['）开始扫描配对括号，返回结束下标（不含），失败返回 -1。"""
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def parse_payload(text: str):
    text = (text or "").strip()
    if not text:
        return {}
    match = _CALLBACK_RE.match(text)
    if match:
        text = match.group(1)
    else:
        idx = text.find("frameElement.callback(")
        if idx >= 0:
            start = text.find("(", idx) + 1
            while start < len(text) and text[start] in " \t\r\n":
                start += 1
            if start < len(text) and text[start] in "{[":
                end = _balanced_json_end(text, start)
                if end > start:
                    text = text[start:end]
    try:
        return json.loads(text)
    except Exception as exc:
        raise QzoneError(f"QQ 空间接口返回无法解析: {text[:120]}") from exc


def _payload_code(payload) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("ret", "code"):
        if key in payload and payload.get(key) not in (None, ""):
            try:
                return int(payload.get(key) or 0)
            except (TypeError, ValueError):
                return None
    return None


def _unwrap(payload):
    while isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and len(payload) <= 3:
            payload = data
            continue
        break
    return payload


def _check_payload(payload):
    code = _payload_code(payload)
    if code in (None, 0):
        return payload
    msg = str(
        (payload.get("msg") if isinstance(payload, dict) else "")
        or (payload.get("message") if isinstance(payload, dict) else "")
        or ""
    )
    lowered = msg.lower()
    if code in AUTH_ERROR_CODES or any(k in msg or k in lowered for k in _AUTH_KEYWORDS):
        raise QzoneAuthError(msg or "QQ 空间登录态已失效，请重新绑定 Cookie")
    raise QzoneError(_describe_code(code, msg, payload))


# 已知返回码的人话解释。只写实测/社区反复验证过的，其余原样抛出。
_CODE_HINTS = {
    -10049: (
        "空间拒绝了这次写操作（-10049）。读接口不校验这些，所以典型表现是「能读不能写」。"
        "常见原因按概率排：① comment_uin 不是被回复评论的真实作者（填成了自己或说说主人，"
        "空间会直接判非法目标）；② 目标说说设置了评论权限/评论审核，或对方把你删了好友；"
        "③ 这个 Cookie 的账号写操作被风控，需要先在手机 QQ 里人工发一条评论过掉验证码"
        "（返回里 needVerify=1 就是这一种）。"
    ),
    -10052: "该条内容已被删除，或 comment_id 不存在（-10052）。请重新拉一次说说列表再试。",
    -10004: "参数错误（-10004）：说说/评论 ID 对不上。多半是 fid 或 comment_id 抄错了。",
    -10000: "参数错误（-10000）：缺少必填字段或格式不对。",
    -10013: "没有权限（-10013）：对方限制了评论，或不是好友。",
    -8: "原文已被删除或不可见（-8）。",
    -10005: "内容为空（-10005）：请求已经到服务端、鉴权也过了，只是内容被判为空。"
             "自检里拿到这个码说明写接口这条路是通的。",
}


def _describe_code(code: int, msg: str, payload=None) -> str:
    head = f"QQ 空间接口错误 {code}" + (f"（{msg}）" if msg else "")
    notes = []
    hint = _CODE_HINTS.get(code)
    if hint:
        notes.append(hint)
    extra = []
    if isinstance(payload, dict):
        subcode = payload.get("subcode")
        if subcode not in (None, "", 0):
            extra.append(f"subcode={subcode}")
        if payload.get("needVerify") not in (None, "", 0):
            extra.append(f"needVerify={payload.get('needVerify')}")
        result = payload.get("result")
        if isinstance(result, dict) and result.get("code") not in (None, 0, code):
            extra.append(f"result.code={result.get('code')}")
    if extra:
        notes.append("返回细节：" + " ".join(extra))
    elif not msg:
        notes.append(f"原始返回：{str(payload)[:200]}")
    return " ".join([head] + notes)


def _extract_pic_bo(url: str) -> str:
    if not url or "bo=" not in url:
        return ""
    result = url.split("bo=", 1)[1]
    for token in ("!!", "&", "#"):
        result = result.split(token, 1)[0]
    import urllib.parse

    return urllib.parse.unquote(result)


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", str(text or ""))
    return re.sub(r"<[^>]+>", "", text).strip()


class QzoneClient:
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    def __init__(self, cookie_str: str, timeout: float = 20.0, on_auth_error=None):
        self.cookies = parse_cookie_str(cookie_str)
        self.uin = uin_from_cookies(self.cookies)
        self.gtk = gtk_from_skey(
            self.cookies.get("p_skey") or self.cookies.get("skey") or ""
        )
        # 撞到登录态失效时回调一次，插件那边靠它立刻醒来补 Cookie
        self._on_auth_error = on_auth_error
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._qzonetokens: dict[str, str] = {}
        self._qzonetoken_at: dict[str, float] = {}
        self._diagnose_at: float = 0.0

    @property
    def cookie_text(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _notify_auth_error(self, msg: str) -> None:
        """登录态失效的唯一出口。回调必须足够轻，出什么错都不能影响原异常。"""
        cb = self._on_auth_error
        if not cb:
            return
        try:
            cb(str(msg or ""))
        except Exception:  # noqa: BLE001
            pass

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # DummyCookieJar：不让任何 Set-Cookie 进入会话，永远只用显式 Cookie 头，
            # 避免 get_qzonetoken 等 GET 请求的响应 Cookie 污染后续 POST（实测会触发接口 500）
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, cookie_jar=aiohttp.DummyCookieJar()
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        referer: str = "",
        origin: str = "https://user.qzone.qq.com",
        max_retries: int = 3,
    ):
        if not self.cookies:
            raise QzoneAuthError("尚未绑定 Cookie，请先执行 /qzone bind")
        if not self.gtk:
            self._notify_auth_error("Cookie 缺少 p_skey 或 skey，无法计算 g_tk")
            raise QzoneAuthError("Cookie 缺少 p_skey 或 skey，无法计算 g_tk")
        merged = dict(params or {})
        merged.setdefault("g_tk", self.gtk)
        headers = {
            "User-Agent": self.UA,
            "Cookie": self.cookie_text,
            "Referer": referer or f"https://user.qzone.qq.com/{self.uin}",
            "Origin": origin,
            "X-Requested-With": "XMLHttpRequest",
        }
        session = await self._ensure_session()
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                async with session.request(
                    method, url, params=merged, data=data,
                    headers=headers, allow_redirects=False,
                ) as resp:
                    if resp.status in _REDIRECT_STATUSES:
                        location = resp.headers.get("Location", "")
                        lowered = location.lower()
                        if "ptlogin" in lowered or "login" in lowered or "check" in lowered:
                            self._notify_auth_error("登录态已失效，请重新绑定 Cookie")
                            raise QzoneAuthError("登录态已失效，请重新绑定 Cookie")
                        raise QzoneError(f"接口跳转异常 {resp.status}: {location[:120]}")
                    if resp.status == 403:
                        raise QzoneError("QQ 空间拒绝访问(403)，可能没有权限或触发风控")
                    if resp.status == 429:
                        raise QzoneError("请求太频繁(429)，请稍后再试")
                    if resp.status >= 400:
                        raise QzoneError(f"接口 HTTP {resp.status}")
                    text = await resp.text()
                try:
                    payload = _check_payload(parse_payload(text))
                    return _check_payload(_unwrap(payload))
                except QzoneAuthError as exc:
                    self._notify_auth_error(str(exc))
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= max_retries:
                    raise QzoneError(f"网络请求失败: {exc}") from exc
                await asyncio.sleep(min(2.0 * attempt, 6.0))
        raise QzoneError(f"网络请求失败: {last_exc}")

    # ---------- 图片上传 ----------

    async def upload_image(self, data: bytes, filename: str = "img.jpg") -> dict:
        if not self.uin:
            raise QzoneAuthError("Cookie 缺少 UIN，无法上传图片")
        if not data:
            raise QzoneError("图片内容为空")
        if len(data) > MAX_IMAGE_BYTES:
            raise QzoneError("图片超过 8MB，无法上传")
        encoded = base64.b64encode(data).decode("ascii")
        skey = self.cookies.get("skey") or self.cookies.get("p_skey") or ""
        p_skey = self.cookies.get("p_skey") or self.cookies.get("skey") or ""
        form = {
            "filename": filename,
            "uin": str(self.uin),
            "skey": skey,
            "zzpaneluin": str(self.uin),
            "p_uin": str(self.uin),
            "p_skey": p_skey,
            "uploadtype": "1",
            "albumtype": "7",
            "exttype": "0",
            "refer": "shuoshuo",
            "output_type": "json",
            "charset": "utf-8",
            "output_charset": "utf-8",
            "upload_hd": "1",
            "hd_width": "2048",
            "hd_height": "10000",
            "hd_quality": "96",
            "backUrls": (
                "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
            ),
            "url": f"{IMAGE_UPLOAD_URL}?g_tk={self.gtk}",
            "base64": "1",
            "picfile": encoded,
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
        }
        payload = await self._request(
            "POST",
            IMAGE_UPLOAD_URL,
            params={"g_tk": self.gtk},
            data=form,
            referer=f"https://user.qzone.qq.com/{self.uin}",
        )
        albumid = str(payload.get("albumid") or payload.get("albumId") or "")
        lloc = str(payload.get("lloc") or payload.get("LLoc") or "")
        sloc = str(payload.get("sloc") or payload.get("SLoc") or "")
        if not albumid or not lloc or not sloc:
            raise QzoneError(f"图片上传返回缺少 richval 字段: {payload}")
        photo_type = str(payload.get("type") or ("png" if filename.endswith(".png") else "jpg"))
        height = str(payload.get("height") or payload.get("h") or 0)
        width = str(payload.get("width") or payload.get("w") or 0)
        url = str(
            payload.get("url")
            or payload.get("origin_url")
            or payload.get("originUrl")
            or payload.get("pre")
            or ""
        )
        pic_bo = str(payload.get("pic_bo") or payload.get("picBo") or _extract_pic_bo(url) or "")
        richval = f",{albumid},{lloc},{sloc},{photo_type},{height},{width},,{height},{width}"
        return {"richval": richval, "pic_bo": pic_bo}

    async def publish_mood(
        self,
        content: str,
        photos: list[dict] | None = None,
        sync_weibo: bool = False,
    ) -> dict:
        photos = photos or []
        richval = "\t".join(p["richval"] for p in photos)
        pic_bo = ",".join(p.get("pic_bo", "") for p in photos)
        form = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": 1,
            "to_sign": 0,
            "hostuin": str(self.uin),
            "code_version": "1",
            "richval": richval,
            "issyncweibo": int(bool(sync_weibo)),
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
        }
        if photos:
            form.update(
                {
                    "richtype": "1",
                    "subrichtype": "1",
                    "pic_bo": pic_bo,
                    "pic_template": "",
                }
            )
        return await self._request(
            "POST",
            PUBLISH_URL,
            data=form,
            referer=f"https://user.qzone.qq.com/{self.uin}",
        )

    # ---------- 读说说 ----------

    async def list_moods(self, hostuin: int | None = None, pos: int = 0, num: int = 10) -> list[dict]:
        msgs, _total = await self.list_moods_page(hostuin=hostuin, pos=pos, num=num)
        return msgs

    async def list_moods_page(
        self, hostuin: int | None = None, pos: int = 0, num: int = 10
    ) -> tuple[list[dict], int]:
        """读说说列表，额外带回总数（面板算总页数用）。"""
        hostuin = int(hostuin or self.uin)
        params = {
            "uin": hostuin,
            "hostUin": hostuin,
            "pos": max(0, int(pos)),
            "num": max(1, min(int(num), 20)),
            "replynum": 100,
            "callback": "_preloadCallback",
            "code_version": 1,
            "format": "json",
            "need_comment": 1,
            "need_private_comment": 1,
        }
        payload = await self._request(
            "GET",
            MSGLIST_URL,
            params=params,
            referer=f"https://user.qzone.qq.com/{hostuin}",
        )
        msgs = payload.get("msglist")
        try:
            total = int(payload.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return (msgs if isinstance(msgs, list) else []), max(0, total)

    async def find_mood(self, hostuin: int, fid: str, max_pages: int = 3) -> dict | None:
        """按 tid 在某个人的说说列表里翻出这条说说（最多翻 max_pages 页）。"""
        hostuin = int(hostuin or self.uin)
        fid = str(fid or "")
        for page in range(max(1, int(max_pages))):
            msgs, _total = await self.list_moods_page(hostuin=hostuin, pos=page * 10, num=10)
            for mood in msgs:
                if str(mood.get("tid") or "") == fid:
                    return mood
            if len(msgs) < 10:
                break
        return None

    @staticmethod
    def comment_authors(mood: dict) -> dict[str, int]:
        """{comment_id: 作者 uin}，一级评论和楼中楼都收。

        楼中楼报 -10049 最常见的原因就是把 commentUin 填成了自己或说说主人 ——
        有了这张表就能在发请求前用服务端数据把 uin 校正回来。
        """
        authors: dict[str, int] = {}

        def take(node) -> None:
            if not isinstance(node, dict):
                return
            cid = str(
                node.get("commentid") or node.get("commentId") or node.get("tid") or ""
            ).strip()
            raw_uin = str(node.get("uin") or "").strip().lstrip("oO")
            if cid and raw_uin.isdigit() and int(raw_uin) > 0:
                authors.setdefault(cid, int(raw_uin))

        for comment in mood.get("commentlist") or []:
            take(comment)
            for reply in comment.get("replylist") or comment.get("list_3") or []:
                take(reply)
        return authors

    # ---------- 点赞明细 ---------- #

    _LIKE_ITEM_RE = re.compile(
        r'data-unikey="https?://user\.qzone\.qq\.com/(\d+)/mood/([0-9a-fA-F]+)"'
    )
    _LIKE_MODULE_RE = re.compile(
        r'((?:<a[^>]*class="[^"]*q_namecard[^"]*"[^>]*>[^<]*</a>(?:、)?)+)'
        r'等<span class="f-like-cnt">(\d+)</span>人觉得很赞'
    )
    _LIKE_NAME_RE = re.compile(r"<a[^>]*>([^<]+)</a>")

    async def get_feed_like_map(self, num: int = 30) -> dict:
        """抓一次空间动态流，整理成 {tid: {"count": n, "names": [...]}}。

        为什么不用 get_like_list_app：该接口 2026-11-03 线上实测稳定 500——
        换过 g_tk 来源（p_skey / skey / 不带）、GET 与 POST、user / h5 两个
        域名、加 if_theme / callback / format，全部一样。老的点赞列表接口已经
        不能用。现在点赞明细只剩 feeds3_html_more 这一条：它返回的是 HTML 片段，
        里面直接写着「A、B 等 N 人觉得很赞」，配 data-unikey 能跟 tid 对齐。

        局限：只有落在动态流里的说说才有数据（自己发的、好友近期的）。流里没有
        的 tid 不会出现在返回值里，调用方必须按「未知」处理，不能当成「没人赞」。
        """
        if not self.cookies:
            raise QzoneAuthError("尚未绑定 Cookie，请先执行 /qzone bind")
        params = {
            "uin": self.uin,
            "scope": 0,
            "view": 1,
            "filter": "all",
            "flag": 1,
            "appid": 311,
            "daylist": "",
            "uinlist": "",
            "gid": "",
            "aim": 1,
            "sort": 0,
            "pos": 0,
            "num": max(5, min(int(num or 30), 50)),
            "g_tk": self.gtk,
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "frequency": 1,
            "refresh": 0,
            "style": 1,
            "useutf8": 1,
        }
        session = await self._ensure_session()
        headers = {
            "User-Agent": self.UA,
            "Cookie": self.cookie_text,
            "Referer": f"https://user.qzone.qq.com/{self.uin}",
        }
        try:
            async with session.get(
                FEEDS_URL, params=params, headers=headers, allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    return {}
                raw = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return {}
        return self._parse_feed_likes(raw)

    @classmethod
    def _parse_feed_likes(cls, raw: str) -> dict:
        """从动态流的 JSON-in-JS 里把点赞模块抠出来。

        返回体不是标准 JSON（键名不带引号），但内容是转义过的 HTML，所以不解析
        JSON，直接反转义后按 data-unikey 切片，每片里找「…等 N 人觉得很赞」。
        """
        if not raw:
            return {}
        text = (
            raw.replace("\\x22", '"')
            .replace("\\x3C", "<")
            .replace("\\x3E", ">")
            .replace("\\/", "/")
        )
        hits = list(cls._LIKE_ITEM_RE.finditer(text))
        out: dict[str, dict] = {}
        for i, hit in enumerate(hits):
            tid = hit.group(2)
            end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
            chunk = text[hit.end():end]
            module = cls._LIKE_MODULE_RE.search(chunk)
            if not module:
                continue
            names = []
            for raw_name in cls._LIKE_NAME_RE.findall(module.group(1)):
                # 昵称里常混零宽 / 标签字符（实测有 U+E0000，category 是 Cn），
                # 整类 C* 连同变体选择符一起清掉再判空。
                clean = "".join(
                    ch for ch in raw_name
                    if unicodedata.category(ch)[0] != "C"
                    and not (0xFE00 <= ord(ch) <= 0xFE0F)
                ).strip()
                if clean and clean not in names:
                    names.append(clean)
            try:
                count = int(module.group(2))
            except (TypeError, ValueError):
                count = len(names)
            if count <= 0 and not names:
                continue
            prev = out.get(tid)
            if prev and int(prev.get("count") or 0) >= count:
                continue
            out[tid] = {"count": count, "names": names}
        return out

    # ---------- 评论 / 回复 ----------

    async def get_qzonetoken(self, hostuin: int) -> str:
        """评论接口的 qzonetoken，藏在 H5 页面的 shine0callback 脚本里，按 hostuin 缓存 30 分钟。

        两个坑（2026-11-05 实测）：
        1. 这个请求以前**没带 Cookie**，会话又是 DummyCookieJar，等于匿名访问 H5，
           h5.qzone.qq.com/mqzone/index 直接 302 回 user.qzone.qq.com，正文为空，
           所以永远取不到 token —— 现在补上 Cookie 并跟随跳转；
        2. 桌面版首页里 window.g_qzonetoken 现在是空串，说明腾讯把这条链路弱化了，
           取不到属于正常情况，**不能再把空 token 当致命错误**，只是不附这个参数。
        """
        key = str(int(hostuin or self.uin))
        cached = self._qzonetokens.get(key)
        if cached and time.time() - self._qzonetoken_at.get(key, 0) < 1800:
            return cached
        session = await self._ensure_session()
        if int(hostuin or 0) == self.uin:
            url, referer, params = H5_INDEX_URL, f"https://user.qzone.qq.com/{self.uin}", None
        else:
            url, referer = H5_PROFILE_URL, f"https://user.qzone.qq.com/{hostuin}"
            params = {"hostuin": int(hostuin), "starttime": int(time.time() * 1000)}
        headers = {
            "User-Agent": self.UA,
            "Cookie": self.cookie_text,
            "Referer": referer,
        }
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                html = await resp.text(errors="ignore")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return ""
        match = _SHINE_TOKEN_RE.search(html) or _G_QZONETOKEN_RE.search(html)
        if not match:
            return ""
        token = (match.group(1) or "").strip()
        if not token:
            return ""
        self._qzonetokens[key] = token
        self._qzonetoken_at[key] = time.time()
        return token

    def invalidate_qzonetoken(self, hostuin: int | None = None) -> None:
        if hostuin is None:
            self._qzonetokens.clear()
            self._qzonetoken_at.clear()
        else:
            key = str(int(hostuin or self.uin))
            self._qzonetokens.pop(key, None)
            self._qzonetoken_at.pop(key, None)

    async def _post_comment_form(self, form: dict, referer: str, hostuin: int) -> dict:
        """评论/回复共用：带 token 发一次，失败就丢缓存换新 token 再发一次。

        token 为空本身不算错（2026 年这块已经被腾讯弱化），只是不附参数；
        但如果第一次吃了业务错误，很可能是缓存里的 token 过期，换一次值得。
        """
        token = await self.get_qzonetoken(hostuin)
        if token:
            form["qzonetoken"] = token
        try:
            return await self._request("POST", COMMENT_URL, data=form, referer=referer)
        except QzoneAuthError:
            raise
        except QzoneError:
            if not token:
                raise
            self.invalidate_qzonetoken(hostuin)
            fresh = await self.get_qzonetoken(hostuin)
            if not fresh or fresh == token:
                raise
            form["qzonetoken"] = fresh
            return await self._request("POST", COMMENT_URL, data=form, referer=referer)

    def _comment_form(
        self,
        hostuin: int,
        fid: str,
        content: str,
        private: int = 0,
        commentid: str | None = None,
        comment_uin: int | None = None,
    ) -> dict:
        """评论和楼中楼共用的表单。带 commentId/commentUin 就是楼中楼。

        诊断用的空内容探活也走这里，保证探针和真实请求同一条链路。
        """
        hostuin = int(hostuin)
        referer = f"https://user.qzone.qq.com/{hostuin}/mood/{fid}"
        form = {
            "topicId": f"{hostuin}_{fid}__1",
            "uin": str(self.uin),
            "hostUin": hostuin,
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "platformid": 50,
            "format": "fs",
            "ref": "feeds",
            "content": content,
            "private": private,
            "paramstr": "1",
            "isSignIn": "0",
            "richval": "",
            "richtype": "",
            "appid": 311,
            "busi_param": "{}",
            "qzreferrer": referer,
        }
        if commentid is not None:
            form["commentId"] = str(commentid)
        if comment_uin is not None:
            form["commentUin"] = str(comment_uin)
        return form

    async def add_comment(self, hostuin: int, fid: str, content: str, private: bool = False) -> dict:
        hostuin = int(hostuin)
        referer = f"https://user.qzone.qq.com/{hostuin}/mood/{fid}"
        form = self._comment_form(hostuin, fid, content, private=int(bool(private)))
        return await self._post_comment_form(form, referer, hostuin)

    async def reply_comment(
        self,
        hostuin: int,
        fid: str,
        commentid: str,
        comment_uin: int,
        content: str,
    ) -> dict:
        """楼中楼回复。三个端点依次试，全挂才报错，报错里带每个端点的返回码。

        为什么这么写：re_feeds 带 commentId/commentUin 时服务端确实按楼中楼处理
        （假 fid 会把 t2_uin/t2_tid 原样回显），但它在某些账号上会稳定回 -10049；
        h5 域名是同一 CGI 的另一入口，addreply_ugc 是 UGC 回复接口，都能兜底。
        """
        hostuin = int(hostuin)
        referer = f"https://user.qzone.qq.com/{hostuin}/mood/{fid}"
        form = self._comment_form(
            hostuin, fid, content, commentid=commentid, comment_uin=comment_uin
        )
        token = await self.get_qzonetoken(hostuin)
        if token:
            form["qzonetoken"] = token
        errors: list[str] = []
        for url in REPLY_URLS:
            try:
                return await self._request("POST", url, data=form, referer=referer, max_retries=1)
            except QzoneAuthError:
                raise
            except QzoneError as exc:
                host = "h5" if "h5.qzone" in url else ("addreply_ugc" if "addreply" in url else "主站")
                errors.append(f"[{host}] {exc}")
        hint = ""
        if any("-10049" in e for e in errors):
            hint = "（-10049 的排查顺序：先确认 comment_uin 是不是被回复评论的真实作者，再看对方说说有没有开评论权限/审核，最后考虑这个号被风控）"
        raise QzoneError("楼中楼回复失败：" + "；".join(errors) + hint)

    # ---------- 探活 / 自检 ----------

    async def probe_raw(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        referer: str = "",
        timeout: float = 12.0,
        keep_text: bool = False,
    ) -> dict:
        """发一次请求，**不抛业务异常**，把 HTTP 状态和返回码原样交回去。

        给「接口自检」用：诊断需要区分「鉴权没过」「参数不对」「端点被挡」，
        这些差别全在返回码里，走 _request 会被统一抛成异常，反而看不出细节。
        """
        merged = dict(params or {})
        merged.setdefault("g_tk", self.gtk)
        headers = {
            "User-Agent": self.UA,
            "Cookie": self.cookie_text,
            "Referer": referer or f"https://user.qzone.qq.com/{self.uin}",
            "Origin": "https://user.qzone.qq.com",
            "X-Requested-With": "XMLHttpRequest",
        }
        session = await self._ensure_session()
        started = time.perf_counter()
        out = {
            "http": 0,
            "code": None,
            "msg": "",
            "subcode": None,
            "need_verify": 0,
            "location": "",
            "chars": 0,
            "error": "",
            "ms": 0,
            "text": "",
        }
        try:
            async with session.request(
                method, url, params=merged, data=data, headers=headers,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                out["http"] = resp.status
                if resp.status in _REDIRECT_STATUSES:
                    out["location"] = resp.headers.get("Location", "")[:160]
                    text = ""
                else:
                    text = await resp.text(errors="ignore")
                out["chars"] = len(text)
                if keep_text:
                    out["text"] = text
            if text:
                try:
                    payload = parse_payload(text)
                except QzoneError:
                    payload = {}
                if isinstance(payload, dict):
                    out["code"] = _payload_code(payload)
                    out["msg"] = str(payload.get("msg") or payload.get("message") or "")
                    subcode = payload.get("subcode")
                    if subcode not in (None, "", 0):
                        out["subcode"] = subcode
                    try:
                        out["need_verify"] = int(payload.get("needVerify") or 0)
                    except (TypeError, ValueError):
                        out["need_verify"] = 0
                    result = payload.get("result")
                    if out["code"] in (None, 0) and isinstance(result, dict):
                        out["code"] = _payload_code(result)
                        out["msg"] = out["msg"] or str(result.get("message") or result.get("msg") or "")
                elif isinstance(payload, list):
                    out["code"] = 0
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"
        out["ms"] = int((time.perf_counter() - started) * 1000)
        return out

    async def check_alive(self) -> tuple[bool | None, str]:
        """轻量校验登录态：真发一次「读一条说说」。

        返回 (True 有效 / False 失效 / None 说不准, 说明)。
        None 是留给网络故障的——那种情况不该触发重新取 Cookie，取了也一样连不上。
        """
        if not self.cookies:
            return False, "尚未绑定 Cookie"
        if not self.uin or not self.gtk:
            return False, "Cookie 缺 uin 或 p_skey/skey"
        try:
            await self.list_moods(num=1)
            return True, "登录态有效"
        except QzoneAuthError as exc:
            return False, f"登录态失效：{exc}"
        except QzoneError as exc:
            msg = str(exc)
            if "网络" in msg or "超时" in msg or "timeout" in msg.lower():
                return None, msg
            return None, msg
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _write_verdict(result: dict, success_ok: bool = False) -> tuple[str, str]:
        """把一次写接口探活结果翻译成 (状态, 人话)。

        ok / warn / fail 三档。空内容探针的「正确结果」往往是参数错误——
        服务端先校验登录态再校验参数，能返回参数错误就说明鉴权这关已经过了。
        success_ok：个别端点（点赞）对不存在的目标直接回 succ，那种 succ 就是通道正常的证据。
        """
        if result.get("error"):
            return "fail", f"请求没发出去：{result['error']}"
        http = int(result.get("http") or 0)
        code = result.get("code")
        msg = str(result.get("msg") or "")
        if http in _REDIRECT_STATUSES:
            loc = str(result.get("location") or "")
            if "login" in loc.lower() or "ptlogin" in loc.lower():
                return "fail", "被踢回登录页（302）——Cookie 已失效，需要重新绑定"
            return "fail", f"接口跳转 {http}：{loc[:80]}"
        if http == 403:
            return "fail", "403 拒绝访问：没有权限或触发风控"
        if http == 429:
            return "warn", "429 请求太频繁，稍后再试"
        if http >= 400:
            return "fail", f"HTTP {http}"
        extra = ""
        if result.get("subcode") not in (None, ""):
            extra += f" subcode={result['subcode']}"
        if result.get("need_verify"):
            extra += f" needVerify={result['need_verify']}"
        if code is None:
            if http == 200 and result.get("chars"):
                return "warn", "端点有响应，但返回的不是标准 JSON（多半是提示页），这次看不出鉴权结果，仅供参考"
            return "warn", "返回正文为空，端点连通但没给出结果"
        if code == 0:
            if success_ok:
                return "ok", "端点连通且鉴权通过（假 ID 不存在，服务端回 succ 空操作，没有产生任何互动）"
            return "warn", "空内容也被接受了（返回成功）——接口行为异常，请人工确认有没有发出去"
        if code == -3000:
            return "fail", f"登录态失效（-3000）{extra}"
        if code == -10049:
            hint = "写权限被拒（-10049）"
            if result.get("need_verify"):
                return "fail", f"{hint}：needVerify=1，账号写操作被风控，先用手机 QQ 人工评一条过验证码"
            return "fail", f"{hint}：多半是 commentUin 不是被回复评论的真实作者，或对方限制了评论{extra}"
        return "ok", f"鉴权已通过（服务端返回 {code}：{msg or '参数/内容不合法'}，说明这条链路是通的）{extra}"

    async def diagnose(self, hostuin: int | None = None) -> list[dict]:
        """逐个接口探活。只读接口真读，写接口发**空内容**探针，绝不产生真实内容。"""
        host = int(hostuin or self.uin or 0)
        rows: list[dict] = []

        def add(key, name, status, detail, ms=0, code=None, note=""):
            rows.append({
                "key": key, "name": name, "status": status, "detail": detail,
                "ms": int(ms or 0), "code": code, "note": note,
            })

        # 1. Cookie 本地解析
        fields = sorted(self.cookies.keys())
        need = [k for k in ("uin", "p_uin", "skey", "p_skey") if k not in self.cookies]
        if not self.cookies:
            add("cookie", "Cookie 解析", "fail", "没有 Cookie")
        elif not self.uin or not self.gtk:
            add("cookie", "Cookie 解析", "fail",
                f"解不出 {'uin' if not self.uin else ''} {'g_tk' if not self.gtk else ''}".strip()
                + "（缺 p_skey/skey 或 uin）", code=None)
        else:
            status = "warn" if need else "ok"
            detail = f"QQ {self.uin} · g_tk={self.gtk} · {len(fields)} 个字段"
            if need:
                detail += f" · 缺 {'/'.join(need)}（缺 p_skey 会算不出 g_tk，H5 接口也可能被挡）"
            add("cookie", "Cookie 解析", status, detail)

        # 2. 说说列表（读）
        started = time.perf_counter()
        try:
            msgs = await self.list_moods(num=1)
            ms = int((time.perf_counter() - started) * 1000)
            add("msglist", "说说列表 emotion_cgi_msglist_v6", "ok",
                f"读取正常，返回 {len(msgs)} 条（0 条也可能只是没有说说）", ms)
        except QzoneAuthError as exc:
            add("msglist", "说说列表", "fail", f"登录态失效：{exc}",
                int((time.perf_counter() - started) * 1000))
        except QzoneError as exc:
            add("msglist", "说说列表", "fail", str(exc),
                int((time.perf_counter() - started) * 1000))
        except Exception as exc:  # noqa: BLE001
            add("msglist", "说说列表", "fail", f"{type(exc).__name__}: {exc}",
                int((time.perf_counter() - started) * 1000))

        # 3. 动态流（点赞数据来源，读）
        feed_params = {
            "uin": self.uin, "scope": 0, "view": 1, "filter": "all", "flag": 1,
            "appid": 311, "daylist": "", "uinlist": "", "gid": "", "aim": 1, "sort": 0,
            "pos": 0, "num": 5, "format": "json", "inCharset": "utf-8",
            "outCharset": "utf-8", "frequency": 1, "refresh": 0, "style": 1, "useutf8": 1,
        }
        res = await self.probe_raw("GET", FEEDS_URL, params=feed_params,
                                   referer=f"https://user.qzone.qq.com/{self.uin}",
                                   keep_text=True)
        if res["error"]:
            add("feeds", "动态流 feeds3_html_more（点赞明细）", "fail", res["error"], res["ms"])
        elif res["http"] != 200:
            add("feeds", "动态流 feeds3_html_more（点赞明细）", "warn",
                f"HTTP {res['http']}，点赞明细会静默不显示（不影响其他功能）", res["ms"])
        else:
            picked = bool(self._parse_feed_likes(res.get("text") or ""))
            add("feeds", "动态流 feeds3_html_more（点赞明细）", "ok" if picked else "warn",
                ("拉到点赞数据" if picked else "连通，但这次没解析出点赞模块（可能动态流里暂时没有带赞的说说）"),
                res["ms"])

        # 4. qzonetoken
        started = time.perf_counter()
        token = await self.get_qzonetoken(host)
        ms = int((time.perf_counter() - started) * 1000)
        if token:
            add("qzonetoken", "评论令牌 qzonetoken", "ok", f"取到 {len(token)} 字符", ms)
        else:
            add("qzonetoken", "评论令牌 qzonetoken", "warn",
                "没取到。腾讯已弱化这条链路，评论会不附这个参数直接发，属正常范围", ms)

        # 5. 评论（写 · 空内容探活）
        form = self._comment_form(host, "0", "")
        token = token or ""
        if token:
            form["qzonetoken"] = token
        res = await self.probe_raw("POST", COMMENT_URL, data=form,
                                   referer=f"https://user.qzone.qq.com/{host}/mood/0")
        status, detail = self._write_verdict(res)
        add("comment", "评论 emotion_cgi_re_feeds", status, detail, res["ms"], res.get("code"),
            "空内容探针，不会发出任何评论")

        # 6. 楼中楼：三个端点各探一次
        rform = self._comment_form(host, "0", "", commentid="0", comment_uin=self.uin)
        if token:
            rform["qzonetoken"] = token
        labels = {
            "主站 re_feeds": REPLY_URLS[0] if REPLY_URLS else "",
            "h5 re_feeds": REPLY_URLS[1] if len(REPLY_URLS) > 1 else "",
            "addreply_ugc": REPLY_URLS[2] if len(REPLY_URLS) > 2 else "",
        }
        for label, url in labels.items():
            if not url:
                continue
            res = await self.probe_raw("POST", url, data=rform,
                                       referer=f"https://user.qzone.qq.com/{host}/mood/0")
            status, detail = self._write_verdict(res)
            add(f"reply:{label}", f"楼中楼·{label}", status, detail, res["ms"], res.get("code"),
                "空内容探针，不会发出任何回复")

        # 7. 点赞（写 · 空探活，假 fid 不可能命中真实说说）
        mock_key = f"https://user.qzone.qq.com/{host}/mood/0"
        like_form = {
            "unikey": mock_key,
            "curkey": mock_key,
            "appid": 311,
            "opuin": str(self.uin),
            "uin": str(self.uin),
            "hostuin": host,
            "fid": "0",
            "from": 1,
            "typeid": 0,
            "abstime": 0,
            "active": 0,
            "fupdate": 1,
            "opr_type": "like",
            "format": "purejson",
            "qzreferrer": mock_key,
        }
        res = await self.probe_raw("POST", LIKE_PROXY_URL, data=like_form,
                                   referer=f"https://user.qzone.qq.com/{host}")
        status, detail = self._write_verdict(res, success_ok=True)
        add("like", "点赞 internal_dolike_app", status, detail, res["ms"], res.get("code"),
            "假 fid 探针，不会赞到任何真实说说")

        # 8. 有副作用、不能探的
        for key, name, why in (
            ("publish", "发说说 emotion_cgi_publish_v6", "会产生真实内容，不做探活（发说说失败时看报错码即可）"),
            ("upload", "图片上传 up.qzone.qq.com", "需要真图，不做探活"),
            ("delete", "删说说 emotion_cgi_delete_v6", "有删数据的风险，不做探活"),
        ):
            add(key, name, "skip", why, 0, None, "跳过")

        self._diagnose_at = time.time()
        return rows

    # ---------- 点赞 ----------

    async def like(self, hostuin: int, fid: str, like: bool = True, abstime: int = 0) -> dict:
        hostuin = int(hostuin)
        key = f"https://user.qzone.qq.com/{hostuin}/mood/{fid}"
        form = {
            "unikey": key,
            "curkey": key,
            "appid": 311,
            "opuin": str(self.uin),
            "uin": str(self.uin),
            "hostuin": hostuin,
            "fid": fid,
            "from": 1,
            "typeid": 0,
            "abstime": int(abstime or 0),
            "active": 0,
            "fupdate": 1,
            "opr_type": "like" if like else "unlike",
            "format": "purejson",
            "qzreferrer": key,
        }
        endpoints = (
            (LIKE_PROXY_URL, LIKE_DIRECT_URL)
            if like
            else (UNLIKE_PROXY_URL, UNLIKE_DIRECT_URL)
        )
        last_exc: Exception | None = None
        for endpoint in endpoints:
            try:
                return await self._request(
                    "POST", endpoint, data=form,
                    referer=key, max_retries=1,
                )
            except QzoneError as exc:
                last_exc = exc
                continue
        raise QzoneError(f"点赞接口不可用: {last_exc}")

    # ---------- 删除 ----------

    async def delete_mood(self, fid: str, created_time: int = 0) -> dict:
        form = {
            "uin": str(self.uin),
            "topicId": f"{self.uin}_{fid}__1",
            "feedsType": 0,
            "feedsFlag": 0,
            "feedsKey": fid,
            "feedsAppid": 311,
            "feedsTime": int(created_time or time.time()),
            "fupdate": 1,
            "ref": "feeds",
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
        }
        return await self._request(
            "POST",
            DELETE_URL,
            data=form,
            referer=f"https://user.qzone.qq.com/{self.uin}/mood/{fid}",
        )
