from pathlib import Path
import sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from delivery_budget import DeliveryBudget
from delivery_budget import DeliveryDeferred
from hub import Hub


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'state.db')

    def test_platform_cooldown_persists_across_restart(self):
        budget=DeliveryBudget(self.path)
        self.assertTrue(budget.reserve(1000))
        budget.throttled(900,1000)
        restarted=DeliveryBudget(self.path)
        self.assertFalse(restarted.reserve(1899))
        self.assertEqual(restarted.delay(1899),1)
        self.assertTrue(restarted.reserve(1900))

    def test_daily_budget_does_not_forget_attempts_on_restart(self):
        budget=DeliveryBudget(self.path,2)
        self.assertTrue(budget.reserve(1000));self.assertTrue(budget.reserve(1001))
        self.assertFalse(DeliveryBudget(self.path,2).reserve(1002))
        self.assertFalse(budget.reserve(87399));self.assertTrue(budget.reserve(87400))

    def test_shorter_rejection_does_not_shorten_existing_cooldown(self):
        budget=DeliveryBudget(self.path)
        budget.throttled(900,1000);budget.throttled(60,1001)
        self.assertEqual(budget.delay(1001),899)


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_http429_stops_other_sends_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            token=Path(folder)/'token';token.write_text('fixture_token')
            config=dict(database=str(Path(folder)/'state.db'),owner='fixture',token_file=str(token),send_url='http://example.invalid/send')
            class Response:
                status=429;headers={}
                async def __aenter__(self):return self
                async def __aexit__(self,*args):pass
                async def json(self,**kwargs):raise AssertionError('A 429 need not have a JSON body')
            class HTTP:
                count=0
                def post(self,*args,**kwargs):self.count+=1;return Response()
            http=HTTP();hub=Hub(config);hub.http=http
            with self.assertRaises(DeliveryDeferred):await hub.send('user','message')
            with self.assertRaises(DeliveryDeferred):await hub.send('user','message')
            restarted=Hub(config);restarted.http=http
            with self.assertRaises(DeliveryDeferred):await restarted.send('user','message')
            self.assertEqual(http.count,1)
