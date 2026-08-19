"""数据源路由：数据库连接 + 文件桶上传/解析。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import BucketFile, DataSource
from ..schemas import BucketFileOut, DataSourceIn, DataSourceOut, Msg, QueryResult, TableInfo
from ..services import datasource_service, doc_parser

router = APIRouter(prefix="/data-sources", tags=["data-sources"])


def _out(ds: DataSource) -> DataSourceOut:
    return DataSourceOut(
        id=ds.id,
        scenario_id=ds.scenario_id,
        name=ds.name,
        type=ds.type,
        config=ds.config or {},
        status=ds.status,
        last_error=ds.last_error,
        created_at=ds.created_at,
        file_count=len(ds.files),
    )


@router.get("", response_model=list[DataSourceOut])
def list_data_sources(scenario_id: str | None = None, db: Session = Depends(get_db)):
    stmt = select(DataSource)
    if scenario_id:
        stmt = stmt.where(DataSource.scenario_id == scenario_id)
    return [_out(d) for d in db.execute(stmt).scalars().all()]


@router.post("", response_model=DataSourceOut)
def create_data_source(payload: DataSourceIn, db: Session = Depends(get_db)):
    ds = DataSource(**payload.model_dump())
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return _out(ds)


@router.put("/{ds_id}", response_model=DataSourceOut)
def update_data_source(ds_id: str, payload: DataSourceIn, db: Session = Depends(get_db)):
    ds = db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    for k, v in payload.model_dump().items():
        setattr(ds, k, v)
    ds.status = "unknown"
    db.commit()
    db.refresh(ds)
    return _out(ds)


@router.delete("/{ds_id}", response_model=Msg)
def delete_data_source(ds_id: str, db: Session = Depends(get_db)):
    ds = db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    for f in list(ds.files):
        datasource_service.delete_bucket_file(f)
    db.delete(ds)
    db.commit()
    return Msg(message="已删除")


@router.post("/{ds_id}/test", response_model=Msg)
def test_data_source(ds_id: str, db: Session = Depends(get_db)):
    ds = db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    if ds.type == "file_bucket":
        ds.status = "ok"
        ds.last_error = ""
        db.commit()
        return Msg(ok=True, message="文件桶就绪")
    ok, msg = datasource_service.test_connection(ds)
    ds.status = "ok" if ok else "error"
    ds.last_error = "" if ok else msg
    db.commit()
    return Msg(ok=ok, message=msg)


@router.get("/{ds_id}/tables", response_model=list[TableInfo])
def list_tables(ds_id: str, db: Session = Depends(get_db)):
    ds = db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    if ds.type == "file_bucket":
        return []
    try:
        return datasource_service.list_tables(ds)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"获取表结构失败: {exc}")


@router.post("/{ds_id}/query", response_model=QueryResult)
def query(ds_id: str, payload: dict, db: Session = Depends(get_db)):
    ds = db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    sql = payload.get("sql", "")
    if not sql.strip():
        raise HTTPException(400, "SQL 不能为空")
    try:
        return datasource_service.run_query(ds, sql)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"查询失败: {exc}")


# ── 文件桶 ────────────────────────────────────
@router.get("/{ds_id}/files", response_model=list[BucketFileOut])
def list_files(ds_id: str, db: Session = Depends(get_db)):
    ds = db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    if ds.type != "file_bucket":
        return []
    return list(ds.files)


@router.post("/{ds_id}/files", response_model=list[BucketFileOut])
async def upload_files(ds_id: str, files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    ds = db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    if ds.type != "file_bucket":
        raise HTTPException(400, "该数据源不是文件桶")
    created: list[BucketFile] = []
    for uf in files:
        content = await uf.read()
        bf = datasource_service.save_bucket_file(ds, uf.filename or "file", content)
        db.add(bf)
        db.commit()
        db.refresh(bf)
        # 解析
        r = doc_parser.parse_file(bf.stored_path, bf.filename)
        bf.status = "parsed" if r["status"] == "success" else "error"
        bf.parsed_text = r.get("text", "")
        bf.error = "" if r["status"] == "success" else r.get("message", "")
        db.commit()
        db.refresh(bf)
        created.append(bf)
    return created


@router.post("/files/{file_id}/reparse", response_model=BucketFileOut)
def reparse_file(file_id: str, db: Session = Depends(get_db)):
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    r = doc_parser.parse_file(bf.stored_path, bf.filename)
    bf.status = "parsed" if r["status"] == "success" else "error"
    bf.parsed_text = r.get("text", "")
    bf.error = "" if r["status"] == "success" else r.get("message", "")
    db.commit()
    db.refresh(bf)
    return bf


@router.get("/files/{file_id}/text")
def file_text(file_id: str, db: Session = Depends(get_db)):
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    return {"filename": bf.filename, "text": bf.parsed_text}


@router.get("/files/{file_id}/download")
def file_download(file_id: str, db: Session = Depends(get_db)):
    """下载文件桶中的文件（附件）。"""
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    p = Path(bf.stored_path)
    if not p.exists():
        raise HTTPException(404, "文件已丢失")
    return FileResponse(
        p,
        media_type=bf.mime or "application/octet-stream",
        filename=bf.filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(bf.filename)}"},
    )


@router.delete("/files/{file_id}", response_model=Msg)
def delete_file(file_id: str, db: Session = Depends(get_db)):
    bf = db.get(BucketFile, file_id)
    if not bf:
        raise HTTPException(404, "文件不存在")
    datasource_service.delete_bucket_file(bf)
    db.delete(bf)
    db.commit()
    return Msg(message="已删除")
