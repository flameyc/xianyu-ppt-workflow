import json
import tempfile
import unittest
from pathlib import Path
from workflow import Workflow

class RouteIntegrationTests(unittest.TestCase):
    def test_route_never_changes_finance_and_new_fact_invalidates_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'config').mkdir()
            (root/'config/routing_policy.json').write_text('{"quality_threshold":"10"}',encoding='utf-8')
            w=Workflow(root);w.create('DEMO-ROUTE',{'title':'路由隔离验证'},'demo')
            before=w.load('DEMO-ROUTE')
            decision=w.route('DEMO-ROUTE',{'agreed_total':'80','billable_pages':20,'price_evidence':'历史文字'})
            after=w.load('DEMO-ROUTE')
            self.assertEqual(decision['route'],'economy')
            for key in ('finance','payments','deliveries'):
                self.assertEqual(before[key],after[key])
            packet=json.loads((root/'orders/DEMO-ROUTE/context/task_packet.json').read_text(encoding='utf-8'))
            self.assertEqual(packet['production_route']['decision']['effective_unit_price'],'4')
            w.reconcile('DEMO-ROUTE',{'evidence':'追加来源','facts':{'new_scope':{'value':'复杂品牌图表','source':'客户新要求'}}})
            self.assertTrue(w.load('DEMO-ROUTE')['production_route']['needs_recheck'])
