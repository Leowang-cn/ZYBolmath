from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from scripts.dingtalk_export import DingTalkExportError, _positive_int, validate_xlsx


PASS_THROUGH = (
    "DINGTALK_DOCUMENT_URL", "DINGTALK_EXPORT_TIMEOUT_MS", "DINGTALK_LOGIN_TIMEOUT_MS",
    "DINGTALK_MENU_TEXT", "DINGTALK_EXPORT_TEXT", "DINGTALK_EXCEL_TEXT",
)


def prepare_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(directory, 1000, 1000)
    if directory.stat().st_uid != 1000:
        raise DingTalkExportError("runner_permissions", "导出目录必须归 UID 1000 所有，请管理员配置目录权限")
    if os.geteuid() in (0, 1000):
        directory.chmod(0o700)
    if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
        raise DingTalkExportError("runner_permissions", "宿主机进程无法读写导出目录")


def read_result(work: Path, returncode: int) -> Path:
    try:
        result = json.loads((work / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DingTalkExportError("runner_failed", f"导出容器未返回有效结果，退出码 {returncode}") from error
    if not isinstance(result, dict):
        raise DingTalkExportError("runner_failed", "导出容器结果格式错误")
    if result.get("ok") is not True:
        code = result.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
            code = "export_failed"
        message = re.sub(r"https?://\S+", "[URL]", str(result.get("message", "钉钉导出失败")))[:1500]
        raise DingTalkExportError(code, message)
    if returncode != 0 or result.get("file") != "dingtalk-export.xlsx":
        raise DingTalkExportError("runner_failed", "导出容器退出状态或文件名异常")
    produced = work / "dingtalk-export.xlsx"
    if produced.is_symlink():
        raise DingTalkExportError("download_invalid", "导出文件不能是符号链接")
    validate_xlsx(produced)
    return produced


def export_in_container(destination: Path, callback: Callable[[bytes], None] | None = None) -> Path:
    url = urlparse(os.getenv("DINGTALK_DOCUMENT_URL", "").strip())
    if url.scheme != "https" or not url.hostname:
        raise DingTalkExportError("not_configured", "钉钉文档地址必须是有效的 HTTPS URL")
    profile = Path(os.getenv("BROWSER_PROFILE_PATH", "data/browser-profile")).resolve()
    prepare_directory(profile)
    container_name = f"olmath-export-{uuid.uuid4().hex}"
    image = os.getenv("DINGTALK_RUNNER_IMAGE", "olmath-chromium-runner:1.55.0")
    scripts = Path(__file__).resolve().parent
    timeout = _positive_int("DINGTALK_RUNNER_TIMEOUT_MS", 900_000) / 1000
    with tempfile.TemporaryDirectory(prefix="dingtalk-export-") as directory:
        work = Path(directory)
        prepare_directory(work)
        argv = ["docker", "run", "--rm", "--name", container_name, "--shm-size=1g", "--network", "bridge",
                "--mount", f"type=bind,src={scripts},dst=/opt/export,readonly",
                "--mount", f"type=bind,src={work},dst=/work",
                "--mount", f"type=bind,src={profile},dst=/work/profile"]
        for name in PASS_THROUGH:
            if os.getenv(name):
                argv.extend(["-e", name])
        argv.extend([image, "python3", "/opt/export/container_export.py"])
        process = None
        try:
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(argv, stdout=output, stderr=output)
                deadline = time.monotonic() + timeout
                last_screenshot = None
                while True:
                    try:
                        process.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline:
                            raise DingTalkExportError("export_timeout", "钉钉导出容器执行超时")
                        screenshot = work / "login.png"
                        if callback and screenshot.is_file():
                            payload = screenshot.read_bytes()
                            if payload != last_screenshot:
                                callback(payload)
                                last_screenshot = payload
                output.seek(0, 2)
                output.seek(max(0, output.tell() - 8192))
                diagnostic = output.read().decode("utf-8", errors="replace")
                diagnostic = re.sub(r"https?://\S+", "[URL]", diagnostic)
                diagnostic = re.sub(r"(?i)(cookie|authorization|token|password)[^\n]*", "[REDACTED]", diagnostic)
                if diagnostic:
                    print(f"container_exit={process.returncode}\n{diagnostic}", flush=True)
                produced = read_result(work, process.returncode)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(produced), str(destination))
        except OSError as error:
            raise DingTalkExportError("runner_unavailable", "无法启动容器运行时或访问导出文件，请检查 Docker 和目录权限") from error
        finally:
            if process is not None:
                try:
                    cleanup = subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=30)
                    if cleanup.returncode and process.poll() is None:
                        print(f"container_cleanup_failed name={container_name}", flush=True)
                except (OSError, subprocess.TimeoutExpired):
                    print(f"container_cleanup_failed name={container_name}", flush=True)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
    return destination
