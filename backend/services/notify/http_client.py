# -*- coding: utf-8 -*-
"""统一 HTTP 客户端（可选注入 HTTP 代理，复用 Gmail 代理 URL）。"""
from __future__ import annotations

from typing import Optional

import httpx


def build_async_client(
    proxy_url: Optional[str] = None,
    timeout: float = 15.0,
    *,
    trust_env: bool = True,
) -> httpx.AsyncClient:
    """构建 httpx.AsyncClient；proxy_url 非空时走 HTTP 代理。

    兼容不同 httpx 版本的 proxy / proxies 参数名。

    trust_env=False 时忽略环境变量代理（HTTP_PROXY/HTTPS_PROXY）：
    访问本机回环服务（如自建微信推送网关）必须显式关掉，
    否则进程若带有代理环境变量，127.0.0.1 会被错误地经代理转发而失败。
    """
    proxy_url = (proxy_url or "").strip() or None
    kwargs = {"timeout": timeout}
    if not trust_env:
        kwargs["trust_env"] = False
    if not proxy_url:
        return httpx.AsyncClient(**kwargs)
    # 优先新版 proxy= 参数
    try:
        return httpx.AsyncClient(proxy=proxy_url, **kwargs)
    except TypeError:
        return httpx.AsyncClient(proxies=proxy_url, **kwargs)
