# 文件职责：无需网络和模型的词面检索基线：分词、BM25、倒数排名融合与词面重排。
"""Local lexical ranking; no model calls, credentials or cross-namespace storage.

Chinese character bigrams and Latin words intentionally avoid a downloaded tokenizer.
The reranker is a transparent lexical heuristic, NOT a neural cross-encoder.
"""

import math
import re
from collections import Counter


# tokens：英文按词、中文按相邻双字拆分，得到可用于词频统计的 token 列表。
def tokens(text):
    parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    return [
        t
        for p in parts
        for t in ([p] if p.isascii() or len(p) == 1 else [p[i : i + 2] for i in range(len(p) - 1)])
    ]


# bm25：结合词频、逆文档频率和长度归一计算相关性，只返回正分并稳定处理同分。
def bm25(query, documents):
    """Return positive (id, score), with deterministic ties. k1=1.2, b=0.75."""
    bags = {key: Counter(tokens(body)) for key, body in documents.items()}
    if not bags:
        return []
    average = sum(sum(b.values()) for b in bags.values()) / len(bags) or 1
    document_frequency = Counter(term for bag in bags.values() for term in bag)
    query_terms = sorted(set(tokens(query)))
    scores = []
    for key, bag in bags.items():
        score = 0.0
        for term in query_terms:
            frequency = bag[term]
            df = document_frequency[term]
            idf = math.log(1 + (len(bags) - df + 0.5) / (df + 0.5))
            denominator = frequency + 1.2 * (0.25 + 0.75 * sum(bag.values()) / average)
            score += idf * frequency * 2.2 / denominator
        if score > 0:
            scores.append((key, score))
    return sorted(scores, key=lambda x: (-x[1], x[0]))


# fuse：用倒数排名融合多个排序，不直接相加不同算法的原始分数。
def fuse(*rankings):
    scores = Counter()
    for ranking in rankings:
        for rank, (key, _) in enumerate(ranking, 1):
            scores[key] += 1 / (60 + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


# rerank：以查询词覆盖率加融合分重排；这是启发式规则，不是神经网络 reranker。
def rerank(query, ranking, documents):
    terms = set(tokens(query))
    # Query coverage dominates; original RRF rank resolves near ties.
    scores = [
        (key, len(terms & set(tokens(documents[key]))) / max(1, len(terms)) + score) for key, score in ranking
    ]
    return sorted(scores, key=lambda x: (-x[1], x[0]))


# tfidf_vector：构造本地 TF-IDF 余弦相似度基线，不能称作真实语义模型效果。
def tfidf_vector(query, documents):
    """Offline lexical-vector cosine baseline; NOT a semantic embedding model."""
    bags = {key: Counter(tokens(body)) for key, body in documents.items()}
    vocabulary = set(t for bag in bags.values() for t in bag)
    idf = {t: math.log((1 + len(bags)) / (1 + sum(t in b for b in bags.values()))) + 1 for t in vocabulary}

    # vector：计算词频乘 IDF 并做 L2 归一化，便于点积表示余弦相似度。
    def vector(bag):
        v = {t: count * idf[t] for t, count in bag.items() if t in idf}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1
        return {t: x / norm for t, x in v.items()}

    q = vector(Counter(tokens(query)))
    scores = [
        (key, sum(q.get(t, 0) * weight for t, weight in vector(bag).items())) for key, bag in bags.items()
    ]
    return sorted((x for x in scores if x[1] > 0), key=lambda x: (-x[1], x[0]))
