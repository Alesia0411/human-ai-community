# 文件职责：离线检索消融与工具预测评分；默认词面基线不能冒充真实语义模型效果。
"""Reproducible retrieval ablation and optional real tool-prediction scoring.

Run from repository root: .venv/Scripts/python evaluations/run.py
No network, application database, credentials or external model calls.
"""

import argparse
import hashlib
import json
import math
import platform
import statistics
import time
from pathlib import Path

import jsonschema
from app.retrieval_rank import bm25, fuse, rerank, tfidf_vector
from app.tool_gateway import tool_schemas

ROOT = Path(__file__).resolve().parent


# digest：计算输入文件 SHA256，绑定评测数据与报告。
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# score_tools：验证预测覆盖所有案例，再分别检查工具选择、参数规范及期望参数。
def score_tools(dataset, predictions):
    expected_ids = {q["id"] for q in dataset["tools"]}
    rows = predictions["rows"]
    if (
        len({r["id"] for r in rows}) != len(rows)
        or {r["id"] for r in rows} != expected_ids
    ):
        raise ValueError("Tool predictions must contain exactly one row per fixture id")
    by_id = {r["id"]: r for r in rows}
    results = []
    for question in dataset["tools"]:
        row = by_id[question["id"]]
        correct = row["decision"] == question["expected"]
        args_ok = False
        if question["expected"] in {"none", "clarify", "refuse"}:
            args_ok = not row.get("arguments")
        elif correct:
            try:
                args = row.get("arguments", {})
                jsonschema.validate(
                    args, tool_schemas([row["decision"]])[0]["function"]["parameters"]
                )
                args_ok = all(
                    args.get(k) == v for k, v in question["argument_subset"].items()
                )
                if row["decision"] == "image_generate":
                    args_ok &= bool(args.get("prompt", "").strip())
                if row["decision"] == "vision_describe":
                    args_ok &= bool(args.get("question", "").strip())
            except (jsonschema.ValidationError, IndexError):
                args_ok = False
        results.append(
            {
                "id": question["id"],
                "split": question["split"],
                "decision_correct": correct,
                "arguments_valid": args_ok,
                "joint_correct": correct and args_ok,
            }
        )
    return {
        "status": "scored_supplied_predictions",
        "provenance": predictions.get("provenance"),
        "correct": sum(x["joint_correct"] for x in results),
        "total": len(results),
        "rows": results,
    }


# evaluate：比较无检索、向量基线、混合与重排，记录逐题命中、排名和耗时。
def evaluate(dataset, semantic=None):
    traces = []
    for q in dataset["queries"]:
        docs = {
            d["id"]: d["text"]
            for d in dataset["documents"]
            if d["role_id"] == q["role_id"]
        }
        start = time.perf_counter()
        if semantic:
            # Supplied rankings must have been obtained using a real embedding model.
            vector = [(key, float(score)) for key, score in semantic[q["id"]]]
            if len({key for key, _ in vector}) != len(vector) or any(
                key not in docs or not math.isfinite(score) for key, score in vector
            ):
                raise ValueError(
                    "Duplicate or cross-role retrieval in supplied vector rankings"
                )
        else:
            vector = tfidf_vector(q["query"], docs)
        vector_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        hybrid = fuse(vector[:20], bm25(q["query"], docs)[:20])
        hybrid_ms = (time.perf_counter() - start) * 1000 + vector_ms
        start = time.perf_counter()
        reranked = rerank(q["query"], hybrid, docs)
        rerank_ms = (time.perf_counter() - start) * 1000 + hybrid_ms
        for mode, ranking, elapsed in [
            ("none", [], 0),
            ("vector", vector, vector_ms),
            ("hybrid", hybrid, hybrid_ms),
            ("hybrid_rerank", reranked, rerank_ms),
        ]:
            found = [key for key, _ in ranking[:3]]
            relevant = set(q["relevant"])
            positions = [i for i, key in enumerate(found, 1) if key in relevant]
            traces.append(
                {
                    "id": q["id"],
                    "split": q["split"],
                    "mode": mode,
                    "top3": found,
                    "relevant": q["relevant"],
                    "hits": len(set(found) & relevant),
                    "rr": 1 / min(positions) if positions else 0,
                    "no_answer": not relevant,
                    "retrieved_on_no_answer": not relevant and bool(found),
                    "elapsed_ms": round(elapsed, 4),
                }
            )
    summaries = []
    for split in ["dev", "test"]:
        for mode in ["none", "vector", "hybrid", "hybrid_rerank"]:
            rows = [x for x in traces if x["split"] == split and x["mode"] == mode]
            answerable = [x for x in rows if not x["no_answer"]]
            unknown = [x for x in rows if x["no_answer"]]
            hits = sum(x["hits"] for x in answerable)
            denominator = sum(len(x["relevant"]) for x in answerable)
            summaries.append(
                {
                    "split": split,
                    "mode": mode,
                    "hits": hits,
                    "relevant_total": denominator,
                    "recall_at_3": hits / denominator,
                    "mrr_at_3": statistics.mean(x["rr"] for x in answerable),
                    "no_answer_retrievals": sum(
                        x["retrieved_on_no_answer"] for x in unknown
                    ),
                    "no_answer_total": len(unknown),
                    "mean_ms": statistics.mean(x["elapsed_ms"] for x in rows),
                }
            )
    return summaries, traces


