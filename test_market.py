import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import contribution_bot as c
from market import Market, render, fresh, fingerprint


def fixture():
    now = datetime.now(timezone.utc)
    return {'updatedAt':now.isoformat(),
            'shops':[{'name':'농부 상점','nextChangeAt':(now+timedelta(minutes=5)).isoformat()}],
            'items':[{'productId':1,'shop':'농부 상점','name':'밀','grade':'일반',
                      'price':130,'basePrice':100,'changePercent':30,'currency':'쿠키'},
                     {'productId':2,'shop':'농부 상점','name':'호박','grade':'고급',
                      'price':90,'basePrice':100,'changePercent':-10,'currency':'쿠키'}]}


class MarketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = c.DB_PATH
        c.DB_PATH = Path(self.tmp.name)/'test.db'
        # 테스트마다 같은 전역 트리에 명령어가 중복 등록되지 않게 정리
        for name in ['농부','어부','광부','요리사','연금','목수','시세설정','시세설정확인','고점기준']:
            c.bot.tree.remove_command(name)
        self.market = Market(c.bot,c.connect,c.manager)
        self.market.poll.cancel()

    async def asyncTearDown(self):
        self.market.poll.cancel()
        c.DB_PATH = self.old
        self.tmp.cleanup()

    async def test_commands_and_delivery_tables_coexist(self):
        names = {x.name for x in c.bot.tree.get_commands()}
        self.assertTrue({'납품등록','백업','농부','어부','광부','요리사','연금','목수'} <= names)
        with c.connect() as db:
            db.execute("INSERT INTO materials VALUES(1,'돌',2)")
            self.assertEqual(db.execute('SELECT weight FROM materials').fetchone()[0],2)

    async def test_rising_only_grade_and_high_threshold(self):
        text = render(fixture(),'농부',25)[0].description
        self.assertIn('🔥 고점',text)
        self.assertIn('밀 [일반]',text)
        self.assertNotIn('호박',text)
        self.assertIn('아직',render(fixture(),'광부')[0].description)
        self.assertNotIn('🔥 고점',render(fixture(),'농부')[0].description)

    async def test_stale_and_future_data(self):
        data=fixture()
        self.assertTrue(fresh(data))
        data['updatedAt']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        self.assertFalse(fresh(data))
        self.assertIn('오래된',render(data,'농부')[0].description)
        data['updatedAt']=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        self.assertFalse(fresh(data))

    async def test_poll_baseline_warning_update_and_dedup(self):
        data=fixture()
        self.market.fetch=AsyncMock(return_value=data)
        self.market.emit=AsyncMock()
        with c.connect() as db:
            db.execute('INSERT INTO market_settings VALUES(1,\'농부\',123,1,NULL)')
        await self.market.poll.coro(self.market)
        self.assertEqual(self.market.emit.call_count,1) # 시작 시 갱신 알림 없이 사전 안내만
        data['items'][0]['price']=140
        await self.market.poll.coro(self.market)
        self.assertEqual(self.market.emit.call_count,3) # 사전 알림은 emit에서 중복 억제
        self.assertTrue(self.market.emit.call_args.args[1].startswith('update:'))
        channel=AsyncMock()
        original=c.bot.get_channel
        c.bot.get_channel=lambda _:channel
        try:
            self.market.emit=Market.emit.__get__(self.market)
            with c.connect() as db:
                row=db.execute('SELECT * FROM market_settings').fetchone()
            await self.market.emit(row,'test','hello')
            await self.market.emit(row,'test','hello')
            self.assertEqual(channel.send.call_count,1)
        finally:
            c.bot.get_channel=original
        data['updatedAt']='2020-01-01 00:00:00'
        self.market.emit=AsyncMock()
        await self.market.poll.coro(self.market)
        self.market.emit.assert_not_called()

    async def test_long_list_pages_and_stable_digest(self):
        data=fixture()
        data['items']=[dict(data['items'][0],productId=i,name='가'*40+str(i)) for i in range(150)]
        embeds=render(data,'농부')
        self.assertGreater(len(embeds),1)
        self.assertTrue(all(len(e.description)<4096 for e in embeds))
        self.assertEqual(fingerprint(data['items']),fingerprint(data['items'][::-1]))

if __name__=='__main__':
    unittest.main()
