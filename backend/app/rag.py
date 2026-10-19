# 文件职责：知识切分、嵌入、Qdrant 检索与混合排序；向量命中不代替角色、会话和提供商权限检查。
"""Scoped Qdrant retrieval with authoritative SQL post-filtering."""

import hashlib
import math
from contextlib import contextmanager

import httpx
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient
from qdrant_client import models as qm
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import defer

from .config import settings
from .db import SessionLocal
from .gateways import begin_call, complete_call
from .models import Conversation, KnowledgeChunk, KnowledgeDocument
from .policy import fail
from .r2_policy import check_run, connection_for
from .retrieval_rank import bm25, fuse, rerank
from .security import decrypt


# vector_db：按配置打开远程或本地 Qdrant 客户端，退出上下文时关闭资源。
@contextmanager
def vector_db():
    cfg = settings()
    client = (
        QdrantClient(url=cfg.qdrant_url, timeout=15) if cfg.qdrant_url else QdrantClient(path=cfg.qdrant_path)
    )
    try:
        yield client
    finally:
        client.close()


# collection：由提供商与模型生成集合名，隔离不同嵌入空间。
def collection(provider, model):
    return "knowledge_" + hashlib.sha256((provider + ":" + model).encode()).hexdigest()[:16]


# mock_embedding：生成确定性的测试向量，仅供演示和规则测试，不代表真实语义嵌入。
def mock_embedding(text):
    # A reproducible demonstration vector; not a semantic model or an evaluation baseline.
    vec = [0.0] * 128
    for i in range(max(1, len(text) - 1)):
        token = text[i : i + 2].lower()
        vec[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 128] += 1
    norm = math.sqrt(sum(x * x for x in vec)) or 1
    return [v / norm for v in vec]


# embed：通过付款人授权的连接生成嵌入，使用调用账本避免重复消费。
async def embed(rid, key, texts, connection_id, model):
    with SessionLocal() as db:
        run, role, _ = check_run(db, rid)
        conn = connection_for(db, run.payer_id, connection_id, role.provider_scope)
        provider, credential = conn.provider, decrypt(conn.encrypted_key)
    ident, cached = begin_call(
        rid,
        key,
        "embedding",
        sum(len(t.encode()) for t in texts) + 256,
        billing="mock" if provider == "mock" else "payer",
    )
    if cached is not None:
        return cached["vectors"]
    if provider == "mock":
        if not settings().demo_mode:
            fail("MOCK_DISABLED", 409)
        vectors, used = [mock_embedding(t) for t in texts], 0
    else:
        async with httpx.AsyncClient(timeout=45, trust_env=False, follow_redirects=False) as client:
            response = await client.post(
                settings().providers[provider] + "/embeddings",
                headers={"Authorization": "Bearer " + credential},
                json={"model": model, "input": texts},
            )
            response.raise_for_status()
            data = response.json()
        vectors = [x["embedding"] for x in sorted(data["data"], key=lambda x: x["index"])]
        used = data.get("usage", {}).get("total_tokens")
    if len(vectors) != len(texts) or not vectors or not all(len(v) == len(vectors[0]) for v in vectors):
        fail("INVALID_EMBEDDING", 502)
    complete_call(ident, {"vectors": vectors}, used)
    return vectors


