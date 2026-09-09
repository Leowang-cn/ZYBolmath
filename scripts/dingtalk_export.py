from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


class DingTalkExportError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def export_workbook(destination: Path, on_login_screenshot: Callable[[bytes], None] | None = None) -> Path:
    backend = os.getenv("DINGTALK_EXPORT_BACKEND", "local")
    if backend == "container":
        from scripts.container_runner import export_in_container
        return export_in_container(destination, on_login_screenshot)
    if backend != "local":
        raise DingTalkExportError("not_configured", "未知的钉钉导出后端")
    document_url = os.getenv("DINGTALK_DOCUMENT_URL", "").strip()
    if not document_url:
        raise DingTalkExportError("not_configured", "服务器尚未配置钉钉文档地址")
    parsed_url = urlparse(document_url)
    if parsed_url.scheme != "https" or not parsed_url.hostname:
        raise DingTalkExportError("not_configured", "钉钉文档地址必须是有效的 HTTPS URL")

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise DingTalkExportError("browser_unavailable", "服务器尚未安装 Playwright 浏览器组件") from error

    profile_path = Path(os.getenv("BROWSER_PROFILE_PATH", "data/browser-profile")).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    timeout_ms = _positive_int("DINGTALK_EXPORT_TIMEOUT_MS", 120_000)
    executable_path = os.getenv("CHROMIUM_EXECUTABLE_PATH") or None

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_path),
                executable_path=executable_path,
                headless=os.getenv("CHROMIUM_HEADLESS", "1") != "0",
                accept_downloads=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(timeout_ms)
                if os.getenv("DINGTALK_FORCE_LOGIN") == "1":
                    page.goto("https://alidocs.dingtalk.com/i/desktop/recent", wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_timeout(2_000)
                    if _login_required(page):
                        _wait_for_login(page, document_url, on_login_screenshot)
                    else:
                        raise DingTalkExportError("login_page_not_found", "未检测到钉钉登录页面，请检查登录入口")
                page.goto(document_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(2_000)
                if _login_required(page):
                    _wait_for_login(page, document_url, on_login_screenshot)
                _check_document_access(page)
                _click_first(page, _patterns("DINGTALK_MENU_TEXT", r"更多|菜单|文件"), "export_controls_not_found")
                page.wait_for_timeout(500)
                _click_first(page, _patterns("DINGTALK_EXPORT_TEXT", r"下载|导出"), "export_controls_not_found")
                page.wait_for_timeout(500)
                export_option = _first_visible(page, _patterns("DINGTALK_EXCEL_TEXT", r"Excel|XLSX|表格"))
                if export_option is None:
                    raise DingTalkExportError("export_controls_not_found", "未找到 Excel 导出选项，可能是页面结构变化或账号权限限制，可尝试重新扫码登录")
                with page.expect_download(timeout=timeout_ms) as download_info:
                    export_option.click()
                download = download_info.value
                download.save_as(str(destination))
            finally:
                context.close()
    except DingTalkExportError:
        raise
    except PlaywrightTimeoutError as error:
        raise DingTalkExportError("export_timeout", "钉钉文档导出超时，请检查登录状态和导出权限") from error
    except Exception as error:
        raise DingTalkExportError("export_failed", f"钉钉文档导出失败：{error}") from error

    validate_xlsx(destination)
    return destination


def validate_xlsx(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise DingTalkExportError("download_invalid", "钉钉没有返回有效的导出文件")
    try:
        with zipfile.ZipFile(path) as workbook:
            names = set(workbook.namelist())
            if "xl/workbook.xml" not in names or not any(name.startswith("xl/worksheets/") for name in names):
                raise DingTalkExportError("download_invalid", "下载文件不是有效的 Excel 工作簿")
    except zipfile.BadZipFile as error:
        raise DingTalkExportError("download_invalid", "下载文件不是有效的 XLSX，可能是权限提示页面") from error


def _check_document_access(page: object) -> None:
    url = str(getattr(page, "url", ""))
    body = page.locator("body").inner_text(timeout=10_000)
    if _login_required(page):
        raise DingTalkExportError("login_required", "钉钉登录已失效，请重新完成扫码登录")
    if re.search(r"无权访问|暂无权限|申请权限|access denied|permission denied", body, re.IGNORECASE):
        raise DingTalkExportError("document_access_denied", "当前钉钉账号无权查看目标文档")
    if re.search(r"禁止下载|禁止导出|不允许下载|不允许导出|无下载权限|无导出权限", body):
        raise DingTalkExportError("export_permission_denied", "钉钉页面提示禁止下载或导出，请使用有权限的账号或联系文档所有者")


def _login_required(page: object) -> bool:
    url = str(getattr(page, "url", ""))
    if re.search(r"(?:login|signin)", url, re.IGNORECASE):
        return True
    for scope in [page, *getattr(page, "frames", [])]:
        login_prompt = _first_visible(scope, [re.compile(r"扫码登录|扫码登陆|手机号登录|密码登录|钉钉扫码", re.IGNORECASE)])
        if login_prompt is not None:
            return True
    return False


def _wait_for_login(
    page: object,
    document_url: str,
    on_login_screenshot: Callable[[bytes], None] | None,
) -> None:
    timeout_ms = _positive_int("DINGTALK_LOGIN_TIMEOUT_MS", 5 * 60_000)
    elapsed_ms = 0
    logged_in_checks = 0
    screenshot_interval_ms = 5_000
    while elapsed_ms < timeout_ms:
        if on_login_screenshot is not None and elapsed_ms % screenshot_interval_ms == 0:
            screenshot = _capture_login_screenshot(page)
            if screenshot is not None:
                on_login_screenshot(screenshot)
        page.wait_for_timeout(1_000)
        elapsed_ms += 1_000
        if not _login_required(page):
            logged_in_checks += 1
            if logged_in_checks >= 3:
                page.goto(document_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(2_000)
                return
        else:
            logged_in_checks = 0
    raise DingTalkExportError("login_timeout", "等待钉钉扫码登录超时，请重新发起更新")


def _capture_login_screenshot(page: object) -> bytes | None:
    selectors = (
        "[class*='qr'] canvas",
        "[class*='qrcode']",
        "img[src*='qr']",
        "canvas",
    )
    for scope in [page, *getattr(page, "frames", [])]:
        for selector in selectors:
            locator = scope.locator(selector)
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate.screenshot(type="png")
    return None


def _patterns(environment_name: str, default: str) -> list[re.Pattern[str]]:
    configured = os.getenv(environment_name, "").strip()
    values = configured.split("|") if configured else default.split("|")
    return [re.compile(re.escape(value.strip()), re.IGNORECASE) for value in values if value.strip()]


def _first_visible(page: object, patterns: list[re.Pattern[str]]) -> object | None:
    for pattern in patterns:
        locator = page.get_by_text(pattern)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                return candidate
    return None


def _click_first(page: object, patterns: list[re.Pattern[str]], error_code: str) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    for pattern in patterns:
        exact_pattern = re.compile(r"^\s*(?:" + pattern.pattern + r")\s*$", pattern.flags)
        locator = page.get_by_text(exact_pattern)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if not candidate.is_visible():
                continue
            try:
                candidate.click(timeout=2_000)
                return
            except PlaywrightTimeoutError:
                continue
    raise DingTalkExportError(error_code, "未找到可点击的下载或导出菜单，可能是页面结构变化或账号权限限制，可尝试重新扫码登录")


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1_000, int(os.getenv(name, str(default))))
    except ValueError:
        return default