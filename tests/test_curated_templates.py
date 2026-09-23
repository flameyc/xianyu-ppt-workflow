"""精选页面的路径边界、过期失效、许可与用户审批测试。"""

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools.curated_templates import CuratedTemplates


class CuratedTemplatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.source_root = self.base / "sources"
        self.source_root.mkdir()
        self.deck = self.source_root / "商务.pptx"
        self.make_deck("正文内容")
        self.db = self.workspace / "inventory.sqlite"
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE scan_info(key TEXT, value TEXT)")
            connection.execute("INSERT INTO scan_info VALUES('root', ?)", (str(self.source_root),))
            connection.execute("CREATE TABLE files(file_id TEXT,relative_path TEXT,name TEXT,extension TEXT,size_bytes INTEGER)")
            connection.execute("INSERT INTO files VALUES('f1','商务.pptx','商务.pptx','.pptx',1)")
        connection.close()
        self.tool = CuratedTemplates(self.workspace, self.db)
        self.payload = {"source_relative_path": "商务.pptx", "page": 1, "category": "商务汇报",
                        "style": "蓝白", "use": "年度成果", "layout": "数据对比", "reusable_elements": ["列对齐", "数据层次"]}

    def tearDown(self):
        self.temp.cleanup()

    def make_deck(self, content):
        with zipfile.ZipFile(self.deck, "w") as package:
            package.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst></p:presentation>')
            package.writestr("ppt/_rels/presentation.xml.rels", '<Relationships><Relationship Id="rId1" Target="slides/slide8.xml"/><Relationship Id="rId2" Target="slides/slide3.xml"/></Relationships>')
            for part, text in (("slide8.xml", content), ("slide3.xml", "第二页")):
                package.writestr("ppt/slides/" + part, f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>{text}</a:t></p:sld>')

    def visual_review(self, row, **extra):
        self.tool.home.mkdir(parents=True, exist_ok=True)
        preview = self.tool.home / "preview.png"
        preview.write_bytes(b"\x89PNG\r\n\x1a\nTEST")
        return self.tool.review(row["id"], reviewer="测试检查者", note="模拟看图测试，不代表实际质量",
                                ai_quality="good", preview="preview.png", observed=True,
                                source_sha256=row["source_fingerprint"]["sha256"], page=row["page"], **extra)

    def approve(self, row):
        self.visual_review(row)
        return self.tool.review(row["id"], reviewer="测试本人", note="测试授权凭据，不是真实授权",
                                user_feedback="approved", license_status="commercial")

    def test_metadata_search_never_opens_original_ppt(self):
        with patch("tools.curated_templates.fingerprint", side_effect=AssertionError("不应取正文")):
            self.assertEqual(self.tool.candidates(["商务"], 1)["matched_files"], 1)
            self.assertEqual(self.tool.candidates(["%"], 1)["matched_files"], 0)

    def test_page_order_and_limited_incremental_text_cache(self):
        result = self.tool.inspect("商务.pptx", [1])
        self.assertEqual(result["pages"][0]["slide_part"], "ppt/slides/slide8.xml")
        self.assertEqual(result["pages"][0]["text"], "正文内容")
        self.assertEqual(len(list((self.tool.home / "text").rglob("page-*.json"))), 1)
        with patch("tools.curated_templates.read_member", wraps=__import__("tools.curated_templates", fromlist=["read_member"]).read_member) as read:
            self.tool.inspect("商务.pptx", [1])
            self.assertNotIn("ppt/slides/slide8.xml", [call.args[1] for call in read.call_args_list])
        with self.assertRaises(ValueError):
            self.tool.inspect("商务.pptx", [3])

    def test_source_and_preview_path_escape_rejected(self):
        for unsafe in ("../outside.pptx", "..\\outside.pptx", str(self.deck), "C:/outside.pptx"):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.tool.source(unsafe)
        row = self.tool.add(self.payload)
        with self.assertRaises(ValueError):
            self.tool.review(row["id"], reviewer="测试", note="测试", ai_quality="good", preview="../outside.png",
                             observed=True, source_sha256=row["source_fingerprint"]["sha256"], page=1)

    def test_source_symlink_escape_rejected_when_supported(self):
        outside = self.base / "outside.pptx"
        outside.write_bytes(b"outside")
        try:
            (self.source_root / "link.pptx").symlink_to(outside)
        except OSError:
            self.skipTest("当前系统不允许测试创建符号链接")
        with self.assertRaises(ValueError):
            self.tool.source("link.pptx")

    def test_add_ignores_claimed_approval_and_deduplicates(self):
        row = self.tool.add({**self.payload, "ai_quality": "good", "license_status": "commercial", "user_feedback": "approved"})
        self.assertEqual((row["ai_quality"], row["license_status"], row["user_feedback"]), ("pending", "unknown", "pending"))
        self.assertEqual(self.tool.add(self.payload)["id"], row["id"])
        self.assertEqual(len(self.tool.load()["pages"]), 1)

    def test_no_image_or_wrong_source_cannot_mark_seen_or_approved(self):
        row = self.tool.add(self.payload)
        for values in ({"ai_quality": "good"}, {"user_feedback": "approved"}):
            with self.assertRaises(ValueError):
                self.tool.review(row["id"], reviewer="测试", note="只有文字", **values)
        self.tool.home.mkdir(parents=True, exist_ok=True)
        (self.tool.home / "fake.png").write_text("not an image", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.tool.review(row["id"], reviewer="测试", note="错误图片", ai_quality="good", preview="fake.png",
                             observed=True, source_sha256=row["source_fingerprint"]["sha256"], page=1)
        with self.assertRaises(ValueError):
            self.tool.review(row["id"], reviewer="测试", note="错页", ai_quality="good", preview="fake.png",
                             observed=True, source_sha256="0" * 64, page=2)

    def test_visual_quality_does_not_grant_license_or_user_approval(self):
        row = self.tool.add(self.payload)
        self.visual_review(row)
        self.assertEqual(self.tool.search(approved_only=True)["results"], [])
        self.tool.review(row["id"], reviewer="测试用户", note="模拟用户认可", user_feedback="approved")
        self.assertEqual(self.tool.search(approved_only=True)["results"], [])
        self.tool.review(row["id"], reviewer="测试授权核查", note="模拟授权凭据", license_status="commercial")
        self.assertEqual(len(self.tool.search(approved_only=True)["results"]), 1)

    def test_source_change_invalidates_approval_even_with_same_size_and_time(self):
        row = self.tool.add(self.payload)
        self.approve(row)
        stamp = self.deck.stat()
        self.make_deck("改稿内容")
        self.assertEqual(self.deck.stat().st_size, stamp.st_size)
        os.utime(self.deck, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.tool.search(approved_only=True)["results"], [])
        stale = self.tool.search(include_stale=True)["results"][0]
        self.assertIn("源文件变化", stale["validity"])
        self.assertIn("仅可参考", stale["reuse_status"])
        with self.assertRaises(ValueError):
            self.tool.review(row["id"], reviewer="测试", note="旧审批", user_feedback="approved")
        refreshed = self.tool.add(self.payload)
        self.assertNotEqual(refreshed["id"], row["id"])
        self.assertEqual(refreshed["user_feedback"], "pending")

    def test_changed_or_missing_preview_invalidates_visual_approval(self):
        row = self.tool.add(self.payload)
        self.approve(row)
        (self.tool.home / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\nCHANGED")
        self.assertEqual(self.tool.search(approved_only=True)["results"], [])
        self.assertIn("预览文件变化", self.tool.search(include_stale=True)["results"][0]["validity"])
        (self.tool.home / "preview.png").unlink()
        self.assertEqual(self.tool.search()["results"], [])

    def test_rejected_record_requires_explicit_filter(self):
        row = self.tool.add(self.payload)
        self.visual_review(row)
        self.tool.review(row["id"], reviewer="测试用户", note="测试否决依据", user_feedback="rejected")
        self.assertEqual(self.tool.search()["results"], [])
        self.assertEqual(len(self.tool.search(user_feedback="rejected")["results"]), 1)

    def test_search_filters_top_n_hash_only_matching_source(self):
        self.tool.add(self.payload)
        self.tool.add({**self.payload, "page": 2, "layout": "流程"})
        result = self.tool.search(category="商务汇报", style="蓝", layout="流程", limit=1)
        self.assertEqual(result["results"][0]["page"], 2)
        self.assertEqual(result["source_files_hashed"], 1)
        with patch("tools.curated_templates.fingerprint", side_effect=AssertionError("不应检查无关文件")):
            self.assertEqual(self.tool.search(category="小学竞选")["results"], [])

    def test_gallery_is_local_escaped_and_distinguishes_missing_preview(self):
        row = self.tool.add({**self.payload, "layout": '<script src="https://example.test"></script>'})
        paths = self.tool.gallery()
        text = Path(paths["html"]).read_text(encoding="utf-8")
        self.assertIn("没有可用的已核对预览", text)
        self.assertNotIn("<script", text)
        self.assertIn("default-src 'none'", text)
        self.assertNotIn("<img", text)
        self.visual_review(row)
        self.tool.gallery()
        text = Path(paths["html"]).read_text(encoding="utf-8")
        self.assertIn('src="preview.png"', text)
        self.assertIn("未知，仅可参考", text)


if __name__ == "__main__":
    unittest.main()
