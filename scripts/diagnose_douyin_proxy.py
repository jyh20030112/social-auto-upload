#!/usr/bin/env python3
"""Safely compare direct and proxied Douyin creator-page access.

This diagnostic never uploads a file, clicks a publish/login control, persists a
browser storage state, or prints cookies/proxy credentials.  It deliberately
uses the project's proxy provider so the test exercises the same tunnel endpoint
and Playwright configuration as the API service.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit


CREATOR_UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
IP_ECHO_URL = "https://api.ipify.org?format=json"
SENSITIVE_ENV_KEYS = (
    "KDL_SECRET_ID",
    "KDL_SIGNATURE",
    "KDL_SECRET_KEY",
    "KDL_USER_NAME",
    "KDL_USER_PWD",
)


class PageStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    LOGIN_REQUIRED = "login_required"
    VERIFICATION_REQUIRED = "verification_required"
    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"


class Verdict(str, Enum):
    PROXY_FAILED = "proxy_failed"
    NOT_APPLIED = "not_applied"
    UNSTABLE = "unstable"
    CHANGES_IP_BUT_LOGIN_STILL_REQUIRED = "changes_ip_but_login_still_required"
    PROXY_MAY_HELP = "proxy_may_help"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class StorageStateSummary:
    cookie_count: int
    origin_count: int
    douyin_cookie_count: int


@dataclass(frozen=True)
class ModeProbe:
    mode: str
    requested_samples: int
    ip_samples: tuple[str, ...] = ()
    ip_errors: tuple[str, ...] = ()
    page_status: str = PageStatus.UNKNOWN.value
    final_url: str = ""
    page_error: str = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="对比直连与快代理访问抖音创作者上传页的状态（不会上传或发布）"
    )
    parser.add_argument(
        "--storage-state",
        "--account-storage-state",
        dest="storage_state",
        type=Path,
        required=True,
        help="Playwright storage_state JSON 文件路径",
    )
    parser.add_argument(
        "--account",
        help="代理租约使用的账号标识；默认采用 storage_state 文件名",
    )
    parser.add_argument(
        "--user-id",
        default="proxy-diagnostic",
        help="代理租约使用的用户标识（默认: proxy-diagnostic）",
    )
    parser.add_argument(
        "--samples",
        type=positive_int,
        default=3,
        help="每种连接方式检测出口 IP 的次数（默认: 3）",
    )
    parser.add_argument(
        "--interval",
        type=non_negative_float,
        default=1.0,
        help="出口 IP 检测间隔秒数（默认: 1）",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="将完整诊断结果写入权限为 0600 的 JSON 文件",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="显示浏览器窗口；服务器诊断默认使用 headless",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=positive_int,
        default=90,
        help="单次页面导航超时秒数（默认: 90）",
    )
    return parser


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须大于等于 1")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须大于等于 0")
    return parsed


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without replacing process environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def summarize_storage_state(path: Path) -> StorageStateSummary:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"storage_state 文件不存在: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("storage_state 文件不可读或不是有效 JSON") from exc

    if not isinstance(payload, dict):
        raise ValueError("storage_state 顶层必须是 JSON 对象")
    cookies = payload.get("cookies", [])
    origins = payload.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise ValueError("storage_state 的 cookies 和 origins 必须是数组")

    douyin_cookie_count = 0
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise ValueError("storage_state 的 cookies 包含非对象成员")
        domain = str(cookie.get("domain", "")).lower().lstrip(".")
        if domain == "douyin.com" or domain.endswith(".douyin.com"):
            douyin_cookie_count += 1

    return StorageStateSummary(
        cookie_count=len(cookies),
        origin_count=len(origins),
        douyin_cookie_count=douyin_cookie_count,
    )


def mask_ip(value: str) -> str:
    """Mask an IP for terminal output while keeping address-family context."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "<invalid-ip>"
    if address.version == 4:
        octets = value.split(".")
        return f"{octets[0]}.{octets[1]}.*.*"
    exploded = address.exploded.split(":")
    return ":".join(exploded[:3]) + ":*:*:*:*:*"


