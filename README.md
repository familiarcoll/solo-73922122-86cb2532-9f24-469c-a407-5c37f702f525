# 水位日志 UTC 对齐工具（Europe/Berlin）

把海外水位站的旧日志（本地时刻）对齐到 UTC 雷达降雨时间轴，避免夏令时
回拨造成洪峰错配。技术栈：FastAPI + zoneinfo + SQLite，单容器交付。

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> 。SQLite 数据落在命名卷 `app_data`
（容器内 `/data/app.db`），刷新页面或重启容器后原件与有效选择都保留。

也可本地直接运行（Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

## 输入格式

CSV，UTF-8，含表头，最多 5000 数据行，三列（中英文名均可，顺序不限）：

| 列 | 说明 |
| --- | --- |
| `seq` / `序号` | 严格递增整数，上传后顺序即时间顺序，绝不重排 |
| `local_time` / `本地时刻` | Europe/Berlin 本地时刻，如 `2023-10-29 02:30`；可带 UTC 偏移作**锚点**，如 `2023-10-29 02:30+02:00` |
| `water_level` / `水位` | 数值，导出时保留原文 |

## 对齐规则

- 普通本地时刻 → 唯一 UTC；**秋季回拨**重叠时刻（如 2023-10-29
  02:00–02:59）→ 两个 UTC 候选（CEST/CET 各一次），**不默认选择**；
- **春季跳跃**不存在的本地时刻（如 2024-03-31 02:30）→ 该行标错，
  标明行号并阻止导出；
- 锚点（带偏移）→ 瞬间唯一，但偏移必须与 Berlin 规则一致、墙上时刻必须
  真实存在，否则视为矛盾行；
- 全文件约束：真实时刻（UTC）按序号**严格递增**。用前向-后向传播逐行
  求出在全链约束下仍然可行的全部候选；与约束冲突的候选划线标出；
- 用户在界面上为歧义行选定候选后，**立即重算全文件**：若导致任何行
  无可行解释，该选择被拒绝（409）且**不保存**；
- 仅当所有行都有唯一解释时才可下载 UTC CSV（`seq,utc_time,water_level`，
  保留原序号与水位原文）。

## 内置样例（页面左侧一键加载）

| 文件 | 演示内容 |
| --- | --- |
| `ambiguous_fallback.csv` | 回拨日重叠时刻，两行各有两个候选，需人工选择 |
| `anchored_fallback.csv` | 锚点 + 重复墙钟时刻，由严格递增约束自动全部消解，可直接导出 |
| `contradiction.csv` | 不存在的本地时刻、锚点偏移矛盾、顺序矛盾，三种都会标明行号并阻止导出 |
| `clean_summer.csv` | 普通夏季数据，直接可导出 |

样例文件同时位于 `app/static/samples/`，可直接下载查看。

## API 摘要

- `POST /api/files` 上传（multipart），结构错误 422 整份拒绝
- `GET /api/files` / `GET /api/files/{id}` 列表 / 详情（含逐行候选与状态）
- `POST /api/files/{id}/selections` 选定某行候选（冲突 409 不保存）
- `DELETE /api/files/{id}/selections/{row_id}` 清除选择
- `GET /api/files/{id}/export` 导出 UTC CSV（未全部唯一时 409）
- `GET /api/files/{id}/original` 下载上传原件
- `DELETE /api/files/{id}` 删除文件及其选择
