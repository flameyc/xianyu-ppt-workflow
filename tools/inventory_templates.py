"""只读扫描模板目录，将文件元数据保存到项目数据库；不读取文件正文。"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve(strict=True)
    output = Path(args.output).resolve()
    if not root.is_dir() or output == root or root in output.parents:
        raise SystemExit("输出目录必须在待扫描目录之外，扫描根必须是文件夹。")
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    rows, errors, skipped = [], [], []
    directories = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        stat = entry.stat(follow_symlinks=False)
                        if entry.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                            skipped.append(str(Path(entry.path).relative_to(root)))
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            directories += 1
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            relative = str(Path(entry.path).relative_to(root))
                            # 标识仅取自相对路径，不是文件内容指纹，不能用于确证重复。
                            file_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
                            rows.append((file_id, relative, entry.name, Path(entry.name).suffix.lower(),
                                         stat.st_size, stat.st_mtime_ns, relative.split(os.sep)[0]))
                    except OSError as exc:
                        errors.append({"path": entry.path, "error": str(exc)})
        except OSError as exc:
            errors.append({"path": str(directory), "error": str(exc)})
    rows.sort(key=lambda row: row[1].casefold())
    db_path = output / "template_inventory.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS files (file_id TEXT PRIMARY KEY, relative_path TEXT UNIQUE, name TEXT, extension TEXT, size_bytes INTEGER, modified_ns INTEGER, top_folder TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS scan_info (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("DELETE FROM files")
        conn.executemany("INSERT INTO files VALUES (?,?,?,?,?,?,?)", rows)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size_bytes)")
        conn.executemany("INSERT OR REPLACE INTO scan_info VALUES (?,?)", [
            ("root", str(root)), ("scanned_at", datetime.now().astimezone().isoformat()),
            ("scan_mode", "metadata_only_no_content_hash"),
        ])
    ext_count, ext_size, top_count, top_size = Counter(), Counter(), Counter(), Counter()
    duplicate_candidates = defaultdict(list)
    samples = defaultdict(list)
    for _, relative, name, extension, size, _, top in rows:
        ext_count[extension] += 1
        ext_size[extension] += size
        top_count[top] += 1
        top_size[top] += size
        duplicate_candidates[(name.casefold(), size)].append(relative)
        if len(samples[extension]) < 4:
            samples[extension].append(relative)
    groups = [(key, values) for key, values in duplicate_candidates.items() if len(values) > 1]
    groups.sort(key=lambda pair: pair[0][1] * (len(pair[1]) - 1), reverse=True)
    total = sum(ext_size.values())
    summary = {
        "root": str(root), "scanned_at": datetime.now().astimezone().isoformat(),
        "scan_mode": "只读文件元数据；未读正文、未解压、未渲染、未计算内容哈希",
        "file_count": len(rows), "subdirectory_count": directories,
        "size_bytes": total, "decimal_GB": round(total / 10**9, 3),
        "binary_GiB": round(total / 2**30, 3),
        "extensions": [{"extension": key or "无扩展名", "count": count, "GiB": round(ext_size[key] / 2**30, 3)} for key, count in ext_count.most_common()],
        "top_folders": [{"name": key, "count": top_count[key], "GiB": round(top_size[key] / 2**30, 3)} for key in sorted(top_count, key=lambda key: top_size[key], reverse=True)],
        "same_name_size_candidate_groups": len(groups),
        "same_name_size_candidate_extra_files": sum(len(values)-1 for _, values in groups),
        "same_name_size_candidate_extra_GiB": round(sum(key[1]*(len(values)-1) for key, values in groups) / 2**30, 3),
        "candidate_note": "同名同大小仅为疑似重复，未计算内容哈希，不能据此删除或确认可节省空间。",
        "samples_by_extension": dict(samples),
        "largest_files": [{"path": row[1], "GiB": round(row[4] / 2**30, 3)} for row in sorted(rows, key=lambda row: row[4], reverse=True)[:8]],
        "errors": errors, "skipped_links": skipped,
        "elapsed_seconds": round(time.perf_counter()-started, 3),
        "database": str(db_path),
    }
    (output / "template_inventory_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "duplicate_candidates.json").write_text(json.dumps([
        {"name": key[0], "size_bytes": key[1], "paths": values} for key, values in groups
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    # 全量清单留在本地，只向调用者返回紧凑统计，避免文件名占满模型上下文。
    compact = {key: summary[key] for key in (
        "root", "file_count", "subdirectory_count", "decimal_GB", "binary_GiB",
        "same_name_size_candidate_groups", "same_name_size_candidate_extra_files",
        "elapsed_seconds", "database")}
    compact["errors_count"] = len(errors)
    compact["skipped_links_count"] = len(skipped)
    compact["top_extensions"] = summary["extensions"][:10]
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
