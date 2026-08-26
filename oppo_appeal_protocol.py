#!/usr/bin/env python3
"""
HeyTap/OPPO appeal query protocol helper.

This implements the encrypted application protocol observed in the HAR:

1. Build the query payload.
2. Add the web signing fields used by the frontend.
3. AES-ECB/PKCS7 encrypt the JSON body with a random 16 byte key.
4. RSA/PKCS#1 v1.5 encrypt that AES key and send it as X-Key.
5. Decrypt the encrypted JSON response with the same AES key.

滑块验证已集成自动识别。当服务端要求验证码时，
captcha_solver 模块会自动完成顶象滑块验证，
无需人工干预。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import random
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


BASE_URL = "https://safe.heytap.com"
QUERY_BIND_ACCOUNT_PATH = "/appeal/v1/pre/query-bind-account"
COUNTRY_CALLING_CODES_PATH = "/web/appeal/country/v1/country-calling-codes"

DEFAULT_BIZ_APP_KEY = "TZeSXfQXxrCyjhvARaVrmw"
DEFAULT_PAYLOAD_APP_KEY = "UkjPCtxsnb8yThUxJz54Lm"
DEFAULT_SIGN_KEY = "&key=4WSQJF3X+zEUl+x1PfeR/g=="
DEFAULT_RSA_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCf5viGpYn1duRt9wzwca1SEuL+wwnBfBfza0nTuLPYR5uZyheUoFI+cud"
    "N9eB4jlvXij4yAxH59ML8BhVUab/j+TmeDsCe+OLpswdHWEXtY1HacLpw/wpsKQHBQZYhAARZRx/4J5/fiz/pJcH5qVGYK0"
    "Yu8c9CNl9/eHDQkj9LoQIDAQAB"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class ProtocolError(RuntimeError):
    pass


def require_crypto() -> Any:
    try:
        from Crypto.Cipher import AES, PKCS1_v1_5  # type: ignore
        from Crypto.PublicKey import RSA  # type: ignore
    except ModuleNotFoundError as exc:
        raise ProtocolError(
            "缺少加密依赖 pycryptodome。先运行：python3 -m pip install -r requirements.txt"
        ) from exc
    return AES, PKCS1_v1_5, RSA


def json_dumps(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def now_ms() -> int:
    return int(time.time() * 1000)


def random_aes_key(length: int = 16) -> str:
    alphabet = string.ascii_letters + "123456789"
    return "".join(random.choice(alphabet) for _ in range(length))


def random_hex(length: int) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(length))


def random_digits(length: int) -> str:
    return "".join(random.choice(string.digits) for _ in range(length))


def pkcs7_pad(raw: bytes, block_size: int = 16) -> bytes:
    size = block_size - (len(raw) % block_size)
    return raw + bytes([size]) * size


def pkcs7_unpad(raw: bytes, block_size: int = 16) -> bytes:
    if not raw or len(raw) % block_size:
        raise ProtocolError("AES 解密结果长度异常")
    size = raw[-1]
    if size < 1 or size > block_size or raw[-size:] != bytes([size]) * size:
        raise ProtocolError("AES PKCS7 padding 校验失败")
    return raw[:-size]


def aes_encrypt_base64(plain_text: str, aes_key: str) -> str:
    AES, _, _ = require_crypto()
    cipher = AES.new(aes_key.encode("utf-8"), AES.MODE_ECB)
    encrypted = cipher.encrypt(pkcs7_pad(plain_text.encode("utf-8")))
    return base64.b64encode(encrypted).decode("ascii")


def aes_decrypt_base64(cipher_text: str, aes_key: str) -> str:
    AES, _, _ = require_crypto()
    try:
        encrypted = base64.b64decode(cipher_text, validate=True)
    except Exception as exc:
        raise ProtocolError(f"响应不是合法 Base64 密文：{cipher_text[:300]!r}") from exc
    if not encrypted or len(encrypted) % 16:
        raise ProtocolError(
            "AES 解密结果长度异常；响应可能不是该协议的加密 JSON。"
            f" base64解码后长度={len(encrypted)}，响应预览={cipher_text[:300]!r}"
        )
    cipher = AES.new(aes_key.encode("utf-8"), AES.MODE_ECB)
    decrypted = cipher.decrypt(encrypted)
    return pkcs7_unpad(decrypted).decode("utf-8")


def rsa_encrypt_aes_key(aes_key: str, public_key_b64: str = DEFAULT_RSA_PUBLIC_KEY) -> str:
    _, PKCS1_v1_5, RSA = require_crypto()
    pem = "-----BEGIN PUBLIC KEY-----\n{}\n-----END PUBLIC KEY-----".format(public_key_b64)
    key = RSA.import_key(pem)
    encrypted = PKCS1_v1_5.new(key).encrypt(aes_key.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def stringify_for_sign(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    if isinstance(value, dict):
        return json_dumps(value)
    return str(value)


def sign_payload(
    data: Mapping[str, Any],
    *,
    fallback_bizk: str = DEFAULT_PAYLOAD_APP_KEY,
    bizk_key_name: str = "appKey",
    sign_key: str = DEFAULT_SIGN_KEY,
    with_nonce: bool = False,
    exclude_sign_fields: Iterable[str] = (),
) -> Dict[str, Any]:
    timestamp = now_ms()
    app_key = data.get(bizk_key_name) or data.get("bizk") or fallback_bizk
    payload: Dict[str, Any] = {
        "sign": "",
        "timestamp": timestamp,
        bizk_key_name: str(app_key),
    }
    if with_nonce:
        payload["nonce"] = str(timestamp)
    payload.update(data)

    exclude = set(exclude_sign_fields)
    parts = []
    for key in payload:
        value = payload[key]
        if key in exclude or value is None:
            continue
        text = stringify_for_sign(value)
        if text:
            parts.append(f"{key}={text}")
    parts.sort(key=lambda item: item.upper())
    payload["sign"] = hashlib.md5(("&".join(parts) + sign_key).encode("utf-8")).hexdigest()

    return {key: value for key, value in payload.items() if value is not None}


@dataclass
class HeyTapEncryptedSession:
    aes_key: str = field(default_factory=random_aes_key)
    device_id: str = field(default_factory=lambda: hashlib.md5(str(random.random()).encode()).hexdigest())
    biz_app_key: str = DEFAULT_BIZ_APP_KEY
    payload_app_key: str = DEFAULT_PAYLOAD_APP_KEY
    public_key_b64: str = DEFAULT_RSA_PUBLIC_KEY
    timezone: str = "GMT+8"
    session_ticket: str = ""
    observation_session_id: str = field(
        default_factory=lambda: f"{hashlib.md5(str(random.random()).encode()).hexdigest()}-{now_ms()}-accountappeal"
    )

    def __post_init__(self) -> None:
        self.refresh_encryption(clear_ticket=False)
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name="cloudObservationRumSessionId",
                value=self.observation_session_id,
                port=None,
                port_specified=False,
                domain="safe.heytap.com",
                domain_specified=True,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def refresh_encryption(self, *, clear_ticket: bool = True) -> None:
        if clear_ticket:
            self.aes_key = random_aes_key()
            self.session_ticket = ""
        self.encrypted_aes_key = rsa_encrypt_aes_key(self.aes_key, self.public_key_b64)

    def init_page_cookie(self) -> None:
        req = urllib.request.Request(
            BASE_URL + "/appeal-system/entry/check",
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            method="GET",
        )
        with self.opener.open(req, timeout=15) as resp:
            resp.read()
        self.post_encrypted(COUNTRY_CALLING_CODES_PATH, {})

    def store_set_cookie_headers(self, headers: Any) -> None:
        cookie_headers = []
        if hasattr(headers, "get_all"):
            cookie_headers = headers.get_all("Set-Cookie") or []
        else:
            value = headers.get("set-cookie") or headers.get("Set-Cookie")
            cookie_headers = [value] if value else []

        jar = None
        for handler in self.opener.handlers:
            if hasattr(handler, "cookiejar"):
                jar = handler.cookiejar
                break
        if jar is None:
            return

        for header in cookie_headers:
            parsed = SimpleCookie()
            parsed.load(header)
            for morsel in parsed.values():
                jar.set_cookie(
                    http.cookiejar.Cookie(
                        version=0,
                        name=morsel.key,
                        value=morsel.value,
                        port=None,
                        port_specified=False,
                        domain="safe.heytap.com",
                        domain_specified=True,
                        domain_initial_dot=False,
                        path=morsel["path"] or "/",
                        path_specified=True,
                        secure=bool(morsel["secure"]),
                        expires=None,
                        discard=True,
                        comment=None,
                        comment_url=None,
                        rest={"HttpOnly": morsel["httponly"]} if morsel["httponly"] else {},
                        rfc2109=False,
                    )
                )

    def build_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/encrypted-json;charset=UTF-8",
            "Accept-Language": "zh-CN",
            "Bizappkey": self.biz_app_key,
            "Content-Type": "application/encrypted-json;charset=UTF-8",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/appeal-system/entry/check",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-CH-UA": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"macOS"',
            "Traceparent": f"00-{random_hex(32)}-{random_hex(16)}-00",
            "User-Agent": USER_AGENT,
            "X-BusinessSystem": "HeyTap",
            "X-Device-Id": self.device_id,
            "X-From-HT": "true",
            "X-Key": self.encrypted_aes_key,
            "X-Protocol-Version": "1.0",
            "X-Timezone": self.timezone,
        }
        if self.session_ticket:
            headers["X-Session-Ticket"] = self.session_ticket
        return headers

    def post_encrypted(self, path: str, data: Mapping[str, Any], *, retry_times: int = 3) -> Dict[str, Any]:
        status = 0
        content_type = ""
        raw = ""
        last_error: Optional[Exception] = None

        for attempt in range(retry_times + 1):
            payload = dict(data)
            payload.setdefault("nonce", random_digits(9))
            signed = sign_payload(payload, fallback_bizk=self.payload_app_key, bizk_key_name="appKey")
            encrypted_body = aes_encrypt_base64(json_dumps(signed), self.aes_key).encode("utf-8")
            req = urllib.request.Request(
                BASE_URL + path,
                data=encrypted_body,
                headers=self.build_headers(),
                method="POST",
            )
            try:
                with self.opener.open(req, timeout=20) as resp:
                    status = resp.status
                    content_type = resp.headers.get("content-type", "")
                    raw = resp.read().decode("utf-8")
                    ticket = resp.headers.get("x-session-ticket")
                    if ticket:
                        self.session_ticket = ticket
            except urllib.error.HTTPError as exc:
                status = exc.code
                content_type = exc.headers.get("content-type", "")
                self.store_set_cookie_headers(exc.headers)
                raw = exc.read().decode("utf-8", errors="replace")

            if status in (222, 400) and attempt < retry_times:
                self.refresh_encryption(clear_ticket=True)
                continue
            if status == 222:
                raise ProtocolError(
                    "服务端返回 HTTP 222（前端枚举为 PublicKeyInvalid）。"
                    "这表示当前 X-Key/RSA 握手没有被网关接受，通常是公钥版本、加密实现或环境校验不匹配。"
                )
            if status >= 400:
                raise ProtocolError(f"HTTP {status}: {raw[:500]}")

            try:
                plain = aes_decrypt_base64(raw, self.aes_key)
                return json.loads(plain)
            except (ProtocolError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < retry_times:
                    self.refresh_encryption(clear_ticket=True)
                    continue
                if isinstance(exc, json.JSONDecodeError):
                    raise ProtocolError(f"响应解密成功，但不是 JSON：{plain}") from exc

        raise ProtocolError(
            f"请求重试 {retry_times} 次后仍失败：{last_error} HTTP状态={status} "
            f"Content-Type={content_type!r} X-Session-Ticket={bool(self.session_ticket)}"
        )

    def query_bind_account(
        self,
        account_name: str,
        *,
        account_type: str = "MOBILE",
        country_calling_code: str = "+86",
        source: str = "2",
        captcha_ticket: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "accountType": account_type,
            "accountName": account_name,
            "countryCallingCode": country_calling_code,
            "source": source,
        }
        if captcha_ticket:
            payload["captchaTicket"] = captcha_ticket
        return self.post_encrypted(QUERY_BIND_ACCOUNT_PATH, payload)


def auto_solve_captcha() -> Optional[str]:
    """自动完成滑块验证，返回 captchaTicket（token）。

    使用 cdp_captcha_solver 模块（Playwright 真浏览器 + greenseer SDK）：
    1. Playwright 启动 headless Chromium
    2. 在真浏览器中加载 greenseer SDK
    3. 利用完整浏览器 API 生成有效 ac 参数（不限 x 范围）
    4. 提交验证获取 token
    """
    import sys as _sys
    import asyncio
    try:
        from cdp_captcha_solver import auto_solve_captcha as _cdp_solve
        _loop = asyncio.new_event_loop()
        try:
            token = _loop.run_until_complete(_cdp_solve(max_attempts=3))
        finally:
            _loop.close()
        if token:
            print(f"滑块验证成功 token={token}", file=_sys.stderr)
            return token
        print("滑块自动验证失败：所有尝试均未通过", file=_sys.stderr)
    except ImportError as e:
        print(f"缺少依赖: {e}", file=_sys.stderr)
    except Exception as exc:
        print(f"滑块自动验证出错：{exc}", file=_sys.stderr)
        import traceback
        traceback.print_exc(file=_sys.stderr)
    return None





def query_main() -> None:
    """CLI 入口"""
    import argparse
    parser = argparse.ArgumentParser(
        description="HeyTap/OPPO appeal query protocol helper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    q = sub.add_parser("query", help="查询绑定账号")
    q.add_argument("account", help="手机号 / 账号")
    q.add_argument("--captcha-ticket", help="人工完成滑块后得到的 captchaTicket")
    q.add_argument("--no-interactive", action="store_true",
        help="触发滑块时只打印提示，不在终端交互输入 captchaTicket")

    hs = sub.add_parser("har-summary", help="解析 HAR 文件中的关键请求")
    hs.add_argument("har_file", help="HAR 文件路径")

    args = parser.parse_args()

    if args.command == "query":
        session = HeyTapEncryptedSession()
        result = session.query_bind_account(
            args.account, captcha_ticket=args.captcha_ticket,
        )

        if result.get("success") and result.get("data"):
            print(json.dumps(result["data"], indent=2, ensure_ascii=False))
            return

        error = result.get("error") or result
        code = error.get("code", "")
        msg = error.get("message", error.get("msg", ""))

        if code in ("CAPTCHA_NEEDED", "APL00201"):
            if args.captcha_ticket:
                print(f"服务端仍要求验证码：{msg}", file=sys.stderr)
                sys.exit(1)

            ticket = auto_solve_captcha()
            if ticket:
                result2 = session.query_bind_account(
                    args.account, captcha_ticket=ticket,
                )
                if result2.get("success") and result2.get("data"):
                    print(json.dumps(result2["data"], indent=2, ensure_ascii=False))
                    return
                print(json.dumps(result2, indent=2, ensure_ascii=False))
                sys.exit(1)

            if not args.no_interactive and sys.stdin.isatty():
                ticket = input("\n服务端要求验证码，请粘贴人工完成验证后得到的 captchaTicket: ").strip()
                if ticket:
                    result3 = session.query_bind_account(
                        args.account, captcha_ticket=ticket,
                    )
                    print(json.dumps(result3, indent=2, ensure_ascii=False))
                    return

            print("服务端要求验证码。自动验证失败，请手动获取 captchaTicket 后重试。", file=sys.stderr)
            sys.exit(1)

        print(f"查询失败: code={code} msg={msg}", file=sys.stderr)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(1)

    elif args.command == "har-summary":
        with open(args.har_file) as f:
            har = json.load(f)
        entries = har.get("log", {}).get("entries", [])
        print(f"共 {len(entries)} 个请求")
        from collections import Counter
        hosts = Counter()
        for entry in entries:
            req = entry.get("request", {})
            url = req.get("url", "")
            from urllib.parse import urlparse
            hosts[urlparse(url).netloc] += 1
        print("\n请求目标统计:")
        for host, count in hosts.most_common(10):
            print(f"  {host}: {count}")


if __name__ == "__main__":
    query_main()
