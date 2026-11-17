"""周报 API（迁移自旧系统 routers/weekly_report.py）— 含审核流与 AI 润色
"""
import os
import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user, require_module, filter_store_by_permission
from app.models import WeeklyReport, WeeklyReportImage, Store, SysUser, OperationLog
from app.services.ai.client import chat

router = APIRouter()

UPLOAD_ROOT = settings.UPLOAD_DIR / "weekly"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


class ReportCreate(BaseModel):
    """周报新建请求"""
    store: str
    year: int
    week_num: int
    title: str
    content: str = ""


class ReportUpdate(BaseModel):
    """周报更新请求（仅草稿/被驳回状态可改）"""
    title: str | None = None
    content: str | None = None


class ReviewRequest(BaseModel):
    """审核动作：submit 提交 / approve 通过 / reject 驳回"""
    action: str          # submit / approve / reject
    comment: str = ""


class PolishRequest(BaseModel):
    """AI 润色请求：content 原文 + instruction 润色要求"""
    content: str
    instruction: str = "请把这段周报内容润色得更专业、条理清晰，保留所有数据事实，不要编造。"


@router.get("")
def list_reports(store: str | None = None, status: str | None = None,
                 db: Session = Depends(get_db), user=Depends(require_module("weekly"))):
    """周报列表（可按店铺/状态筛选）

    ★ 返回**裸数组**：旧前端 WeeklyReport 页直接 `setReports(res.data)` 后 map，
      若包成 {items:[...]} 会导致页面拿不到数组而渲染异常。
    """
    q = db.query(WeeklyReport)
    if store:
        s = db.query(Store).filter(Store.store_code == store).first()
        if s:
            q = q.filter(WeeklyReport.store_id == s.id)
    if status:
        q = q.filter(WeeklyReport.status == status)
    rows = q.order_by(WeeklyReport.year.desc(), WeeklyReport.week_num.desc()).limit(200).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    users = {u.id: u.username for u in db.query(SysUser).all()}
    return [{
        'id': r.id, 'store_code': stores.get(r.store_id, ''),
        'store_id': r.store_id,
        'year': r.year, 'week_num': r.week_num, 'title': r.title,
        'content': r.content, 'polished_content': r.polished_content,
        'status': r.status, 'author': users.get(r.author_id, ''),
        'reviewer': users.get(r.reviewer_id, ''), 'review_comment': r.review_comment,
        'created_at': r.created_at.isoformat() if r.created_at else None,
        'updated_at': r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]


@router.post("")
def create_report(req: ReportCreate, db: Session = Depends(get_db),
                  user=Depends(require_module("weekly"))):
    """新建周报草稿"""
    s = filter_store_by_permission(user, req.store, db)
    if not s:
        raise HTTPException(status_code=404, detail="店铺不存在")
    r = WeeklyReport(store_id=s.id, year=req.year, week_num=req.week_num,
                     title=req.title, content=req.content, status='draft', author_id=user.id)
    db.add(r)
    db.commit()
    return {'id': r.id}


@router.put("/{report_id}")
def update_report(report_id: int, req: ReportUpdate, db: Session = Depends(get_db),
                  user=Depends(require_module("weekly"))):
    """更新周报内容（非草稿/驳回状态拒绝修改）"""
    r = db.query(WeeklyReport).get(report_id)
    if not r:
        raise HTTPException(status_code=404, detail="周报不存在")
    if r.status not in ('draft', 'rejected'):
        raise HTTPException(status_code=400, detail="非草稿/驳回状态不可编辑")
    if req.title is not None:
        r.title = req.title
    if req.content is not None:
        r.content = req.content
    r.updated_at = datetime.now()
    db.commit()
    return {'ok': True}


@router.post("/{report_id}/review")
def review_report(report_id: int, req: ReviewRequest, db: Session = Depends(get_db),
                  user=Depends(require_module("weekly"))):
    """审核流：submit → submitted；approve → approved；reject → rejected"""
    r = db.query(WeeklyReport).get(report_id)
    if not r:
        raise HTTPException(status_code=404, detail="周报不存在")
    if req.action == 'submit':
        r.status = 'submitted'
        r.submitted_at = datetime.now()
    elif req.action == 'approve':
        r.status = 'approved'
        r.reviewer_id = user.id
        r.reviewed_at = datetime.now()
        r.review_comment = req.comment
    elif req.action == 'reject':
        r.status = 'rejected'
        r.reviewer_id = user.id
        r.reviewed_at = datetime.now()
        r.review_comment = req.comment
    else:
        raise HTTPException(status_code=400, detail="action 必须是 submit/approve/reject")
    db.add(OperationLog(user_id=user.id, username=user.username,
                        action=f"weekly_{req.action}", target=str(report_id), result="ok"))
    db.commit()
    return {'ok': True, 'status': r.status}


@router.get("/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db),
               user=Depends(require_module("weekly"))):
    """单条周报详情（含图片）"""
    r = db.query(WeeklyReport).get(report_id)
    if not r:
        raise HTTPException(status_code=404, detail="周报不存在")
    st = db.query(Store).get(r.store_id)
    users = {u.id: u.username for u in db.query(SysUser).all()}
    imgs = db.query(WeeklyReportImage).filter(
        WeeklyReportImage.report_id == report_id).order_by(WeeklyReportImage.sort_order).all()
    return {
        'id': r.id, 'store_code': st.store_code if st else '',
        'year': r.year, 'week_num': r.week_num, 'title': r.title,
        'content': r.content, 'polished_content': r.polished_content,
        'status': r.status, 'author': users.get(r.author_id, ''),
        'reviewer': users.get(r.reviewer_id, ''), 'review_comment': r.review_comment,
        'created_at': r.created_at.isoformat() if r.created_at else None,
        'updated_at': r.updated_at.isoformat() if r.updated_at else None,
        'images': [{
            'id': i.id, 'filename': i.filename, 'description': i.description,
            'url': f"/api/v2/weekly-reports/images/{os.path.basename(i.file_path)}",
        } for i in imgs],
    }


@router.delete("/{report_id}")
def delete_report(report_id: int, db: Session = Depends(get_db),
                  user=Depends(require_module("weekly"))):
    """删除周报及其图片"""
    r = db.query(WeeklyReport).get(report_id)
    if not r:
        raise HTTPException(status_code=404, detail="周报不存在")
    db.query(WeeklyReportImage).filter(WeeklyReportImage.report_id == report_id).delete()
    db.delete(r)
    db.add(OperationLog(user_id=user.id, username=user.username, action="delete_weekly",
                        target=str(report_id), result="ok"))
    db.commit()
    return {"ok": True}


@router.get("/{report_id}/export")
def export_report(report_id: int, db: Session = Depends(get_db),
                  user=Depends(require_module("weekly"))):
    """导出周报为 Word（含图片）"""
    import io
    from docx import Document
    from docx.shared import Inches
    from fastapi.responses import StreamingResponse

    r = db.query(WeeklyReport).get(report_id)
    if not r:
        raise HTTPException(status_code=404, detail="周报不存在")
    st = db.query(Store).get(r.store_id)
    imgs = db.query(WeeklyReportImage).filter(
        WeeklyReportImage.report_id == report_id).order_by(WeeklyReportImage.sort_order).all()

    doc = Document()
    doc.add_heading(r.title, level=0)
    doc.add_paragraph(f"店铺：{st.store_code if st else ''}    {r.year} 年第 {r.week_num} 周")
    body = r.polished_content or r.content or ''
    for para in (body or '').split('\n'):
        doc.add_paragraph(para)
    for img in imgs:
        try:
            if os.path.exists(img.file_path):
                doc.add_picture(img.file_path, width=Inches(5.5))
                if img.description:
                    doc.add_paragraph(img.description)
        except Exception:
            pass
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    fname = f"周报_{st.store_code if st else ''}_{r.year}W{r.week_num}.docx"
    from urllib.parse import quote
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@router.post("/polish")
async def polish(req: PolishRequest, user=Depends(require_module("weekly"))):
    """AI 润色周报内容"""
    try:
        text = await chat([{"role": "user", "content": f"{req.instruction}\n\n原文：\n{req.content}"}],
                          system="你是资深亚马逊运营周报写作助手，输出专业、简洁、数据准确的中文周报。",
                          max_tokens=4000)
        return {'polished': text}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 润色失败: {e}")


@router.post("/{report_id}/images")
async def upload_image(report_id: int, file: UploadFile = File(...),
                       description: str = Form(""), db: Session = Depends(get_db),
                       user=Depends(require_module("weekly"))):
    """上传周报配图（png/jpg/gif/webp）"""
    r = db.query(WeeklyReport).get(report_id)
    if not r:
        raise HTTPException(status_code=404, detail="周报不存在")
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        raise HTTPException(status_code=400, detail="仅支持图片格式")
    fname = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_ROOT / fname
    with open(dest, 'wb') as out:
        import shutil
        shutil.copyfileobj(file.file, out)
    img = WeeklyReportImage(report_id=report_id, filename=file.filename or fname,
                            file_path=str(dest), description=description)
    db.add(img)
    db.commit()
    return {'id': img.id, 'file_path': f"/api/v2/weekly-reports/images/{fname}"}


@router.get("/images/{fname}")
def get_image(fname: str):
    """读取周报图片文件（静态资源）"""
    from fastapi.responses import FileResponse
    path = UPLOAD_ROOT / fname
    if not path.exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(str(path))


@router.get("/{report_id}/images")
def list_images(report_id: int, db: Session = Depends(get_db),
                user=Depends(require_module("weekly"))):
    """周报图片列表"""
    rows = db.query(WeeklyReportImage).filter(
        WeeklyReportImage.report_id == report_id).order_by(WeeklyReportImage.sort_order).all()
    return {'items': [{
        'id': i.id, 'filename': i.filename, 'description': i.description,
        'url': f"/api/v2/weekly-reports/images/{os.path.basename(i.file_path)}",
    } for i in rows]}


@router.delete("/images/{image_id}")
def delete_image(image_id: int, db: Session = Depends(get_db),
                 user=Depends(require_module("weekly"))):
    """删除周报图片（同时删磁盘文件）"""
    img = db.query(WeeklyReportImage).get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        os.remove(img.file_path)
    except OSError:
        pass
    db.delete(img)
    db.commit()
    return {'ok': True}