def sanitize_url(value: str) -> str:
    """Remove query, fragment and possible user info from a URL."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{parsed.hostname}{port}", parsed.path, "", ""))


def redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    """Redact known credentials and common proxy-credential spellings."""
    redacted = value
    candidates = [secret for secret in secrets if secret]
    candidates.extend(os.environ.get(key, "") for key in SENSITIVE_ENV_KEYS)
    for secret in sorted({item for item in candidates if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")

    redacted = re.sub(
        r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@",
        r"\1<redacted>:<redacted>@",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(password|passwd|pwd|signature|secret(?:_id|_key)?|username|user_name)\s*[=:]\s*[^\s,;]+",
        r"\1=<redacted>",
        redacted,
    )
    return redacted[:500]


def safe_error(exc: BaseException) -> str:
    message = redact_text(str(exc)).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def classify_probe(direct: ModeProbe, proxy: ModeProbe) -> Verdict:
    """Classify a direct/proxy comparison without relying on browser objects."""
    proxy_ips = set(proxy.ip_samples)
    direct_ips = set(direct.ip_samples)

    if not proxy.ip_samples or proxy.page_status == PageStatus.UNREACHABLE.value:
        return Verdict.PROXY_FAILED
    if (
        len(proxy.ip_samples) != proxy.requested_samples
        or proxy.ip_errors
        or len(proxy_ips) != 1
    ):
        return Verdict.UNSTABLE
    if not direct.ip_samples or len(direct.ip_samples) != direct.requested_samples:
        return Verdict.INCONCLUSIVE
    if direct.ip_errors or len(direct_ips) != 1:
        return Verdict.INCONCLUSIVE
    if proxy_ips == direct_ips:
        return Verdict.NOT_APPLIED

    if proxy.page_status in {
        PageStatus.LOGIN_REQUIRED.value,
        PageStatus.VERIFICATION_REQUIRED.value,
    }:
        return Verdict.CHANGES_IP_BUT_LOGIN_STILL_REQUIRED
    if (
        proxy.page_status == PageStatus.AUTHENTICATED.value
        and direct.page_status
        in {PageStatus.LOGIN_REQUIRED.value, PageStatus.VERIFICATION_REQUIRED.value}
    ):
        return Verdict.PROXY_MAY_HELP
    return Verdict.INCONCLUSIVE


def _parse_ip_payload(text: str) -> str:
    try:
        payload = json.loads(text)
        raw_ip = payload.get("ip", "") if isinstance(payload, dict) else ""
        address = ipaddress.ip_address(str(raw_ip).strip())
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("IP 回显服务返回了无法识别的结果") from exc
    return str(address)


async def _is_visible(locator: Any) -> bool:
    try:
        return bool(await locator.count()) and bool(await locator.is_visible())
    except Exception:
        return False


async def _is_attached(locator: Any) -> bool:
    try:
        return bool(await locator.count())
    except Exception:
        return False


async def detect_page_status(page: Any) -> PageStatus:
    # Captcha/slider challenges take precedence over a login card which may be
    # visible underneath the challenge overlay.
    captcha_markers = (
        page.locator('iframe[src*="captcha" i]').first,
        page.locator('iframe[src*="verify" i]').first,
        page.locator('[class*="captcha" i], [id*="captcha" i]').first,
        page.get_by_text("请完成验证", exact=False).first,
        page.get_by_text("拖动滑块", exact=False).first,
    )
    if any([await _is_visible(marker) for marker in captcha_markers]):
        return PageStatus.VERIFICATION_REQUIRED

    login_markers = (
        page.get_by_text("扫码登录", exact=True).first,
        page.get_by_text("手机号登录", exact=True).first,
        page.get_by_text("二维码失效", exact=True).first,
    )
    if any([await _is_visible(marker) for marker in login_markers]):
        return PageStatus.LOGIN_REQUIRED

    verification_markers = (
        page.locator('input[placeholder*="验证码"]').first,
        page.locator('input[placeholder*="短信"]').first,
        page.get_by_text("安全验证", exact=True).first,
        page.get_by_text("完成验证", exact=False).first,
    )
    if any([await _is_visible(marker) for marker in verification_markers]):
        return PageStatus.VERIFICATION_REQUIRED

    upload_input = page.locator(
        'input[type="file"], div[class^="container"] input'
    ).first
    if (
        "creator.douyin.com/creator-micro/content/upload" in page.url
        and await _is_attached(upload_input)
    ):
        return PageStatus.AUTHENTICATED
    return PageStatus.UNKNOWN


async def _probe_ip(page: Any, timeout_ms: int) -> str:
    await page.goto(IP_ECHO_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    body_text = await page.locator("body").inner_text(timeout=timeout_ms)
    return _parse_ip_payload(body_text[:500])


async def run_browser_probe(
    *,
    mode: str,
    storage_state: Path,
    samples: int,
    interval: float,
    timeout_seconds: int,
    headless: bool,
    proxy: dict[str, str] | None,
) -> ModeProbe:
    """Run read-only browser checks using a single browser and fixed proxy."""
    # Delayed imports keep classifier/redaction tests independent from Playwright.
    from patchright.async_api import async_playwright

    ip_samples: list[str] = []
    ip_errors: list[str] = []
    page_status = PageStatus.UNKNOWN
    final_url = ""
    page_error = ""
    browser = None
    context = None
    timeout_ms = timeout_seconds * 1000
    try:
        async with async_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "channel": "chromium",
                "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            }
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(storage_state=str(storage_state))

            ip_page = await context.new_page()
            try:
                for index in range(samples):
                    try:
                        ip_samples.append(await _probe_ip(ip_page, timeout_ms))
                    except Exception as exc:
                        ip_errors.append(safe_error(exc))
                    if index + 1 < samples and interval:
                        await asyncio.sleep(interval)
            finally:
                await ip_page.close()

            creator_page = await context.new_page()
            try:
                await creator_page.goto(
                    CREATOR_UPLOAD_URL,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                final_url = sanitize_url(creator_page.url)
                # TPS 隧道带宽低于直连时，创作者中心的前端资源可能在
                # domcontentloaded 之后数秒才完成挂载。轮询已知状态，避免把
                # 单纯的页面水合延迟误报为 unknown；诊断仍不会点击或上传。
                status_deadline = asyncio.get_running_loop().time() + min(
                    timeout_seconds,
                    15,
                )
                while True:
                    page_status = await detect_page_status(creator_page)
                    if page_status is not PageStatus.UNKNOWN:
                        break
                    if asyncio.get_running_loop().time() >= status_deadline:
                        break
                    await creator_page.wait_for_timeout(500)
            except Exception as exc:
                page_status = PageStatus.UNREACHABLE
                page_error = safe_error(exc)
                final_url = sanitize_url(getattr(creator_page, "url", ""))
            finally:
                await creator_page.close()
    except Exception as exc:
        if not page_error:
            page_error = safe_error(exc)
        page_status = PageStatus.UNREACHABLE
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

    return ModeProbe(
        mode=mode,
        requested_samples=samples,
        ip_samples=tuple(ip_samples),
        ip_errors=tuple(ip_errors),
        page_status=page_status.value,
        final_url=final_url,
        page_error=page_error,
    )


async def acquire_proxy_endpoint(
    user_id: str,
    account: str,
):
    """Acquire through the production provider, while overriding its rollout gate."""
    # Executing a file under scripts/ puts scripts/ (not the repository root) on
    # sys.path.  Add only the known repository root before delayed project imports.
    repository_root = Path(__file__).resolve().parents[1]
    root_text = str(repository_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    from app.src.config import Settings
    from app.src.services.douyin_proxy import DouyinProxyManager

    settings = Settings.from_env().with_overrides(douyin_proxy_enabled=True)
    manager = DouyinProxyManager.from_settings(settings)
    try:
        endpoint = await manager.acquire(user_id, account)
    except Exception:
        await manager.aclose()
        raise
    return manager, endpoint


def build_report(
    *,
    summary: StorageStateSummary,
    direct: ModeProbe,
    proxy: ModeProbe,
    tunnel_endpoint_acquired: bool,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": classify_probe(direct, proxy).value,
        "storage_state": asdict(summary),
        "direct": asdict(direct),
        "proxy": asdict(proxy),
        "proxy_tunnel": {
            "provider": "kuaidaili_tps",
            "endpoint_acquired": tunnel_endpoint_acquired,
        },
    }


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON with mode 0600 and refuse to follow an existing symlink."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_obj:
            descriptor = -1
            file_obj.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def print_terminal_report(report: dict[str, Any], json_output: Path | None) -> None:
    direct = report["direct"]
    proxy = report["proxy"]
    direct_ips = [mask_ip(item) for item in direct["ip_samples"]]
    proxy_ips = [mask_ip(item) for item in proxy["ip_samples"]]
    print(f"结论: {report['verdict']}")
    print(f"直连出口: {direct_ips or ['<unavailable>']}")
    print(f"代理出口: {proxy_ips or ['<unavailable>']}")
    print(f"直连上传页: {direct['page_status']}")
    print(f"代理上传页: {proxy['page_status']}")
    print(
        "TPS 隧道端点: "
        + (
            "acquired"
            if report["proxy_tunnel"]["endpoint_acquired"]
            else "unavailable"
        )
    )
    if direct["ip_errors"] or direct["page_error"]:
        print("直连探测存在错误（详细脱敏信息见 JSON）")
    if proxy["ip_errors"] or proxy["page_error"]:
        print("代理探测存在错误（详细脱敏信息见 JSON）")
    if json_output:
        print(f"JSON 结果已写入: {json_output} (0600)")


def exit_code_for_verdict(verdict: str) -> int:
    return 0 if verdict == Verdict.PROXY_MAY_HELP.value else 2


async def run(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(Path.cwd() / ".env")
    summary = summarize_storage_state(args.storage_state)
    account = args.account or args.storage_state.stem

    direct = await run_browser_probe(
        mode="direct",
        storage_state=args.storage_state,
        samples=args.samples,
        interval=args.interval,
        timeout_seconds=args.timeout_seconds,
        headless=not args.headful,
        proxy=None,
    )

    manager = None
    tunnel_endpoint_acquired = False
    try:
        manager, endpoint = await acquire_proxy_endpoint(args.user_id, account)
        proxy_config = endpoint.playwright_proxy()
        tunnel_endpoint_acquired = True
        proxy = await run_browser_probe(
            mode="proxy",
            storage_state=args.storage_state,
            samples=args.samples,
            interval=args.interval,
            timeout_seconds=args.timeout_seconds,
            headless=not args.headful,
            proxy=proxy_config,
        )
    except Exception as exc:
        proxy = ModeProbe(
            mode="proxy",
            requested_samples=args.samples,
            ip_errors=(safe_error(exc),),
            page_status=PageStatus.UNREACHABLE.value,
            page_error=safe_error(exc),
        )
    finally:
        if manager is not None:
            await manager.aclose()

    return build_report(
        summary=summary,
        direct=direct,
        proxy=proxy,
        tunnel_endpoint_acquired=tunnel_endpoint_acquired,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run(args))
        if args.json_output:
            write_private_json(args.json_output, report)
        print_terminal_report(report, args.json_output)
    except (OSError, ValueError) as exc:
        parser.error(redact_text(str(exc)))
    return exit_code_for_verdict(report["verdict"])


if __name__ == "__main__":
    raise SystemExit(main())
