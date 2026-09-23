"""只读检查交付PPT的备注、批注和生成工具说明，不输出客户原文。"""
import argparse
import json
import re
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

MARKERS = re.compile(r'ChatGPT|Codex|OpenAI|AI生成|AI制作|网页版生成|本地重建|内部验收|制作说明|客户方案|客户确认', re.I)

def inspect_file(path, require_empty_notes=False):
    findings = []
    note_parts = 0
    with ZipFile(path) as package:
        for name in package.namelist():
            if not name.endswith('.xml') or not name.startswith(('ppt/slides/', 'ppt/notesSlides/', 'ppt/comments/', 'docProps/')):
                continue
            root = ET.fromstring(package.read(name))
            values = [node.text or '' for node in root.iter() if node.tag.rsplit('}', 1)[-1] in ('t', 'text', 'description', 'subject', 'title', 'creator', 'lastModifiedBy')]
            text = '\n'.join(values)
            is_note = name.startswith('ppt/notesSlides/')
            if is_note:
                note_parts += 1
            if require_empty_notes and is_note and text.strip():
                findings.append({'part': name, 'issue': 'nonempty_notes'})
            if MARKERS.search(text):
                findings.append({'part': name, 'issue': 'internal_production_marker'})
    return {'file': Path(path).name, 'note_parts': note_parts, 'passed': not findings, 'findings': findings,
            'boundary': '文本扫描不识别图片内文字或软件界面按钮；仍须视觉与需求验收'}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--require-empty-notes', action='store_true')
    args = parser.parse_args()
    result = inspect_file(args.file, args.require_empty_notes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
