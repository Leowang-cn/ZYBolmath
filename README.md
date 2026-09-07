# 奥数大纲题库

将钉钉导出的 XLSX 增量同步到 SQLite 与本地文件或 MinIO，并通过桌面端页面浏览、筛选和维护示例题。

## 数据同步

```bash
/usr/local/python3.12/bin/python3.12 scripts/sync_workbook.py \
  /path/to/3、4年级奥数大纲.xlsx \
  --database ./data/app.sqlite \
  --asset-backend local \
  --asset-dir ./data/assets \
  --package ./release/ZYBolmath-data.tar.gz
```

相同工作簿会按 SHA-256 快速跳过；工作簿变化时只更新变化行。图片按内容哈希去重。`--package` 会在同步后直接生成前端可导入的数据包，并在命令输出中给出文件大小和 SHA-256；即使源文件未变化，也会重新生成数据包。该选项只支持本地图片存储。

页面中的字段与图片修改保存在独立覆盖层。管理员上传新版数据包时，服务端会将现有覆盖层及其图片合并到新版题库，因此不会被后续更新覆盖。

入库前会按工作表行号顺序，对业务行的 A、B、C、D、E、F 列分别向上查找最近一个非空值并补齐。第 1 行表头不参与继承；补齐后的值会纳入行哈希，因此上游值变化时，依赖它的后续行也会自动更新。

## 构建与运行

服务器使用 Node 24 构建前端，Python 3.12 运行应用：

```bash
/usr/local/node24/bin/npm ci
/usr/local/node24/bin/npm run build
/usr/local/python3.12/bin/python3.12 -m pip install -r requirements.txt
CHROMIUM_EXECUTABLE_PATH=/path/to/chromium \
PORT=8910 SECRET_KEY='生产随机密钥' EDITOR_PASSWORD='编辑密码' \
  /usr/local/python3.12/bin/gunicorn --bind 0.0.0.0:8910 --workers 2 --threads 4 app:app
```

完整环境变量见 [.env.example](.env.example)。生产环境必须设置固定的 `SECRET_KEY` 和强 `EDITOR_PASSWORD`；HTTPS 部署时设置 `COOKIE_SECURE=1`。健康检查为 `GET /api/health`。Docker 镜像会安装与 Python Playwright 匹配的 Chromium；CentOS 7 宿主机部署应由运维提供可运行的 Chrome/Chromium，并通过 `CHROMIUM_EXECUTABLE_PATH` 指定，避免系统 glibc 与 Playwright 内置浏览器不兼容。

### 首次部署数据

`data/` 不进入 Git。首次部署启动后，健康检查会返回 `dataReady: false`，页面自动显示“导入奥数题库”：

1. 输入服务器环境变量 `EDITOR_PASSWORD` 配置的管理员密码。
2. 选择 `ZYBolmath-data.tar.gz` 数据包。
3. 点击“验证并导入”，等待上传、校验和安装完成。

数据包最大 256 MB，必须包含 `data/app.sqlite` 和 `data/assets/`。服务端会拒绝不安全路径、链接、缺少题库表、缺少目标工作表或图片不完整的数据包。导入成功后页面自动进入题库，初始化入口随即关闭，不能用于覆盖现有数据。

健康检查随后返回 `{"dataReady":true,"status":"ok"}`，无需登录服务器或重启应用。

### 后续更新题库

在服务器配置 `DINGTALK_DOCUMENT_URL`、`BROWSER_PROFILE_PATH` 和浏览器，并使用 `ASSET_BACKEND=local` 后，管理员可直接更新：

1. 在页面点击“编辑登录”，输入 `EDITOR_PASSWORD`。
2. 点击工具栏的“更新题库”。
3. 点击“从钉钉更新”，页面会显示登录检查、表格与图片解析、数据校验和安装进度。

浏览器 Profile 使用有权查看并导出该文档的钉钉账号登录。每次任务仍会重新检查登录、文档访问和 Excel 导出权限，并分别报告登录超时、无查看权限、无导出权限、下载异常或数据校验失败。只有查看权限但没有导出权限时任务会停止，不会通过页面抓取或其他方式绕过限制。
首次运行或登录失效时，管理员更新窗口会显示服务器 Chromium 生成的钉钉登录二维码。使用钉钉扫码后任务会在同一浏览器会话中自动继续，登录状态保存在 `BROWSER_PROFILE_PATH`，不需要服务器终端或远程桌面。生产环境必须为 `data/` 配置持久化存储，否则重新部署后需要再次扫码。

若自动导出暂时不可用，仍可在同一弹窗中上传离线数据包：手工导出新版 XLSX，使用“数据同步”命令生成 `ZYBolmath-data.tar.gz`，选择压缩包并点击“上传并更新”。

更新期间服务端会校验并备份当前数据；更新失败会保留旧题库。成功后页面自动刷新，新版基础数据生效，网页字段和图片覆盖继续保留。

同步任务状态保存在独立的 `data/sync-jobs.sqlite`，不会随题库数据库原子替换而丢失。任务由独立子进程执行，多 Gunicorn worker 下也只允许一个自动同步任务运行。表格新增、修改、删除以及图片内容单独变化都会进入现有行哈希检测；网页覆盖目前按工作表和行号关联，源表插行、删行或排序时建议提供不可变题目 ID，避免旧覆盖关联到其他题目。

## 验证

```bash
python -m unittest discover -s tests -v
npm run lint
npm run build
```