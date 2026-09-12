"""查询本地模板元数据索引；只返回限量路径，不读取 PPT 正文。"""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import sys
import time


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--keywords", nargs="+", default=[])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--overview", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("返回数量应在 1–20 之间")
    started = time.perf_counter()
    path = Path(args.database).resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT file_id,relative_path,name,extension,size_bytes,top_folder FROM files WHERE extension IN ('.pptx','.ppt') ORDER BY relative_path").fetchall()
        root = conn.execute("SELECT value FROM scan_info WHERE key='root'").fetchone()[0]
    if args.overview:
        groups = defaultdict(lambda: {"count": 0, "size_bytes": 0, "pptx": 0, "ppt": 0, "examples": []})
        for file_id, relative, name, extension, size, top in rows:
            group = groups[top]
            group["count"] += 1
            group["size_bytes"] += size
            group[extension[1:]] += 1
            if len(group["examples"]) < 3:
                group["examples"].append(relative)
        for group in groups.values():
            group["GiB"] = round(group["size_bytes"] / 2**30, 3)
        print(json.dumps({"groups": groups, "ppt_files_total": len(rows)}, ensure_ascii=False, indent=2))
        return
    if not args.keywords:
        parser.error("请提供 --keywords 或 --overview")
    keywords = list(dict.fromkeys(word.casefold().strip() for word in args.keywords if word.strip()))
    matches, seen, candidate_counts = [], set(), {word: 0 for word in keywords}
    for file_id, relative, name, extension, size, top in rows:
        matched = [word for word in keywords if word in relative.casefold()]
        for word in matched:
            candidate_counts[word] += 1
        if not matched:
            continue
        # 优先多词匹配和精选目录；这是命名启发式，不代表视觉质量评分。
        score = len(matched) * 10 + sum(word in name.casefold() for word in matched) * 2
        score += 3 if top != "创赛作品合集" else 0
        score += 1 if extension == ".pptx" else 0
        matches.append((score, file_id, relative, name, size, matched))
    matches.sort(key=lambda item: (-item[0], item[2]))
    results = []
    for score, file_id, relative, name, size, matched in matches:
        # 展示时折叠同名同大小候选，不改写索引，不声明已确认重复。
        signature = (name.casefold(), size)
        if signature in seen:
            continue
        seen.add(signature)
        results.append({"file_id": file_id, "relative_path": relative, "size_MiB": round(size / 2**20, 2), "matched_keywords": matched})
        if len(results) == args.limit:
            break
    print(json.dumps({"root": root, "keyword_counts": candidate_counts, "matched_files": len(matches),
                      "elapsed_ms": round((time.perf_counter()-started)*1000, 2), "results": results,
                      "note": "匹配目录和文件名；未验证页面风格、内容质量与可编辑性。"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
