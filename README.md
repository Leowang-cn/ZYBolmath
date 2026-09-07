# 奥数大纲题库

将钉钉导出的 XLSX 增量同步到 SQLite 与本地文件或 MinIO，并通过桌面端页面浏览、筛选和维护示例题。

## 数据同步

```bash
/usr/local/python3.12/bin/python3.12 scripts/sync_workbook.py \
  /path/to/3、4年级奥数大纲.xlsx \
  --database ./data/app.sqlite \
  --asset-backend local \
  --asset-dir ./data/assets
```

相同工作簿会按 SHA-256 快速跳过；工作簿变化时只更新变化行。图片按内容哈希去重。页面中的字段与图片修改保存在独立覆盖层，不会被后续同步覆盖。

入库前会按工作表行号顺序，对业务行的 A、B、C、D、E、F 列分别向上查找最近一个非空值并补齐。第 1 行表头不参与继承；补齐后的值会纳入行哈希，因此上游值变化时，依赖它的后续行也会自动更新。

## 构建与运行

服务器使用 Node 24 构建前端，Python 3.12 运行应用：

```bash
/usr/local/node24/bin/npm ci
/usr/local/node24/bin/npm run build
/usr/local/python3.12/bin/python3.12 -m pip install -r requirements.txt
PORT=8910 SECRET_KEY='生产随机密钥' EDITOR_PASSWORD='编辑密码' \
  /usr/local/python3.12/bin/gunicorn --bind 0.0.0.0:8910 --workers 2 --threads 4 app:app
```

完整环境变量见 [.env.example](.env.example)。生产环境必须设置固定的 `SECRET_KEY` 和强 `EDITOR_PASSWORD`；HTTPS 部署时设置 `COOKIE_SECURE=1`。健康检查为 `GET /api/health`。

### 首次部署数据

`data/` 不进入 Git。首次部署启动后，健康检查会返回 `dataReady: false`，页面自动显示“导入奥数题库”：

1. 输入服务器环境变量 `EDITOR_PASSWORD` 配置的管理员密码。
2. 选择 `ZYBolmath-data.tar.gz` 数据包。
3. 点击“验证并导入”，等待上传、校验和安装完成。

数据包最大 256 MB，必须包含 `data/app.sqlite` 和 `data/assets/`。服务端会拒绝不安全路径、链接、缺少题库表、缺少目标工作表或图片不完整的数据包。导入成功后页面自动进入题库，初始化入口随即关闭，不能用于覆盖现有数据。

健康检查随后返回 `{"dataReady":true,"status":"ok"}`，无需登录服务器或重启应用。

## 验证

```bash
python -m unittest discover -s tests -v
npm run lint
npm run build
```