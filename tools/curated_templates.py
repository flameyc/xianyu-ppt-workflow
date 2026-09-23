"""精选页面的本地索引、增量取文与检索；原素材库始终只读。"""

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import sqlite3
import sys
from urllib.parse import quote
import xml.etree.ElementTree as ET
import zipfile


DEFAULT_DATABASE = "template_inventory.sqlite"
AI_STATES = {"pending": "未看图", "good": "AI初筛优质", "candidate": "已看图候选", "rejected": "不推荐"}
FEEDBACK_STATES = {"pending": "待校准", "approved": "用户认可", "rejected": "用户否决"}
LICENSE_STATES = {"unknown": "未知，仅可参考", "commercial": "已核实商用许可", "restricted": "限制使用"}
NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def fingerprint(path):
    before = path.stat()
    sha = digest(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("文件在校验时变化，请稳定后重试")
    return {"sha256": sha, "size_bytes": after.st_size, "modified_ns": after.st_mtime_ns}


def within(root, value, *, must_exist=True):
    """拒绝跨目录路径、绝对路径和经符号链接逃出的文件。"""
    value = str(value).replace("\\", "/")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or ":" in value:
        raise ValueError("只允许根目录内的相对路径")
    target = (root / Path(*pure.parts)).resolve(strict=must_exist)
    if target == root or root not in target.parents:
        raise ValueError("路径超出允许的本地目录")
    return target


def read_member(package, name):
    info = package.getinfo(name)
    if info.file_size > 16 * 1024 * 1024:
        raise ValueError("单个PPT XML超过16MiB，只读检查停止")
    return package.read(name)


def slide_parts(package):
    """按演示文稿顺序取页码，不能把 slide 文件名当作展示页码。"""
    presentation = ET.fromstring(read_member(package, "ppt/presentation.xml"))
    rels = ET.fromstring(read_member(package, "ppt/_rels/presentation.xml.rels"))
    targets = {node.attrib["Id"]: node.attrib.get("Target", "")
               for node in rels if node.attrib.get("TargetMode") != "External"}
    result = []
    for node in presentation.findall("p:sldIdLst/p:sldId", NS):
        target = targets[node.attrib["{" + NS["r"] + "}id"]].replace("\\", "/")
        if ".." in PurePosixPath(target).parts:
            raise ValueError("PPT内部页路径异常")
        part = target.lstrip("/") if target.startswith("/") else "ppt/" + target
        if not part.startswith("ppt/slides/") or not part.endswith(".xml"):
            raise ValueError("PPT内部页路径异常")
        result.append(part)
    return result


class CuratedTemplates:
    def __init__(self, workspace, database=None):
        self.workspace = Path(workspace).resolve()
        self.home = (self.workspace / ".private" / "curation").resolve()
        if self.workspace not in self.home.parents:
            raise ValueError("精选库必须留在工作区.private内")
        db = Path(database or DEFAULT_DATABASE)
        self.database = (self.workspace / db).resolve() if not db.is_absolute() else db.resolve()
        with self.connect() as conn:
            self.source_root = Path(conn.execute("SELECT value FROM scan_info WHERE key='root'").fetchone()[0]).resolve(strict=True)
        if self.source_root == self.home or self.source_root in self.home.parents or self.home in self.source_root.parents:
            raise ValueError("精选输出目录不能与原素材根重叠")
        self.catalog_path = self.home / "catalog.json"

    def connect(self):
        # sqlite连接的with只结束事务，不会关闭Windows文件句柄。
        return closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True))

    def load(self):
        if not self.catalog_path.exists():
            return {"schema_version": 1, "source_root": str(self.source_root), "pages": []}
        data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or Path(data["source_root"]).resolve() != self.source_root:
            raise ValueError("精选索引版本或原素材根发生变化，不能沿用审批")
        return data

    def save(self, data):
        self.home.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = now()
        temporary = self.catalog_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.catalog_path)

    def source(self, relative):
        return within(self.source_root, relative)

    def candidates(self, keywords, limit=10):
        if not 1 <= limit <= 20 or not keywords or len(keywords) > 10:
            raise ValueError("提供1–10个关键词，返回数限1–20")
        words = list(dict.fromkeys(word.strip().casefold() for word in keywords if word.strip()))
        if not words:
            raise ValueError("关键词不可为空")
        # 查询既有元数据，没有遍历原库、打开PPT或更新盘点数据库。
        clauses = ["instr(lower(relative_path), ?) > 0" for _ in words]
        with self.connect() as conn:
            rows = conn.execute("SELECT file_id, relative_path, name, size_bytes FROM files "
                                "WHERE extension='.pptx' AND (" + " OR ".join(clauses) + ")",
                                words).fetchall()
        ranked = sorted(rows, key=lambda row: (-sum(3 if w in row[2].casefold() else 1
                                                   for w in words if w in row[1].casefold()), row[1]))
        results = [{"file_id": row[0], "relative_path": row[1], "size_bytes": row[3]} for row in ranked[:limit]]
        return {"matched_files": len(rows), "results": results,
                "scope": "仅查询已有SQLite命名元数据；未看图，不代表风格或用途匹配"}

    def inspect(self, relative, pages):
        """只取指定1–20页文字；缓存绑定整个源文件内容哈希。"""
        if not pages or len(pages) > 20 or any(type(p) is not int or p < 1 for p in pages):
            raise ValueError("只允许指定1–20个正整数页码")
        source = self.source(relative)
        if source.suffix.lower() != ".pptx":
            raise ValueError("首版仅支持PPTX，旧PPT需人工打开核查")
        fp = fingerprint(source)
        cache_dir = within(self.home, "text/" + fp["sha256"], must_exist=False)
        cache_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as package:
            parts = slide_parts(package)
            if max(pages) > len(parts):
                raise ValueError("页码超过原文件页数")
            result = []
            for page in sorted(set(pages)):
                cache = cache_dir / f"page-{page}.json"
                if cache.exists():
                    item = json.loads(cache.read_text(encoding="utf-8"))
                else:
                    tree = ET.fromstring(read_member(package, parts[page - 1]))
                    text = "\n".join(node.text or "" for node in tree.findall(".//a:t", NS))
                    item = {"page": page, "slide_part": parts[page - 1], "text": text}
                    cache.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
                result.append(item)
        if source.stat().st_mtime_ns != fp["modified_ns"] or source.stat().st_size != fp["size_bytes"]:
            raise ValueError("读取期间源文件变化，检查结果作废")
        return {"source_relative_path": str(source.relative_to(self.source_root)), "fingerprint": fp,
                "slide_count": len(parts), "pages": result, "visual_status": "未看图"}

    def add(self, payload):
        inspected = self.inspect(payload["source_relative_path"], [payload["page"]])
        for field in ("category", "style", "use", "layout", "reusable_elements"):
            if not payload.get(field) or not isinstance(payload[field], (str, list)):
                raise ValueError(f"缺少页面标签：{field}")
        identity = inspected["source_relative_path"] + "|" + str(payload["page"]) + "|" + inspected["fingerprint"]["sha256"]
        page_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        data = self.load()
        existing = next((row for row in data["pages"] if row["id"] == page_id), None)
        if existing:
            return existing
        row = {"id": page_id, "source_relative_path": inspected["source_relative_path"],
               "source_file": str(self.source(inspected["source_relative_path"])), "page": payload["page"],
               "source_fingerprint": inspected["fingerprint"], "slide_part": inspected["pages"][0]["slide_part"],
               "text_excerpt": inspected["pages"][0]["text"][:1200],
               **{field: payload[field] for field in ("category", "style", "use", "layout", "reusable_elements")},
               "license_status": "unknown", "license_evidence": "", "user_feedback": "pending",
               "user_feedback_evidence": "", "ai_quality": "pending", "visual_evidence": None,
               "created_at": now(), "history": []}
        data["pages"].append(row)
        self.save(data)
        return row

    def validity(self, row, hashes=None):
        hashes = {} if hashes is None else hashes
        try:
            source = self.source(row["source_relative_path"])
            key = str(source)
            if key not in hashes:
                hashes[key] = fingerprint(source)
            if hashes[key] != row["source_fingerprint"]:
                return "源文件变化，旧预览与审批失效"
            evidence = row.get("visual_evidence")
            if evidence:
                preview = within(self.home, evidence["preview"])
                if evidence["source_sha256"] != hashes[key]["sha256"] or evidence["page"] != row["page"]:
                    return "预览来源与页面不匹配"
                if digest(preview) != evidence["preview_sha256"]:
                    return "预览文件变化，视觉判断失效"
        except (OSError, ValueError, KeyError) as exc:
            return "文件或来源证据不可用：" + str(exc)
        return "current"

    def review(self, page_id, *, reviewer, note, ai_quality=None, preview=None,
               observed=False, source_sha256=None, page=None, license_status=None, user_feedback=None):
        if not reviewer.strip() or not note.strip():
            raise ValueError("审核需有执行者与具体依据，不能空填通过")
        data = self.load()
        row = next((row for row in data["pages"] if row["id"] == page_id), None)
        if row is None:
            raise ValueError("找不到精选页面")
        validity = self.validity(row)
        if validity != "current":
            raise ValueError("记录已失效，请重新登记源文件：" + validity)
        if ai_quality is not None:
            if ai_quality not in AI_STATES:
                raise ValueError("未知视觉状态")
            if ai_quality != "pending":
                if not observed or not preview or source_sha256 != row["source_fingerprint"]["sha256"] or page != row["page"]:
                    raise ValueError("视觉判断需要实际看图声明、预览及准确源哈希和页码")
                target = within(self.home, preview)
                if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise ValueError("只接收本地PNG/JPEG/WebP页面渲染")
                with target.open("rb") as stream:
                    signature = stream.read(12)
                if not (signature.startswith(b"\x89PNG\r\n\x1a\n") or signature.startswith(b"\xff\xd8\xff")
                        or (signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")):
                    raise ValueError("预览缺少有效图片签名")
                row["visual_evidence"] = {"kind": "page_render", "preview": str(target.relative_to(self.home)),
                                          "preview_sha256": digest(target), "source_sha256": source_sha256,
                                          "page": page, "observed": True, "reviewer": reviewer,
                                          "note": note, "reviewed_at": now()}
            else:
                row["visual_evidence"] = None
            row["ai_quality"] = ai_quality
        if license_status is not None:
            if license_status not in LICENSE_STATES:
                raise ValueError("未知许可状态")
            row["license_status"] = license_status
            row["license_evidence"] = note
        if user_feedback is not None:
            if user_feedback not in FEEDBACK_STATES:
                raise ValueError("未知用户反馈状态")
            if user_feedback == "approved" and not row["visual_evidence"]:
                raise ValueError("用户认可需绑定已核对的页面预览")
            row["user_feedback"] = user_feedback
            row["user_feedback_evidence"] = note
        row["history"].append({"at": now(), "reviewer": reviewer, "note": note, "ai_quality": ai_quality,
                               "license_status": license_status, "user_feedback": user_feedback})
        self.save(data)
        return row

    def search(self, *, query="", category=None, style=None, use=None, layout=None,
               ai_quality=None, user_feedback=None, license_status=None, limit=5, include_stale=False,
               approved_only=False):
        if not 1 <= limit <= 50:
            raise ValueError("返回数量应在1–50之间")
        rows = self.load()["pages"]
        matched = []
        for row in rows:
            if any(value is not None and row.get(field) != value for field, value in
                   (("category", category), ("ai_quality", ai_quality), ("user_feedback", user_feedback), ("license_status", license_status))):
                continue
            if any(value and value.casefold() not in json.dumps(row[field], ensure_ascii=False).casefold()
                   for field, value in (("style", style), ("use", use), ("layout", layout))):
                continue
            text = json.dumps({k: row[k] for k in ("category", "style", "use", "layout", "reusable_elements", "text_excerpt")}, ensure_ascii=False).casefold()
            if any(word.casefold() not in text for word in query.split()):
                continue
            if row["user_feedback"] == "rejected" or row["ai_quality"] == "rejected":
                if user_feedback != "rejected" and ai_quality != "rejected":
                    continue
            matched.append(row)
        matched.sort(key=lambda row: (row["user_feedback"] != "approved", row["ai_quality"] != "good", row["id"]))
        hashes, results, invalid = {}, [], 0
        for row in matched:
            validity = self.validity(row, hashes)
            current = validity == "current"
            if not current:
                invalid += 1
                if not include_stale:
                    continue
            allowed = (current and bool(row.get("visual_evidence")) and row["ai_quality"] in {"good", "candidate"}
                       and row["license_status"] == "commercial" and bool(row.get("license_evidence"))
                       and row["user_feedback"] == "approved" and bool(row.get("user_feedback_evidence")))
            if approved_only and not allowed:
                continue
            results.append({**row, "validity": validity, "reuse_status": "可进入订单适配核对" if allowed else "仅可参考，不可直接用于客户交付"})
            if len(results) >= limit:
                break
        return {"results": results, "matched_records": len(matched), "invalid_checked": invalid,
                "source_files_hashed": len(hashes), "note": "仅校验符合条件的已收录源文件；未扫描原素材目录。审批不替代本订单的需求和内容验收。"}

    def gallery(self, **filters):
        result = self.search(**filters)
        self.home.mkdir(parents=True, exist_ok=True)
        cards, lines = [], ["# 精选页面本地预览", "", "许可、用户校准与AI初筛分别记录；未知许可仅可参考。", ""]
        for row in result["results"]:
            title = f'{row["category"]} · 第{row["page"]}页 · {row["layout"]}'
            evidence = row.get("visual_evidence")
            picture = "<p>没有可用的已核对预览</p>"
            if evidence and row["validity"] == "current":
                uri = quote(evidence["preview"].replace("\\", "/"), safe="/")
                picture = f'<img src="{uri}" alt="{html.escape(title, quote=True)}">'
                lines.extend([f'![{title}]({uri})', ""])
            detail = (f'{AI_STATES[row["ai_quality"]]} / {FEEDBACK_STATES[row["user_feedback"]]} / '
                      f'{LICENSE_STATES[row["license_status"]]}\n风格：{row["style"]}\n用途：{row["use"]}\n'
                      f'可参考元素：{row["reusable_elements"]}\n来源：{row["source_file"]}\n'
                      f'源哈希：{row["source_fingerprint"]["sha256"]}\n记录：{row["id"]}\n'
                      f'有效性：{row["validity"]}\n{row["reuse_status"]}')
            if evidence:
                detail += "\n视觉观察：" + evidence["note"]
            cards.append(f'<article><h2>{html.escape(title)}</h2>{picture}<pre>{html.escape(detail)}</pre></article>')
            lines.extend([f"## {title}", "", detail, ""])
        document = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' file:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
                    '<title>精选页面本地预览</title><style>body{max-width:1100px;margin:36px auto;padding:0 24px;font:16px/1.7 sans-serif;background:#f7f7f5;color:#20242a}'
                    'article{margin:48px 0}img{width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 sans-serif}h2{font-size:23px}</style>'
                    '<h1>精选页面本地预览</h1><p>图片与记录均在本机。未知许可仅可参考；AI初筛不等于用户认可。</p>' + "".join(cards) + '</html>')
        (self.home / "预览.html").write_text(document, encoding="utf-8")
        (self.home / "精选页面.md").write_text("\n".join(lines), encoding="utf-8")
        return {"html": str(self.home / "预览.html"), "markdown": str(self.home / "精选页面.md"), "pages": len(result["results"])}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--database")
    subs = parser.add_subparsers(dest="command", required=True)
    candidates = subs.add_parser("candidates")
    candidates.add_argument("--keywords", nargs="+", required=True)
    candidates.add_argument("--limit", type=int, default=10)
    inspect = subs.add_parser("inspect")
    inspect.add_argument("--source", required=True)
    inspect.add_argument("--pages", nargs="+", type=int, required=True)
    add = subs.add_parser("add")
    add.add_argument("--input", required=True, help="本地JSON，含一个页面对象或页面对象数组")
    review = subs.add_parser("review")
    review.add_argument("--id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--note", required=True)
    review.add_argument("--ai-quality", choices=AI_STATES)
    review.add_argument("--preview", help="相对于.private/curation的真实页面渲染")
    review.add_argument("--observed", action="store_true", help="仅实际看过这张图之后使用")
    review.add_argument("--source-sha256")
    review.add_argument("--page", type=int)
    review.add_argument("--license-status", choices=LICENSE_STATES)
    review.add_argument("--user-feedback", choices=FEEDBACK_STATES)
    for command in ("search", "gallery"):
        search = subs.add_parser(command)
        for field in ("query", "category", "style", "use", "layout"):
            search.add_argument("--" + field)
        search.add_argument("--ai-quality", choices=AI_STATES)
        search.add_argument("--user-feedback", choices=FEEDBACK_STATES)
        search.add_argument("--license-status", choices=LICENSE_STATES)
        search.add_argument("--limit", type=int, default=5 if command == "search" else 50)
        search.add_argument("--include-stale", action="store_true")
        search.add_argument("--approved-only", action="store_true")
    args = vars(parser.parse_args())
    tool = CuratedTemplates(args.pop("workspace"), args.pop("database"))
    command = args.pop("command")
    try:
        if command == "add":
            payload = json.loads(Path(args["input"]).read_text(encoding="utf-8-sig"))
            result = [tool.add(row) for row in payload] if isinstance(payload, list) else tool.add(payload)
        elif command == "inspect":
            result = tool.inspect(args["source"], args["pages"])
        elif command == "review":
            result = tool.review(args.pop("id"), **args)
        else:
            args = {key: value for key, value in args.items() if value is not None}
            result = getattr(tool, command)(**args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
