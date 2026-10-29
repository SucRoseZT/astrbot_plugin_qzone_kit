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

import aiohttp

IMAGE_UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
MSGLIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
H5_INDEX_URL = "https://h5.qzone.qq.com/mqzone/index"
H5_PROFILE_URL = "https://h5.qzone.qq.com/mqzone/profile"
_SHINE_TOKEN_RE = re.compile(r'window\.shine0callback.*?return\s*"([0-9a-f]+?)"\s*;', re.S)
DELETE_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
LIKE_PROXY_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
UNLIKE_PROXY_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_unlike_app"
LIKE_DIRECT_URL = "https://w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
UNLIKE_DIRECT_URL = "https://w.qzone.qq.com/cgi-bin/likes/internal_unlike_app"

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
    raise QzoneError(f"QQ 空间接口错误 {code}: {msg or payload}")


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

    def __init__(self, cookie_str: str, timeout: float = 20.0):
        self.cookies = parse_cookie_str(cookie_str)
        self.uin = uin_from_cookies(self.cookies)
        self.gtk = gtk_from_skey(
            self.cookies.get("p_skey") or self.cookies.get("skey") or ""
        )
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._qzonetokens: dict[str, str] = {}

    @property
    def cookie_text(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

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
                            raise QzoneAuthError("登录态已失效，请重新绑定 Cookie")
                        raise QzoneError(f"接口跳转异常 {resp.status}: {location[:120]}")
                    if resp.status == 403:
                        raise QzoneError("QQ 空间拒绝访问(403)，可能没有权限或触发风控")
                    if resp.status == 429:
                        raise QzoneError("请求太频繁(429)，请稍后再试")
                    if resp.status >= 400:
                        raise QzoneError(f"接口 HTTP {resp.status}")
                    text = await resp.text()
                payload = _check_payload(parse_payload(text))
                return _check_payload(_unwrap(payload))
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

    # ---------- 评论 / 回复 ----------

    async def get_qzonetoken(self, hostuin: int) -> str:
        """评论接口需要 qzonetoken，藏在 H5 页面的 shine0callback 脚本里，按 hostuin 缓存。"""
        key = str(int(hostuin or self.uin))
        cached = self._qzonetokens.get(key)
        if cached:
            return cached
        session = await self._ensure_session()
        if int(hostuin or 0) == self.uin:
            url, referer, params = H5_INDEX_URL, f"https://user.qzone.qq.com/{self.uin}", None
        else:
            url, referer = H5_PROFILE_URL, f"https://user.qzone.qq.com/{hostuin}"
            params = {"hostuin": int(hostuin), "starttime": int(time.time() * 1000)}
        try:
            async with session.get(
                url, params=params,
                headers={"User-Agent": self.UA, "Referer": referer},
            ) as resp:
                html = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return ""
        match = _SHINE_TOKEN_RE.search(html)
        if not match:
            return ""
        token = match.group(1)
        self._qzonetokens[key] = token
        return token

    async def add_comment(self, hostuin: int, fid: str, content: str, private: bool = False) -> dict:
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
            "private": int(bool(private)),
            "paramstr": "1",
            "isSignIn": "0",
            "richval": "",
            "richtype": "",
            "appid": 311,
            "busi_param": "{}",
            "qzreferrer": referer,
        }
        token = await self.get_qzonetoken(hostuin)
        if token:
            form["qzonetoken"] = token
        return await self._request("POST", COMMENT_URL, data=form, referer=referer)

    async def reply_comment(
        self,
        hostuin: int,
        fid: str,
        commentid: str,
        comment_uin: int,
        content: str,
    ) -> dict:
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
            "commentId": str(commentid),
            "commentUin": str(comment_uin),
            "private": 0,
            "paramstr": "1",
            "isSignIn": "0",
            "appid": 311,
            "richval": "",
            "richtype": "",
            "qzreferrer": referer,
        }
        token = await self.get_qzonetoken(hostuin)
        if token:
            form["qzonetoken"] = token
        return await self._request("POST", COMMENT_URL, data=form, referer=referer)

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
