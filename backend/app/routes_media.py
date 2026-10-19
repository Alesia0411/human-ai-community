# 文件职责：受保护图片的上传、解码及下载接口；以账号和会话授权决定可见性。
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Asset, Message, Post, uid
from .policy import conversation, fail, visible_role
from .security import current

router = APIRouter()
Image.MAX_IMAGE_PIXELS = 20_000_000


# upload：校验上传体积和图片格式，规范化保存文件及 Asset 记录。
@router.post("/assets", status_code=201)
async def upload(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(None),
    a=Depends(current),
    db: Session = Depends(get_db),
):
    if conversation_id:
        conversation(db, a.id, conversation_id)
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        fail("FILE_TOO_LARGE", 413)
    try:
        im = Image.open(BytesIO(raw))
        im.load()
        im.thumbnail((2000, 2000))
        output = BytesIO()
        im.convert("RGB").save(output, "JPEG", quality=85)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        fail("INVALID_IMAGE", 422)
    ident = uid()
    directory = Path(settings().media_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (ident + ".jpg")).write_bytes(output.getvalue())
    asset = Asset(id=ident, owner_id=a.id, conversation_id=conversation_id, path=ident + ".jpg")
    db.add(asset)
    db.commit()
    return {"id": asset.id}


# download：验证图片归属或会话/内容访问范围，再返回服务器文件。
@router.get("/assets/{aid}/content")
def download(aid: str, a=Depends(current), db: Session = Depends(get_db)):
    asset = db.get(Asset, aid)
    if not asset:
        fail()
    if asset.owner_id != a.id:
        if asset.conversation_id:
            conversation(db, a.id, asset.conversation_id)
            if not db.scalar(select(Message.id).where(Message.asset_id == aid, Message.retracted.is_(False))):
                fail()
        else:
            post = db.scalar(select(Post).where(Post.asset_id == aid, Post.deleted.is_(False)))
            if not post:
                fail()
            visible_role(db, a.id, post.role_id)
    path = Path(settings().media_dir) / asset.path
    if not path.exists():
        fail()
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
