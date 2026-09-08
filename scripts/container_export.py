from __future__ import annotations

import json
import os
from pathlib import Path

from dingtalk_export import DingTalkExportError, export_workbook


def main() -> int:
    work = Path("/work")
    result = {"ok": False, "code": "export_failed", "message": "浏览器导出失败"}
    try:
        os.environ["DINGTALK_EXPORT_BACKEND"] = "local"
        os.environ["BROWSER_PROFILE_PATH"] = "/work/profile"
        os.environ["CHROMIUM_EXECUTABLE_PATH"] = "/usr/bin/chromium"

        def screenshot(payload: bytes) -> None:
            temporary = work / "login.tmp"
            temporary.write_bytes(payload)
            temporary.replace(work / "login.png")

        export_workbook(work / "dingtalk-export.xlsx", screenshot)
        result = {"ok": True, "file": "dingtalk-export.xlsx"}
    except DingTalkExportError as error:
        result = {"ok": False, "code": error.code, "message": str(error)}
    except Exception as error:
        result = {"ok": False, "code": "runner_failed", "message": type(error).__name__}
    temporary = work / "result.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(work / "result.json")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
