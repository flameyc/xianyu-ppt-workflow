"""检查会影响订单隔离、错误交付、重复记账和事实过期的关键行为。"""
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflow import Workflow, atomic_json, digest


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.w = Workflow(self.root)
        self.oid = 'TEST-01'
        self.w.create(self.oid, {'title': '测试订单'}, 'demo')
        self.source = self.root / 'content.txt'
        self.source.write_text('客户资料', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def fake_version(self):
        folder = self.w.order_dir(self.oid)
        target = folder / 'versions/v001/deck.pptx'
        target.parent.mkdir(parents=True)
        # 非渲染测试用字节，不冒充有效PPTX；实际引擎由真实5页演示验证。
        target.write_bytes(b'unit-test-artifact')
        version = {'id': 'v001', 'path': 'versions/v001/deck.pptx', 'sha256': digest(target),
                   'slide_count': 1, 'fact_keys': ['time'], 'needs_review': False}
        with self.w.edit(self.oid) as s:
            s['versions'].append(version)
        return version

    def review_payload(self, v):
        return {'reviewer': '测试模拟检查', 'evidence': '测试桩，不是真实视觉审核',
                'content': 'pass', 'visual': 'pass', 'editability': 'pass', 'approved_sha256': v['sha256']}

    def test_path_traversal_rejected(self):
        for bad in ('../other', 'E:/other', '..', 'a/b'):
            with self.assertRaises(ValueError):
                self.w.order_dir(bad)

    def test_duplicate_order_not_overwritten(self):
        before = self.w.load(self.oid)
        with self.assertRaises(ValueError):
            self.w.create(self.oid, {'title': '覆盖'})
        self.assertEqual(before, self.w.load(self.oid))

    def test_source_deduplicated_and_snapshot_preserved(self):
        a = self.w.add_source(self.oid, self.source)
        b = self.w.add_source(self.oid, self.source)
        self.assertEqual(a['id'], b['id'])
        self.source.write_text('客户后来改过', encoding='utf-8')
        path, _ = self.w.reference(self.w.load(self.oid), a['id'])
        self.assertEqual(path.read_text(encoding='utf-8'), '客户资料')

    def test_mutated_version_blocks_delivery(self):
        v = self.fake_version()
        self.w.review(self.oid, v['id'], self.review_payload(v))
        (self.w.order_dir(self.oid) / v['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '外部修改'):
            self.w.package(self.oid, v['id'])

    def test_changed_fact_invalidates_old_review(self):
        v = self.fake_version()
        self.w.review(self.oid, v['id'], self.review_payload(v))
        self.w.reconcile(self.oid, {'evidence': '客户改时间', 'facts': {'time': {'value':'18:00','source':'客户消息'}}})
        self.assertTrue(self.w.load(self.oid)['versions'][0]['needs_review'])
        with self.assertRaises(ValueError):
            self.w.package(self.oid, v['id'])

    def test_new_material_requires_reconciliation(self):
        v = self.fake_version()
        self.w.review(self.oid, v['id'], self.review_payload(v))
        self.w.add_source(self.oid, self.source)
        with self.assertRaises(ValueError):
            self.w.package(self.oid, v['id'])

    def test_failed_review_blocks_delivery(self):
        v = self.fake_version()
        payload = self.review_payload(v)
        self.w.review(self.oid, v['id'], payload)
        payload['visual'] = 'fail'
        self.w.review(self.oid, v['id'], payload)
        with self.assertRaises(ValueError):
            self.w.package(self.oid, v['id'])

    def test_gaps_allow_draft_but_block_final(self):
        v = self.fake_version()
        self.w.reconcile(self.oid, {'evidence':'资料核对','gaps':[{'id':'G1','question':'缺实际照片','status':'open'}]})
        self.w.review(self.oid, v['id'], self.review_payload(v))
        with self.assertRaises(ValueError):
            self.w.package(self.oid, v['id'])
        d = self.w.package(self.oid, v['id'], draft=True)
        self.assertIsNone(d['sent_at'])
        with ZipFile(self.w.order_dir(self.oid)/d['path']/'交付包.zip') as z:
            self.assertEqual(set(z.namelist()), {'演示.pptx','交付说明.md'})

    def test_duplicate_payment_rejected_and_unknown_profit(self):
        self.w.record(self.oid, 'finance', {'agreed_total':100,'evidence':'用户确认'})
        self.w.record(self.oid, 'payment', {'id':'P1','amount':30,'evidence':'收款记录'})
        with self.assertRaises(ValueError):
            self.w.record(self.oid, 'payment', {'id':'P1','amount':30,'evidence':'收款记录'})
        s = self.w.status(self.oid)
        self.assertEqual(s['remaining'], 70)
        self.assertIsNone(s['profit'])

    def test_failed_mutation_rolls_back_state(self):
        before = self.w.load(self.oid)
        with self.assertRaises(ValueError):
            self.w.reconcile(self.oid, {'evidence':'有来源','facts':{'time':{'value':'18:00','source':'客户'}},'gaps':[{}]})
        self.assertEqual(before, self.w.load(self.oid))

    def test_customer_acceptance_requires_sent_record(self):
        v = self.fake_version()
        self.w.review(self.oid, v['id'], self.review_payload(v))
        d = self.w.package(self.oid, v['id'], draft=True)
        with self.assertRaises(ValueError):
            self.w.record(self.oid,'accepted',{'delivery_id':d['id'],'evidence':'没有发送就验收'})

    def test_packet_does_not_include_raw_material_or_full_log(self):
        self.w.add_source(self.oid, self.source)
        packet = self.w.packet(self.oid)
        text = Path(packet['path']).read_text(encoding='utf-8')
        self.assertNotIn('客户资料', text)
        self.assertNotIn('source_added', text)

    def test_large_packet_is_bounded_and_omissions_visible(self):
        facts = {f'F{i}': {'value':'长内容'*200, 'source':'客户文件'} for i in range(40)}
        self.w.reconcile(self.oid, {'evidence':'大量资料','facts':facts})
        packet = self.w.order_dir(self.oid)/'context/task_packet.json'
        self.assertLessEqual(len(packet.read_text(encoding='utf-8').rstrip()), 12000)
        self.assertTrue(json.loads(packet.read_text(encoding='utf-8'))['omitted'])
        self.assertEqual(len(self.w.load(self.oid)['facts']), 40)

    def test_new_fact_without_old_dependency_invalidates_review(self):
        v = self.fake_version()
        self.w.reconcile(self.oid, {'evidence':'新增场地','facts':{'venue':{'value':'客户新场地','source':'原话'}}})
        self.assertTrue(self.w.load(self.oid)['versions'][0]['needs_review'])

    def test_resolving_gap_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.w.reconcile(self.oid, {'evidence':'随意完成','gaps':[{'id':'G1','question':'缺图','status':'resolved'}]})

    def test_nonfinite_money_rejected(self):
        for amount in (float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                self.w.record(self.oid,'finance',{'agreed_total':amount,'evidence':'测试'})
            with self.assertRaises(ValueError):
                self.w.record(self.oid,'payment',{'id':'bad','amount':amount,'evidence':'测试'})

    def test_unreviewed_version_cannot_be_packaged(self):
        v = self.fake_version()
        with self.assertRaises(ValueError):
            self.w.package(self.oid, v['id'], draft=True)

    def test_lock_prevents_concurrent_order_mutation(self):
        with self.w.edit(self.oid):
            with self.assertRaises(ValueError):
                self.w.record(self.oid,'finance',{'agreed_total':100,'evidence':'测试'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