# index_document：将授权文档切片、生成嵌入并写入 SQL 与 Qdrant；最终 READY 由完成流程设置。
async def index_document(rid):
    with SessionLocal() as db:
        run, _, _ = check_run(db, rid)
        doc = db.get(KnowledgeDocument, run.context["document_id"])
        body, docid, connid, model, provider = (
            doc.body,
            doc.id,
            doc.embedding_connection_id,
            doc.embedding_model,
            doc.embedding_provider,
        )
    # 相邻片段保留重叠上下文；长度单位取切分器默认计量，不等于模型 token。
    chunks = RecursiveCharacterTextSplitter(chunk_size=600, chunk_overlap=80).split_text(body)
    vectors = []
    for offset in range(0, len(chunks), 8):
        vectors += await embed(rid, f"index:{offset}", chunks[offset : offset + 8], connid, model)
    with SessionLocal() as db:
        check_run(db, rid)
        doc = db.get(KnowledgeDocument, docid)
        existing = list(db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == docid)))
        if not existing:
            existing = [
                KnowledgeChunk(document_id=docid, body=t, vector=v, position=i)
                for i, (t, v) in enumerate(zip(chunks, vectors))
            ]
            db.add_all(existing)
            db.flush()
        points = [
            qm.PointStruct(
                id=c.id,
                vector=c.vector,
                payload={
                    "document_id": docid,
                    "role_id": doc.role_id,
                    "conversation_id": doc.conversation_id,
                    "owner_id": doc.owner_id,
                },
            )
            for c in existing
        ]
        with vector_db() as vdb:
            name = collection(provider, model)
            if not vdb.collection_exists(name):
                vdb.create_collection(
                    name, vectors_config=qm.VectorParams(size=len(vectors[0]), distance=qm.Distance.COSINE)
                )
            vdb.upsert(name, points=points, wait=True)
        # Final run completion will recheck authorization before marking ready.
        db.commit()
    return {"document_id": docid}


# retrieve：限定角色/会话/提供商检索候选，回查 SQL 授权后按配置执行向量或混合排序。
async def retrieve(rid, query):
    with SessionLocal() as db:
        run, role, model_conn = check_run(db, rid)
        if run.purpose != "CHAT":
            return []
        conv = db.get(Conversation, run.conversation_id)
        docs = list(
            db.scalars(
                select(KnowledgeDocument)
                .options(defer(KnowledgeDocument.body))
                .where(
                    KnowledgeDocument.role_id == role.id,
                    KnowledgeDocument.deleted.is_(False),
                    KnowledgeDocument.status == "READY",
                    or_(
                        KnowledgeDocument.conversation_id == "",
                        and_(
                            KnowledgeDocument.conversation_id == conv.id,
                            KnowledgeDocument.owner_id == run.payer_id,
                            conv.memory_enabled,
                        ),
                    ),
                )
            )
        )
        docs = [
            d
            for d in docs
            if model_conn.provider in d.providers
            and d.embedding_provider in role.provider_scope
            and (
                not d.conversation_id
                or (d.conversation_id == conv.id and d.owner_id == run.payer_id and conv.memory_enabled)
            )
        ]
        # 查询嵌入使用访客自己的连接，不能借用知识拥有者的 Key。
        connid = conv.grants.get("embedding_connection_id") or run.connection_id
        embed_conn = connection_for(db, run.payer_id, connid, role.provider_scope)
        # Query must use the same embedding space and the visitor's own credentials.
        docs = [d for d in docs if d.embedding_provider == embed_conn.provider]
        groups = {}
        for d in docs:
            groups.setdefault(d.embedding_model, []).append(d.id)
        provider = embed_conn.provider
    hits = []
    for model, ids in groups.items():
        vector = (await embed(rid, "query:" + model, [query], connid, model))[0]
        with vector_db() as vdb:
            name = collection(provider, model)
            if not vdb.collection_exists(name):
                continue
            points = vdb.query_points(
                name,
                query=vector,
                query_filter=qm.Filter(
                    must=[
                        qm.FieldCondition(key="role_id", match=qm.MatchValue(value=role.id)),
                        qm.FieldCondition(key="document_id", match=qm.MatchAny(any=ids)),
                    ]
                ),
                limit=20 if settings().retrieval_mode != "vector" else 5,
            ).points
        with SessionLocal() as db:
            run, role, conn = check_run(db, rid)
            conv = db.get(Conversation, run.conversation_id)
            for p in points:
                chunk = db.get(
                    KnowledgeChunk,
                    str(p.id).replace("-", ""),
                    options=[defer(KnowledgeChunk.body), defer(KnowledgeChunk.vector)],
                )
                doc = (
                    db.get(KnowledgeDocument, chunk.document_id, options=[defer(KnowledgeDocument.body)])
                    if chunk
                    else None
                )
                if (
                    not doc
                    or doc.deleted
                    or doc.status != "READY"
                    or doc.role_id != role.id
                    or conn.provider not in doc.providers
                ):
                    continue
                if doc.conversation_id and (
                    doc.conversation_id != conv.id or doc.owner_id != run.payer_id or not conv.memory_enabled
                ):
                    continue
                if p.score > 0.15:
                    hits.append(
                        {
                            "id": chunk.id,
                            "document_id": doc.id,
                            "title": doc.title,
                            "text": chunk.body,
                            "score": p.score,
                        }
                    )
    # 相似度降序，ID 用于同分稳定排序，便于复现和测试。
    vector_hits = sorted(hits, key=lambda x: (-x["score"], x["id"]))
    if settings().retrieval_mode == "vector":
        return vector_hits[:5]
    # Re-authorize all metadata before loading text for the lexical channel.
    # The entire local corpus is bounded by the project's small-circle deployment.
    scoped = {h["id"]: h for h in hits}
    with SessionLocal() as db:
        run, role, conn = check_run(db, rid)
        conv = db.get(Conversation, run.conversation_id)
        for ids in groups.values():
            for doc in db.scalars(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.id.in_(ids))
                .options(defer(KnowledgeDocument.body))
            ):
                if (
                    doc.deleted
                    or doc.status != "READY"
                    or doc.role_id != role.id
                    or conn.provider not in doc.providers
                ):
                    continue
                if doc.conversation_id and (
                    doc.conversation_id != conv.id or doc.owner_id != run.payer_id or not conv.memory_enabled
                ):
                    continue
                for chunk in db.scalars(
                    select(KnowledgeChunk)
                    .where(KnowledgeChunk.document_id == doc.id)
                    .options(defer(KnowledgeChunk.vector))
                ):
                    scoped[chunk.id] = {
                        "id": chunk.id,
                        "document_id": doc.id,
                        "title": doc.title,
                        "text": chunk.body,
                        "score": 0,
                    }
        # Even vector hits may have been revoked since their first filter.
        for key in list(scoped):
            try:
                valid_references(db, rid, [scoped[key]])
            except Exception:
                del scoped[key]
    texts = {key: hit["text"] for key, hit in scoped.items()}
    ranking = fuse([(h["id"], h["score"]) for h in vector_hits if h["id"] in scoped], bm25(query, texts)[:20])
    # 可选重排是本项目词面启发式，不是额外收费模型调用。
    if settings().retrieval_mode == "hybrid_rerank":
        ranking = rerank(query, ranking, texts)
    return [{**scoped[key], "score": score} for key, score in ranking[:5]]


