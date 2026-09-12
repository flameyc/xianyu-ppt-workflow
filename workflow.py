"""本地PPT制作工作流：订单、资料快照、局部制作、审核、交付和经验记录。"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
NS = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def within(path, base):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(base).resolve()):
        raise ValueError('路径不在指定订单目录内')
    return path


def ppt_info(path):
    """本地XML解析，不调用模型；按真实展示顺序而非压缩包文件名排序。"""
    with ZipFile(path) as z:
        pres = ET.fromstring(z.read('ppt/presentation.xml'))
        rels = ET.fromstring(z.read('ppt/_rels/presentation.xml.rels'))
        mapping = {x.get('Id'): x.get('Target') for x in rels}
        size = pres.find('p:sldSz', NS)
        slides, fonts = [], set()
        for i, item in enumerate(pres.find('p:sldIdLst', NS), 1):
            target = mapping[item.get('{'+NS['r']+'}id')]
            name = target.lstrip('/') if target.startswith('/') else 'ppt/' + target
            tree = ET.fromstring(z.read(name))
            text = '\n'.join(t.text or '' for t in tree.findall('.//a:t', NS))
            slides.append({'slide': i, 'part': name, 'text': text})
            for tag in ('latin', 'ea', 'cs'):
                for font in tree.findall('.//a:' + tag, NS):
                    family = font.get('typeface', '')
                    if family and not family.startswith('+'):
                        fonts.add(family)
        return {'slide_count': len(slides), 'slides': slides,
                'slide_size_emu': size.get('cx') + ',' + size.get('cy'), 'fonts': sorted(fonts)}


class Workflow:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        (self.root / 'orders').mkdir(parents=True, exist_ok=True)

    def order_dir(self, order_id):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{1,79}', order_id):
            raise ValueError('订单编号只能包含字母、数字、横线和下划线，长度2—80')
        return within(self.root / 'orders' / order_id, self.root / 'orders')

    def load(self, order_id):
        return read_json(self.order_dir(order_id) / 'workflow.json')

    @contextmanager
    def edit(self, order_id, create=False):
        folder = self.order_dir(order_id)
        folder.mkdir(parents=True, exist_ok=True)
        lock = folder / '.workflow.lock'
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ValueError('该订单正在操作；如上次意外退出，请先核实进程再处理锁文件')
        try:
            os.write(fd, f'{os.getpid()} {now()}'.encode())
            os.close(fd)
            path = folder / 'workflow.json'
            if create and path.exists():
                raise ValueError('订单已存在，未覆盖')
            state = {} if create else read_json(path)
            yield state
            state['updated_at'] = now()
            atomic_json(path, state)
        finally:
            lock.unlink(missing_ok=True)

    def event(self, state, kind, **data):
        state.setdefault('events', []).append({'id': uuid.uuid4().hex, 'at': now(), 'kind': kind, **data})

    def create(self, order_id, brief, mode='live'):
        if mode not in ('live', 'demo', 'history'):
            raise ValueError('未知订单模式')
        if not isinstance(brief, dict) or not str(brief.get('title', '')).strip():
            raise ValueError('需求必须包含title')
        with self.edit(order_id, create=True) as s:
            s.update(schema_version=1, order_id=order_id, mode=mode, brief=brief,
                     created_at=now(), status='待整理', sources=[], facts={}, gaps=[],
                     versions=[], changes=[], deliveries=[], payments=[], costs=[], work_logs=[],
                     learnings=[], reviews=[], fact_revision=0, reconciled=True,
                     finance={'agreed_total': None, 'evidence': None}, events=[])
            self.event(s, 'order_created', mode=mode)
        return self.status(order_id)

    def import_history(self, order_id):
        legacy = read_json(self.order_dir(order_id) / 'brief.json')
        self.create(order_id, {'title': legacy['title'], 'purpose': legacy['requirements']['purpose'],
                              'content_responsibility': legacy['requirements']['content_responsibility'],
                              'source': 'brief.json'}, 'history')
        with self.edit(order_id) as s:
            c = legacy['commercial']
            s['status'] = '历史制作完成'
            s['finance'] = {'agreed_total': c['final_total_confirmed'], 'evidence': c['confirmation_source']}
            s['payments'] = [{'id': 'historical-deposit', 'amount': c['actual_total_received'],
                              'evidence': c['confirmation_source'], 'at': now(), 'backfilled': True}]
            self.event(s, 'history_imported', note='保留原始案例文件，未虚构制作验证与实际发送记录')
        return self.status(order_id)

    def add_source(self, order_id, source, role='content'):
        source = Path(source).resolve(strict=True)
        if not source.is_file() or role not in ('template', 'content', 'image', 'feedback'):
            raise ValueError('资料必须为文件，角色为template/content/image/feedback')
        checksum = digest(source)
        with self.edit(order_id) as s:
            existing = next((x for x in s['sources'] if x['sha256'] == checksum and x['role'] == role), None)
            if existing:
                return existing
            folder = self.order_dir(order_id)
            sid = 'S' + uuid.uuid4().hex[:10]
            dest = folder / 'inputs' / (sid + source.suffix.lower())
            dest.parent.mkdir(exist_ok=True)
            shutil.copy2(source, dest)
            if digest(dest) != checksum:
                raise ValueError('复制期间源文件改变，请重试')
            cache = self.root / 'cache' / 'text' / (checksum + '.json')
            if not cache.exists():
                if source.suffix.lower() == '.pptx':
                    extraction = {'kind': 'pptx', **ppt_info(dest)}
                elif source.suffix.lower() in ('.md', '.txt', '.csv', '.json'):
                    extraction = {'kind': 'text', 'text': dest.read_text(encoding='utf-8-sig', errors='replace')}
                elif source.suffix.lower() == '.docx':
                    with ZipFile(dest) as z:
                        tree = ET.fromstring(z.read('word/document.xml'))
                    extraction = {'kind': 'text', 'text': '\n'.join(x.text or '' for x in tree.iter() if x.tag.endswith('}t'))}
                else:
                    extraction = {'kind': 'pending', 'note': '已归档；PDF、旧PPT、图片及复杂表格由当前任务按对应技能提取'}
                atomic_json(cache, extraction)
            item = {'id': sid, 'role': role, 'name': source.name, 'original_path': str(source),
                    'path': str(dest.relative_to(folder)), 'sha256': checksum, 'cache': str(cache), 'at': now()}
            s['sources'].append(item)
            s['reconciled'] = False
            self.event(s, 'source_added', source_id=sid, role=role)
        return item

    def reconcile(self, order_id, payload):
        """由当前AI任务依据原资料整理，禁止把提取建议自动当客户事实。"""
        if not payload.get('evidence'):
            raise ValueError('资料核对需要来源说明')
        with self.edit(order_id) as s:
            changed = []
            previous_keys = set(s['facts'])
            for key, item in payload.get('facts', {}).items():
                if not isinstance(item, dict) or 'value' not in item or not item.get('source'):
                    raise ValueError('每项事实必须含value和source')
                if s['facts'].get(key) != item:
                    changed.append(key)
                    s['facts'][key] = item
            if changed:
                s['fact_revision'] += 1
                if s.get('production_route'):
                    s['production_route']['needs_recheck'] = True
            for g in payload.get('gaps', []):
                if not g.get('id') or not g.get('question'):
                    raise ValueError('缺项必须包含id和question')
                if g.get('status', 'open') not in ('open', 'resolved', 'not_applicable'):
                    raise ValueError('缺项状态只能为open/resolved/not_applicable')
                if g.get('status', 'open') != 'open' and not g.get('evidence'):
                    raise ValueError('解决或排除缺项必须记录依据')
            if 'gaps' in payload:
                s['gaps'] = payload['gaps']
            s['reconciled'] = True
            impacted = []
            for version in s['versions']:
                keys = set(version.get('fact_keys', []))
                if changed and (not keys or keys.intersection(changed) or set(changed) - previous_keys):
                    version['needs_review'] = True
                    impacted.append(version['id'])
            self.event(s, 'sources_reconciled', evidence=payload['evidence'], changed_facts=changed, impacted_versions=impacted)
        self.packet(order_id)
        return {'changed_facts': changed, 'impacted_versions': impacted}

    def reference(self, s, ref):
        folder = self.order_dir(s['order_id'])
        for item in s['sources'] + s['versions']:
            if item['id'] == ref:
                path = within(folder / item['path'], folder)
                if digest(path) != item['sha256']:
                    raise ValueError('资料或版本文件已被外部修改，需重新登记，不能沿用旧确认')
                return path, item
        raise ValueError('未找到资料或版本编号')

    def runtime(self):
        config = read_json(self.root / 'config' / 'runtime.json')
        for key in ('python', 'node', 'node_modules', 'skill_dir'):
            if not Path(config[key]).exists():
                raise ValueError('运行时路径失效：' + key + '；请重新加载工作区依赖')
        return config

    def run_adapter(self, mode, job):
        job_path = Path(job['build_dir']) / 'job.json'
        atomic_json(job_path, job)
        env = {**os.environ, 'RUNTIME_NODE_MODULES': job['runtime']['node_modules'], 'PYTHONUTF8': '1'}
        if mode == 'build':
            marker = subprocess.run([job['runtime']['node'], 'container_tools/mark_artifact_operation_started.mjs',
                                     '--operation-kind', 'edit', '--expected-output-count', '1', '--output-format', 'pptx'],
                                    cwd=job['runtime']['skill_dir'], env=env, capture_output=True, timeout=60)
            if marker.returncode:
                raise ValueError('制作入口初始化失败：' + marker.stderr.decode('utf-8', errors='replace')[-800:])
        result = subprocess.run([job['runtime']['node'], str(self.root / 'adapters' / 'pptx.mjs'), mode, str(job_path)],
                                cwd=self.root, env=env, capture_output=True, timeout=600)
        (Path(job['build_dir']) / 'adapter.log').write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise ValueError('PPT工具执行失败，日志：' + str(Path(job['build_dir']) / 'adapter.log'))

    def inspect(self, order_id, ref):
        with self.edit(order_id) as s:
            source, item = self.reference(s, ref)
            if source.suffix.lower() != '.pptx':
                raise ValueError('首版制作适配器只支持PPTX')
            build = self.order_dir(order_id) / 'inspections' / item['sha256']
            if not (build / 'complete.json').exists():
                job = {'source': str(source), 'source_sha256': item['sha256'], 'build_dir': str(build), 'runtime': self.runtime()}
                self.run_adapter('inspect', job)
                atomic_json(build / 'complete.json', {'sha256':item['sha256'], 'completed_at':now()})
            self.event(s, 'pptx_inspected', reference=ref)
        return {'inspection': str(build / 'inspection.ndjson'), 'previews': str(build / 'previews')}

    def build(self, order_id, plan):
        started = time.monotonic()
        with self.edit(order_id) as s:
            if not s['reconciled']:
                raise ValueError('有新资料尚未核对，先执行reconcile生成缺项和影响清单')
            if not plan.get('purpose') or not plan.get('changes'):
                raise ValueError('制作计划必须包含purpose和changes')
            source, base = self.reference(s, plan['base_ref'])
            if plan.get('base_sha256') != base['sha256']:
                raise ValueError('制作计划未锁定正确的基准指纹')
            if plan.get('fact_revision') != s['fact_revision']:
                raise ValueError('事实版本已变化，需要重新核对制作计划')
            folder = self.order_dir(order_id)
            run_id = uuid.uuid4().hex[:10]
            vid = f'v{len(s["versions"])+1:03d}-{run_id}'
            build = folder / 'build' / run_id
            output = folder / 'versions' / vid / 'deck.pptx'
            output.parent.mkdir(parents=True)
            info = ppt_info(source)
            expected = s['brief'].get('expected_slides')
            if expected is not None and expected != info['slide_count']:
                raise ValueError('需求页数与基准PPT不一致；此适配器保留页数，需要新布局时走制作后register流程')
            font_reference, font_item = self.reference(s, plan.get('font_reference', plan['base_ref']))
            job = {'runtime': self.runtime(), 'order_dir': str(folder), 'source': str(source),
                   'source_sha256': base['sha256'], 'build_dir': str(build), 'output': str(output),
                   'changes': plan['changes'], 'fonts': info['fonts'],
                   'font_reference': str(font_reference), 'font_reference_sha256': font_item['sha256'],
                   'slide_count': info['slide_count'], 'slide_size_emu': info['slide_size_emu']}
            atomic_json(build / 'plan.json', plan)
            self.run_adapter('build', job)
            final_info = ppt_info(output)
            # 替换不仅需要函数无报错，还要核对导出文件包含预期新文案。
            all_text = '\n'.join(x['text'] for x in final_info['slides'])
            if any(c['new_text'] not in all_text for c in plan['changes']):
                raise ValueError('最终PPT缺少替换后的文字，版本未登记')
            version = {'id': vid, 'path': str(output.relative_to(folder)), 'sha256': digest(output),
                       'base_ref': plan['base_ref'], 'base_sha256': base['sha256'], 'at': now(),
                       'purpose': plan['purpose'], 'slide_count': final_info['slide_count'],
                       'placeholder_slides': [x['slide'] for x in final_info['slides'] if re.search('图片预留|待补|待确认|待核定',x['text'])],
                       'build_dir': str(build.relative_to(folder)), 'fact_revision': s['fact_revision'],
                       'fact_keys': plan.get('fact_keys', []), 'needs_review': False,
                       'engine': 'artifact-tool/imported-text-replacement', 'machine_seconds': round(time.monotonic()-started, 2)}
            s['versions'].append(version)
            s['changes'].append({'version': vid, 'classification': plan.get('classification', '待分类'),
                                 'request': plan['purpose'], 'changes': plan['changes']})
            s['status'] = '待检查'
            self.event(s, 'version_built', version=vid, machine_seconds=version['machine_seconds'])
        self.packet(order_id)
        return version

    def register(self, order_id, source, note, base_ref=None):
        """复杂设计由当前Codex制作后接回同一订单；外部成稿也必须检查。"""
        source = Path(source).resolve(strict=True)
        info = ppt_info(source)
        with self.edit(order_id) as s:
            if base_ref:
                self.reference(s, base_ref)
            vid = f'v{len(s["versions"])+1:03d}-' + uuid.uuid4().hex[:10]
            folder = self.order_dir(order_id)
            target = folder / 'versions' / vid / 'deck.pptx'
            target.parent.mkdir(parents=True)
            shutil.copy2(source, target)
            v = {'id': vid, 'path': str(target.relative_to(folder)), 'sha256': digest(target), 'base_ref': base_ref,
                 'purpose': note, 'at': now(), 'slide_count': info['slide_count'], 'engine': 'external',
                 'placeholder_slides': [x['slide'] for x in info['slides'] if re.search('图片预留|待补|待确认|待核定',x['text'])],
                 'fact_revision': s['fact_revision'], 'fact_keys': [], 'needs_review': False}
            s['versions'].append(v)
            s['status'] = '待检查'
            self.event(s, 'external_version_registered', version=vid)
        return v

    def review(self, order_id, version_id, payload):
        required = ('reviewer', 'evidence', 'content', 'visual', 'editability')
        if any(not payload.get(k) for k in required):
            raise ValueError('审核须记录检查人、依据及内容/视觉/可编辑性结论')
        if any(payload[k] not in ('pass', 'fail') for k in ('content', 'visual', 'editability')):
            raise ValueError('审核结果只能为pass或fail')
        with self.edit(order_id) as s:
            _, v = self.reference(s, version_id)
            if v not in s['versions']:
                raise ValueError('只能审核制作版本')
            if not s['reconciled']:
                raise ValueError('新资料尚未核对')
            if v.get('needs_review') and not payload.get('fact_reconciliation'):
                raise ValueError('本版涉及旧事实，请说明新旧事实核对结果或制作新版本')
            if payload.get('approved_sha256') != v['sha256']:
                raise ValueError('审核必须绑定检查过的文件指纹')
            record = {**payload, 'version': version_id, 'sha256': v['sha256'], 'at': now()}
            s['reviews'].append(record)
            passed = all(payload[k] == 'pass' for k in ('content', 'visual', 'editability'))
            if passed:
                v['needs_review'] = False
            s['status'] = '已检查' if passed else '需修改'
            self.event(s, 'internal_review', version=version_id, passed=passed)
        return record

    def package(self, order_id, version_id, draft=False):
        with self.edit(order_id) as s:
            path, v = self.reference(s, version_id)
            if v not in s['versions'] or v.get('needs_review') or not s['reconciled']:
                raise ValueError('当前版本或新资料仍需核对')
            reviews = [x for x in s['reviews'] if x['version'] == version_id and x['sha256'] == v['sha256']]
            if not reviews or any(reviews[-1][k] != 'pass' for k in ('content', 'visual', 'editability')):
                raise ValueError('交付前必须完成当前版本的内容、视觉和可编辑性检查')
            open_gaps = [g for g in s['gaps'] if g.get('status', 'open') == 'open']
            if (open_gaps or v.get('placeholder_slides')) and not draft:
                raise ValueError('仍有资料缺项，只能生成明确标记的样稿包，不能当终稿')
            folder = self.order_dir(order_id)
            did = 'D' + uuid.uuid4().hex[:10]
            out = folder / 'delivery' / did
            out.mkdir(parents=True)
            label = '演示' if s['mode'] == 'demo' else ('样稿' if draft else '交付稿')
            dest = out / (label + '.pptx')
            shutil.copy2(path, dest)
            text = f'# {label}\n\n项目：{s["brief"]["title"]}\n页数：{v["slide_count"]}\n'
            if s['mode'] == 'demo':
                text += '\n本文件用于内部流程验证，不是客户终稿或成交证明。\n'
            if draft:
                text += '\n以下内容仍待补充：\n' + '\n'.join('- ' + g['question'] for g in open_gaps) + '\n'
            (out / '交付说明.md').write_text(text, encoding='utf-8')
            with ZipFile(out / '交付包.zip', 'w', ZIP_DEFLATED) as z:
                z.write(dest, dest.name)
                z.write(out / '交付说明.md', '交付说明.md')
            item = {'id': did, 'version': version_id, 'sha256': v['sha256'], 'path': str(out.relative_to(folder)),
                    'stage': 'draft' if draft else 'final', 'prepared_at': now(), 'sent_at': None, 'accepted_at': None}
            s['deliveries'].append(item)
            s['status'] = '演示交付包就绪' if s['mode'] == 'demo' else '待发送'
            self.event(s, 'delivery_prepared', delivery=did, note='仅本地打包，未发送客户')
        return item

    def record(self, order_id, kind, payload):
        with self.edit(order_id) as s:
            if kind == 'finance':
                if not payload.get('evidence') or isinstance(payload.get('agreed_total'),bool) or not isinstance(payload.get('agreed_total'), (int, float)) or not math.isfinite(payload['agreed_total']) or payload['agreed_total'] < 0:
                    raise ValueError('总价必须非负且含确认来源')
                s['finance'] = payload
            elif kind in ('payment', 'cost', 'time'):
                key = 'minutes' if kind == 'time' else 'amount'
                if isinstance(payload.get(key), bool) or not isinstance(payload.get(key), (int, float)) or not math.isfinite(payload[key]) or payload[key] < 0 or not payload.get('evidence'):
                    raise ValueError('金额或分钟须非负，并说明凭据/来源')
                bucket = {'payment': 'payments', 'cost': 'costs', 'time': 'work_logs'}[kind]
                eid = payload.get('id')
                if not eid:
                    raise ValueError('记录必须有唯一id，避免重复计费/工时')
                if any(x['id'] == eid for x in s[bucket]):
                    raise ValueError('该记录ID已存在，未重复记账')
                s[bucket].append({**payload, 'at': now()})
            elif kind in ('sent', 'accepted'):
                item = next((d for d in s['deliveries'] if d['id'] == payload.get('delivery_id')), None)
                if not item or not payload.get('evidence'):
                    raise ValueError('需指明交付包和真实发送/验收依据')
                if kind == 'accepted' and not item['sent_at']:
                    raise ValueError('尚无发送记录，不能登记客户验收')
                item['sent_at' if kind == 'sent' else 'accepted_at'] = now()
                item[kind + '_evidence'] = payload['evidence']
                s['status'] = '待客户验收' if kind == 'sent' else '制作完成'
            elif kind == 'learning':
                if any(not payload.get(k) for k in ('observation', 'action', 'result', 'next_validation')):
                    raise ValueError('经验必须含观察、动作、结果及下次验证方式')
                s['learnings'].append({**payload, 'at': now(), 'status': '候选', 'validated_orders': 0})
            else:
                raise ValueError('未知记录类型')
            self.event(s, kind + '_recorded', detail=payload)
        self.packet(order_id)
        return self.status(order_id)

    def packet(self, order_id):
        s = self.load(order_id)
        folder = self.order_dir(order_id)
        rule_path = self.root / 'config' / 'production_rules.json'
        rules = read_json(rule_path) if rule_path.exists() else []
        compact = {'order_id': order_id, 'mode': s['mode'], 'brief': s['brief'], 'facts': s['facts'],
                   'fact_revision': s['fact_revision'], 'gaps': s['gaps'], 'reconciled': s['reconciled'],
                   'sources': [{k: x[k] for k in ('id', 'name', 'role', 'sha256', 'cache')} for x in s['sources']],
                   'latest_versions': s['versions'][-2:], 'rules': rules,
                   'production_route': s.get('production_route')}
        # 超长需求仍保留于workflow.json，任务包明确列出省略字段并按需读取。
        compact['full_record'] = str(folder / 'workflow.json')
        compact['omitted'] = []
        for key, value in list(compact['brief'].items()):
            if len(json.dumps(value, ensure_ascii=False)) > 1500:
                compact['brief'] = dict(compact['brief'])
                compact['brief'][key] = '内容较长，按需读取完整订单中的此字段'
                compact['omitted'].append('brief.' + key)
        while len(json.dumps(compact, ensure_ascii=False, indent=2)) > 12000:
            if compact['sources']:
                omitted = compact['sources'].pop()
                compact['omitted'].append('source:' + omitted['id'])
            elif compact['facts']:
                compact['facts'] = dict(compact['facts'])
                key = next(reversed(compact['facts']))
                compact['facts'].pop(key)
                compact['omitted'].append('fact:' + key)
            elif compact['gaps']:
                omitted = compact['gaps'].pop()
                compact['omitted'].append('gap:' + omitted['id'])
            else:
                raise ValueError('任务包核心字段超出12000字符，请精简需求标题或用途')
            # 极端批量订单只列省略数量，防止省略项清单自身无限增长。
            if len(compact['omitted']) > 60:
                compact['omitted'] = ['多项内容已省略，请按完整订单路径读取']
        atomic_json(folder / 'context' / 'task_packet.json', compact)
        text = '# 制作任务包\n\n' + json.dumps(compact, ensure_ascii=False, indent=2)
        (folder / 'context' / '任务包.md').write_text(text, encoding='utf-8')
        gaps = '# 缺项与受影响版本\n\n' + '\n'.join('- ' + g['question'] for g in s['gaps'] if g.get('status', 'open') == 'open')
        gaps += '\n\n需核对版本：' + '、'.join(v['id'] for v in s['versions'] if v.get('needs_review'))
        (folder / 'context' / '缺项表.md').write_text(gaps, encoding='utf-8')
        return {'path': str(folder / 'context' / '任务包.md'), 'characters': len(text), 'note': '字符数不是Token数；不含源文件全文和完整事件日志'}

    def status(self, order_id):
        s = self.load(order_id)
        paid = sum(x['amount'] for x in s['payments'])
        total = s['finance'].get('agreed_total')
        return {'order_id': order_id, 'mode': s['mode'], 'title': s['brief']['title'], 'status': s['status'],
                'sources': len(s['sources']), 'versions': len(s['versions']), 'open_gaps': sum(g.get('status','open')=='open' for g in s['gaps']),
                'agreed_total': total, 'received': paid, 'remaining': None if total is None else round(total-paid, 2),
                'recorded_human_minutes': sum(x['minutes'] for x in s['work_logs']),
                'costs_complete': False, 'profit': None, 'learnings': len(s['learnings'])}

    def route(self, order_id, payload):
        """路线只作为内部建议保存；不改变报价、到账与客户承诺。"""
        from production_router import choose_route
        policy = read_json(self.root/'config/routing_policy.json')
        result = choose_route(payload, policy)
        with self.edit(order_id) as s:
            record = {'input': payload, 'policy': policy, 'decision': result,
                      'fact_revision': s['fact_revision'], 'needs_recheck': False, 'at': now()}
            s['production_route'] = record
            self.event(s, 'production_route_selected', **record)
        self.packet(order_id)
        return result

    def dashboard(self):
        rows = []
        for p in sorted((self.root / 'orders').glob('*/workflow.json')):
            s = self.status(p.parent.name)
            state = self.load(p.parent.name)
            link = p.parent.as_uri()
            outputs = []
            if state['versions']:
                latest = state['versions'][-1]
                outputs.append('<a href="'+(p.parent/latest['path']).as_uri()+'">最新PPT</a>')
            if state['deliveries']:
                outputs.append('<a href="'+(p.parent/state['deliveries'][-1]['path']/'交付包.zip').as_uri()+'">交付包</a>')
            route_record = state.get('production_route', {})
            decision = route_record.get('decision', {})
            route_label = {'economy':'省Token', 'quality':'质量'}.get(decision.get('route'), '待选择')
            if route_record.get('needs_recheck'): route_label += '（资料变化待复核）'
            elif decision.get('provisional'): route_label += '（暂定）'
            cells = [s['order_id'], s['title'], s['mode'], s['status']+' / '+route_label, s['versions'], s['open_gaps'],
                     '未定' if s['agreed_total'] is None else f'{s["agreed_total"]} / {s["received"]} / {s["remaining"]}']
            rows.append('<tr>' + ''.join('<td>'+html.escape(str(c))+'</td>' for c in cells) + f'<td><a href="{link}">订单目录</a><br>'+ ' · '.join(outputs) +'</td></tr>')
        page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>PPT制作工作区</title>
<style>body{font:16px/1.6 system-ui;background:#f7f8fa;color:#183046;max-width:1400px;margin:48px auto;padding:0 30px}h1{font-size:30px}table{border-collapse:collapse;width:100%;background:white}td,th{text-align:left;padding:14px;border-bottom:1px solid #dde3e9}a{color:#0869b6}p{max-width:1000px}small{color:#617080}</style>
<h1>闲鱼 PPT 制作工作区</h1><p>先制作，再连接接待与获客。将需求、资料路径、修改原话交给本工作区内的 Codex；订单档案、任务包、版本、交付包与经验记录由工作流保存。</p>
<p><a href="README.md">使用说明</a> · <a href="执行计划书.md">执行计划</a> · <a href="双路线制作说明.md">双路线规则</a> · <a href="reports/黄已经双路线重制对比.md">黄已经两版对比</a> · <a href="reports/最小闭环验收.md">运行验证</a></p>
<table><thead><tr><th>编号</th><th>项目</th><th>类型</th><th>制作状态</th><th>版本</th><th>缺项</th><th>总价 / 已收 / 待收</th><th>文件</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table>
<p>live：实际新订单；history：历史记录回填；demo：内部验证。制作、发送、客户验收、收款分别登记。</p>
<small>本页为本地状态快照。双击“打开工作台.cmd”刷新。没有联网接待或自动发送客户消息。</small></html>'''
        target = self.root / '工作台.html'
        target.write_text(page, encoding='utf-8')
        return {'path': str(target), 'orders': len(rows)}


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    x = sub.add_parser('new'); x.add_argument('order'); x.add_argument('--brief', required=True); x.add_argument('--mode', choices=['live','demo'], default='live')
    for cmd in ('history', 'status', 'packet'):
        x = sub.add_parser(cmd); x.add_argument('order')
    x = sub.add_parser('source'); x.add_argument('order'); x.add_argument('path'); x.add_argument('--role', default='content')
    for cmd in ('reconcile', 'build', 'route'):
        x = sub.add_parser(cmd); x.add_argument('order'); x.add_argument('json_file')
    x = sub.add_parser('inspect'); x.add_argument('order'); x.add_argument('ref')
    x = sub.add_parser('register'); x.add_argument('order'); x.add_argument('path'); x.add_argument('--note', required=True); x.add_argument('--base-ref')
    x = sub.add_parser('review'); x.add_argument('order'); x.add_argument('version'); x.add_argument('json_file')
    x = sub.add_parser('package'); x.add_argument('order'); x.add_argument('version'); x.add_argument('--draft', action='store_true')
    x = sub.add_parser('record'); x.add_argument('order'); x.add_argument('kind'); x.add_argument('json_file')
    sub.add_parser('dashboard')
    args = p.parse_args(); w = Workflow()
    try:
        if args.cmd == 'new': result=w.create(args.order, read_json(args.brief), args.mode)
        elif args.cmd == 'history': result=w.import_history(args.order)
        elif args.cmd == 'source': result=w.add_source(args.order, args.path, args.role)
        elif args.cmd in ('reconcile', 'build', 'route'): result=getattr(w,args.cmd)(args.order, read_json(args.json_file))
        elif args.cmd in ('status', 'packet'): result=getattr(w,args.cmd)(args.order)
        elif args.cmd == 'inspect': result=w.inspect(args.order,args.ref)
        elif args.cmd == 'register': result=w.register(args.order,args.path,args.note,args.base_ref)
        elif args.cmd == 'review': result=w.review(args.order,args.version,read_json(args.json_file))
        elif args.cmd == 'package': result=w.package(args.order,args.version,args.draft)
        elif args.cmd == 'record': result=w.record(args.order,args.kind,read_json(args.json_file))
        else: result=w.dashboard()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, FileNotFoundError, KeyError, OSError, subprocess.TimeoutExpired) as e:
        print('操作未完成：' + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