# main：解析数据集和可选真实采集文件，校验指纹后运行检索/工具评分，输出逐题 JSON 与报告。
def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=ROOT / "synthetic-v1.json")
    p.add_argument("--output", type=Path, default=ROOT / "results")
    p.add_argument(
        "--vector-rankings",
        type=Path,
        help="Optional dataset_sha256, model, rankings per query JSON",
    )
    p.add_argument(
        "--tool-predictions",
        type=Path,
        help="Optional dataset_sha256, provenance, rows JSON",
    )
    args = p.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8-sig"))
    sha = digest(args.dataset)
    semantic = None
    model = "TF-IDF cosine, Chinese bigrams/Latin words (lexical, NOT semantic)"
    if args.vector_rankings:
        supplied = json.loads(args.vector_rankings.read_text(encoding="utf-8-sig"))
        if supplied["dataset_sha256"] != sha or set(supplied["rankings"]) != {
            q["id"] for q in dataset["queries"]
        }:
            raise ValueError("Dataset hash/query IDs mismatch")
        semantic, model = supplied["rankings"], supplied["model"]
    summaries, traces = evaluate(dataset, semantic)
    tools = {
        "status": "NOT_RUN: real model predictions not supplied",
        "total": len(dataset["tools"]),
    }
    if args.tool_predictions:
        predictions = json.loads(args.tool_predictions.read_text(encoding="utf-8-sig"))
        if predictions["dataset_sha256"] != sha:
            raise ValueError("Tool dataset hash mismatch")
        tools = score_tools(dataset, predictions)
    report = {
        "dataset_sha256": sha,
        "python": platform.python_version(),
        "vector_model": model,
        "parameters": {
            "k": 3,
            "bm25_k1": 1.2,
            "bm25_b": 0.75,
            "rrf_k": 60,
            "reranker": "query-token coverage + RRF; non-neural",
        },
        "limitations": [
            "Synthetic single-document questions, not real-user effectiveness.",
            "No parameter tuning performed; dev/test separation is organizational only.",
            "Retrieval latency excludes network/model time; imported ranking timing is parsing only.",
            "Citation support, hallucination, persona and multimodal quality require actual answers and human review.",
        ],
        "rag": summaries,
        "tool_decisions": tools,
        "traces": traces,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# R3 检索消融实测",
        "",
        f"数据 SHA256：`{sha}`",
        "",
        f"向量基线：{model}。",
        "重排为本地词法覆盖率启发式；没有使用神经重排模型。无检索组为空结果。",
        "",
        "| 划分 | 模式 | Recall@3 原始命中 | MRR@3 | 无答案题仍召回 | 本地均耗时 ms |",
        "|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s['split']} | {s['mode']} | {s['hits']}/{s['relevant_total']} | {s['mrr_at_3']:.4f} | {s['no_answer_retrievals']}/{s['no_answer_total']} | {s['mean_ms']:.3f} |"
        )
    lines += [
        "",
        "不能将该结果写成真实语义 RAG / Agent 效果提升。无答案题召回表示需要拒答机制，不能等同已发生幻觉。",
        "工具决策："
        + tools["status"]
        + "。人格评分、引用支持率、真实模型 Token/费用及首 Token 延迟尚未测量。",
        "逐题输出、错误与检索耗时见 report.json；保留相同或负向结果，不为提高测试集成绩调整参数。",
        "",
    ]
    (args.output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "dataset_sha256": sha,
                "queries": len(dataset["queries"]),
            }
        )
    )


if __name__ == "__main__":
    main()