# valid_references：使用或发布引用前再核对文档有效性、角色、提供商和私有会话范围。
def valid_references(db, rid, refs):
    run, role, conn = check_run(db, rid)
    conv = db.get(Conversation, run.conversation_id)
    for ref in refs:
        doc = db.get(KnowledgeDocument, ref["document_id"])
        if (
            not doc
            or doc.deleted
            or doc.status != "READY"
            or doc.role_id != role.id
            or conn.provider not in doc.providers
        ):
            fail("KNOWLEDGE_REVOKED", 409)
        if doc.conversation_id and (
            not conv
            or not conv.memory_enabled
            or doc.conversation_id != conv.id
            or doc.owner_id != run.payer_id
        ):
            fail("MEMORY_REVOKED", 409)


# purge_revoked：清除撤回文档的向量及 SQL 片段，并使失效人设候选不可继续使用。
def purge_revoked():
    with SessionLocal() as db:
        from .models import PersonaCandidate
        from .r2_policy import candidate_valid

        for candidate in db.scalars(select(PersonaCandidate).where(PersonaCandidate.valid.is_(True))):
            if not candidate_valid(db, candidate):
                candidate.valid = False
                candidate.fields = {}
        db.commit()
        docs = list(
            db.scalars(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.deleted.is_(True), KnowledgeDocument.status != "PURGED"
                )
            )
        )
        for doc in docs:
            with vector_db() as vdb:
                name = collection(doc.embedding_provider, doc.embedding_model)
                if vdb.collection_exists(name):
                    vdb.delete(
                        name,
                        qm.FilterSelector(
                            filter=qm.Filter(
                                must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=doc.id))]
                            )
                        ),
                        wait=True,
                    )
            for chunk in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id)):
                db.delete(chunk)
            doc.body, doc.status = "", "PURGED"
        db.commit()
