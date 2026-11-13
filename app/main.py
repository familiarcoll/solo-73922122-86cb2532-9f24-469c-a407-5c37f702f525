"""FastAPI 入口：上传、校验、选择、导出。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, solver

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    yield


app = FastAPI(title="水位日志 UTC 对齐工具", lifespan=lifespan)


def _load_file_or_404(fid: int) -> dict:
    f = db.get_file(fid)
    if f is None:
        raise HTTPException(404, f"文件 {fid} 不存在")
    return f


def _validation(fid: int) -> dict:
    rows = db.get_rows(fid)
    sels = db.get_selections(fid)
    return solver.validate(rows, sels)


# ---------------------------------------------------------------- API

@app.post("/api/files")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 5 MB 上限")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "文件不是有效的 UTF-8 文本")
    try:
        parsed = solver.parse_csv_text(text)
    except solver.CsvStructureError as e:
        raise HTTPException(422, str(e))
    fid = db.create_file(file.filename or "upload.csv", text, parsed)
    return {"file_id": fid, "validation": _validation(fid)}


@app.get("/api/files")
def list_files():
    out = []
    for f in db.list_files():
        v = _validation(f["id"])
        out.append({**f, "status": v["status"], "can_export": v["can_export"],
                    "n_problems": v["n_problems"], "n_ambiguous": v["n_ambiguous"]})
    return out


@app.get("/api/files/{fid}")
def file_detail(fid: int):
    f = _load_file_or_404(fid)
    return {"file": {k: f[k] for k in ("id", "name", "uploaded_at", "row_count")},
            "validation": _validation(fid)}


@app.get("/api/files/{fid}/original", response_class=PlainTextResponse)
def download_original(fid: int):
    f = _load_file_or_404(fid)
    return PlainTextResponse(
        f["raw_text"], media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="original_{fid}.csv"'})


class SelectionIn(BaseModel):
    row_id: int
    utc: str


@app.post("/api/files/{fid}/selections")
def set_selection(fid: int, body: SelectionIn):
    _load_file_or_404(fid)
    rows = db.get_rows(fid)
    row = next((r for r in rows if r.id == body.row_id), None)
    if row is None:
        raise HTTPException(404, f"行 {body.row_id} 不存在")
    try:
        chosen = datetime.fromisoformat(body.utc).astimezone(timezone.utc)
    except ValueError:
        raise HTTPException(422, f"无法解析 UTC 时刻 {body.utc!r}")
    if chosen not in row.candidates:
        raise HTTPException(409, "该时刻不在此行的合法候选中")

    # 先试算：应用该选择后整份必须仍然可行，否则拒绝保存
    sels = db.get_selections(fid)
    sels[body.row_id] = chosen
    result = solver.validate(rows, sels)
    if any(r["state"] == "conflict" for r in result["rows"]):
        bad = [f"行 {r['line_no']}（序号 {r['seq']}）"
               for r in result["rows"] if r["state"] == "conflict"]
        raise HTTPException(
            409, "该选择会导致全文件无可行解释，未保存。受影响：" + "、".join(bad))
    db.set_selection(fid, body.row_id, chosen)
    return {"validation": _validation(fid)}


@app.delete("/api/files/{fid}/selections/{row_id}")
def clear_selection(fid: int, row_id: int):
    _load_file_or_404(fid)
    db.clear_selection(fid, row_id)
    return {"validation": _validation(fid)}


@app.get("/api/files/{fid}/export")
def export(fid: int):
    _load_file_or_404(fid)
    rows = db.get_rows(fid)
    v = _validation(fid)
    if not v["can_export"]:
        raise HTTPException(409, {
            "message": "尚有行没有唯一解释，禁止导出",
            "problems": v["problems"],
            "n_ambiguous": v["n_ambiguous"],
        })
    lines = ["seq,utc_time,water_level"]
    for r, feas in zip(rows, v["feasible"]):
        u = datetime.fromisoformat(feas[0]).astimezone(timezone.utc)
        lines.append(f"{r.seq},{u:%Y-%m-%dT%H:%M:%SZ},{r.water_text}")
    name = f"utc_aligned_{fid}.csv"
    return PlainTextResponse(
        "\n".join(lines) + "\n", media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.delete("/api/files/{fid}")
def remove_file(fid: int):
    _load_file_or_404(fid)
    db.delete_file(fid)
    return {"ok": True}


# ---------------------------------------------------------------- 静态页面

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
