# 文件职责：从仍获准的 SQL 向量副本重建 Qdrant 数据；不重新调用收费嵌入模型。
"""Rebuild authorized Qdrant points from authoritative SQL; no paid re-embedding."""

from sqlalchemy import select

from .db import SessionLocal
from .models import Conversation, KnowledgeChunk, KnowledgeDocument, Role
from .rag import collection, qm, vector_db


# rebuild：筛选有效文档、角色和会话，再创建所需集合并 upsert 已保存向量。
def rebuild():
    with SessionLocal() as db, vector_db() as vdb:
        for doc in db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.deleted.is_(False), KnowledgeDocument.status == "READY"
            )
        ):
            role = db.get(Role, doc.role_id)
            if not role or role.deleted:
                continue
            if doc.conversation_id:
                conv = db.get(Conversation, doc.conversation_id)
                if not conv or not conv.memory_enabled:
                    continue
            chunks = list(db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id)))
            if not chunks:
                continue
            name = collection(doc.embedding_provider, doc.embedding_model)
            if not vdb.collection_exists(name):
                vdb.create_collection(
                    name,
                    vectors_config=qm.VectorParams(size=len(chunks[0].vector), distance=qm.Distance.COSINE),
                )
            vdb.upsert(
                name,
                points=[
                    qm.PointStruct(
                        id=c.id,
                        vector=c.vector,
                        payload={
                            "document_id": doc.id,
                            "role_id": doc.role_id,
                            "conversation_id": doc.conversation_id,
                            "owner_id": doc.owner_id,
                        },
                    )
                    for c in chunks
                ],
                wait=True,
            )


if __name__ == "__main__":
    rebuild()
