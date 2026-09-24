# -*- coding: utf-8 -*-
"""微信推送渠道（对接自建 HTTP 推送服务）。

适配形如 wechatbot / wechaty 网关这类「一条 HTTP 就发微信」的自建服务：

    POST {server}/v1/wechat
    Authorization: Bearer <TOKEN>
    Content-Type: application/json
    {"text": "..."}
    -> {"ok":true,"chars":7,"truncated":false,"ms":1830}

服务端只暴露 text 字段，因此本渠道**只发文字**：
- 文字模式：发送「飞邮 + 主题/元信息/正文预览」
- 图片模式：服务端没有图片通道，自动按同一份文字发送（宁可降级，也不丢通知）

地址与 Token 在「设置 → 通知设置 → 微信」中填写，默认 http://127.0.0.1:9901
（即飞牛本机回环地址，服务与飞邮同机时无需改动）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

from utils.logger import get_logger
from services.notify.channels.base import NotifyChannel
from services.notify.http_client import build_async_client
from services.notify.types import ChannelMessage

logger = get_logger("notify.wechat")

# 默认服务地址（飞牛本机回环）与固定接口路径
DEFAULT_SERVER = "http://127.0.0.1:9901"
PUSH_PATH = "/v1/wechat"

# 服务端内部要走浏览器自动化转发，耗时可能到秒级，超时给足
REQUEST_TIMEOUT = 30.0

# 服务端文本长度上限未知（超长会自行截断并回传 truncated），本地先做保守兜底
TEXT_MAX_CHARS = 2000


def normalize_wechat_config(config: Dict[str, Any]) -> Tuple[str, str]:
    """规范化服务地址与 Token，返回 (server, token)。

    兼容用户直接粘贴各种形态的地址：
    - ``127.0.0.1:9901``                        → 自动补 http://
    - ``http://127.0.0.1:9901/``                → 去掉尾部斜杠
    - ``http://127.0.0.1:9901/v1/wechat``       → 去掉接口后缀，只留服务根
    - ``http://127.0.0.1:9901/v1/wechat?token=xxx`` → 顺带提取 token
    """
    raw_server = str((config or {}).get("server") or DEFAULT_SERVER).strip()
    raw_token = str((config or {}).get("token") or "").strip()

    token_from_url = ""
    if raw_server:
        candidate = raw_server
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = "http://" + candidate
        try:
            parsed = urlparse(candidate)
        except Exception:
            parsed = None
        if parsed is not None and parsed.scheme in ("http", "https") and parsed.netloc:
            path = (parsed.path or "").rstrip("/")
            lower = path.lower()
            for suffix in ("/v1/wechat", "/v1"):
                if lower.endswith(suffix):
                    path = path[: -len(suffix)]
                    break
            raw_server = f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")
            if parsed.query:
                token_from_url = str((parse_qs(parsed.query).get("token") or [""])[0]).strip()

    return raw_server, (raw_token or token_from_url)


def _is_valid_server(server: str) -> bool:
    """服务地址是否为合法 http(s) URL（必须能解析出主机名）。"""
    try:
        p = urlparse(server or "")
        if p.scheme not in ("http", "https"):
            return False
        host = p.hostname or ""
        return bool(host) and " " not in host
    except Exception:
        return False


def _compose_text(message: ChannelMessage) -> str:
    """拼装纯文本：标题（飞邮）+ 正文。"""
    title = str(message.title or "").strip()
    body = str(message.body or "").strip()
    if title and body:
        text = f"{title}\n{body}"
    else:
        text = body or title
    if len(text) > TEXT_MAX_CHARS:
        text = text[:TEXT_MAX_CHARS].rstrip() + "…"
    return text


def _friendly_error(status_code: int, text: str) -> str:
    """把原始 HTTP 错误转成可操作的中文提示。"""
    snippet = (text or "").strip()[:300]
    if status_code == 400:
        return f"微信推送请求被拒绝（HTTP 400）: {snippet}"
    if status_code == 401:
        return "微信推送鉴权失败（HTTP 401）：请检查 Token 是否与服务端配置一致"
    if status_code == 403:
        return "微信推送被拒绝（HTTP 403）：Token 可能没有该接口权限"
    if status_code == 404:
        return "微信推送接口不存在（HTTP 404）：请确认服务地址与接口版本（默认 /v1/wechat）"
    if status_code == 422:
        return f"微信推送参数不被接受（HTTP 422）: {snippet}"
    if status_code >= 500:
        return f"微信推送服务端异常（HTTP {status_code}）: {snippet}"
    return f"微信推送失败（HTTP {status_code}）: {snippet}"


class WechatChannel(NotifyChannel):
    name = "wechat"

    def validate_config(self, config: Dict[str, Any]) -> Optional[str]:
        server, token = normalize_wechat_config(config or {})
        if not _is_valid_server(server):
            return "请填写推送服务地址，例如 http://127.0.0.1:9901"
        if not token:
            return "请填写推送服务的 Token"
        return None

    async def send(
        self,
        message: ChannelMessage,
        config: Dict[str, Any],
        *,
        user_uid: str = "",
    ) -> None:
        """发送文字通知（图片模式同样走文字）。"""
        err = self.validate_config(config)
        if err:
            raise ValueError(err)

        server, token = normalize_wechat_config(config or {})
        url = f"{server}{PUSH_PATH}"

        if (message.mode or "text").strip().lower() == "image":
            logger.info(
                "微信渠道不支持图片，按文字发送 user_uid=%s", user_uid or "-"
            )

        text = _compose_text(message)
        if not text:
            raise RuntimeError("微信推送内容为空，已跳过")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            # trust_env=False：目标是本机/内网网关，禁止被环境变量代理劫持
            async with build_async_client(
                timeout=REQUEST_TIMEOUT, trust_env=False
            ) as client:
                resp = await client.post(url, json={"text": text}, headers=headers)
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"无法连接微信推送服务 {server}：请确认服务已在飞牛上运行、"
                f"地址与端口填写正确（{e.__class__.__name__}）"
            ) from e

        raw = (resp.text or "").strip()
        if resp.status_code >= 400:
            logger.warning(
                "微信推送失败 user_uid=%s server=%s status=%s body=%s",
                user_uid or "-",
                server,
                resp.status_code,
                raw[:300],
            )
            raise RuntimeError(_friendly_error(resp.status_code, raw))

        # 服务端约定 {"ok": true, ...}；HTTP 已 2xx 但业务失败时同样报错
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("ok") is False:
            detail = str(data.get("error") or data.get("message") or data)[:300]
            logger.warning(
                "微信推送被服务端拒绝 user_uid=%s server=%s body=%s",
                user_uid or "-",
                server,
                detail,
            )
            raise RuntimeError(f"微信推送被服务端拒绝: {detail}")

        chars = data.get("chars") if isinstance(data, dict) else None
        logger.info(
            "微信推送成功 user_uid=%s server=%s chars=%s",
            user_uid or "-",
            server,
            chars if chars is not None else len(text),
        )
