import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock
from types import SimpleNamespace
import discord

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
        for name in ['농부','어부','광부','요리사','연금','목수','시세설정','시세설정확인','고점기준','시세채팅정리']:
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
        self.assertIn('현재 공식 사이트',render(fixture(),'광부')[0].description)
        self.assertNotIn('🔥 고점',render(fixture(),'농부')[0].description)

    async def test_stale_and_future_data(self):
        data=fixture()
        self.assertTrue(fresh(data))
        data['updatedAt']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        self.assertTrue(fresh(data))  # 시세가 바뀔 예정 시각 전이면 정상
        self.assertNotIn('⚠️',render(data,'농부')[0].description)
        self.assertIn('시세 갱신:',render(data,'농부')[0].description)
        data['shops'][0]['nextChangeAt']=(datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat()
        self.assertFalse(fresh(data))
        self.assertIn('예정된 갱신',render(data,'농부')[0].description)
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
        data['shops'][0]['nextChangeAt']='2020-01-01T01:00:00+09:00'
        self.market.emit=AsyncMock()
        await self.market.poll.coro(self.market)
        self.market.emit.assert_not_called()

    async def test_kst_deadline_grace_and_job_scope(self):
        now=datetime(2026,11,22,1,0,tzinfo=timezone(timedelta(hours=9)))
        data=fixture()
        data['updatedAt']='2026-11-22 00:00:02'
        data['shops']=[{'name':'농부 상점','nextChangeAt':'2026-11-21T18:00:00Z'},
                       {'name':'연금술사 상점','nextChangeAt':'2026-11-21T14:00:00Z'}]
        self.assertTrue(fresh(data,now,job='농부'))
        self.assertFalse(fresh(data,now,job='연금'))
        self.assertNotIn('⚠️',render(data,'농부',now=now)[0].description)
        data['shops'][0]['nextChangeAt']=now.isoformat()
        self.assertTrue(fresh(data,now+timedelta(minutes=2),job='농부'))
        self.assertFalse(fresh(data,now+timedelta(minutes=2,seconds=1),job='농부'))

    async def test_cached_lookup_preserves_timestamp_and_no_double_warning(self):
        self.market.data=fixture()
        self.market.data['updatedAt']='2026-11-22 00:00:02'
        self.market.fetch=AsyncMock(side_effect=ValueError('site unavailable'))
        interaction=SimpleNamespace(guild_id=1,response=SimpleNamespace(defer=AsyncMock()),
                                    followup=SimpleNamespace(send=AsyncMock()))
        await self.market.lookup(interaction,'농부')
        interaction.followup.send.assert_awaited_once()
        embed=interaction.followup.send.call_args.kwargs['embed']
        self.assertIn('11/22 00:00',embed.description)
        self.assertIn('조회에 실패',embed.description)
        self.assertEqual(embed.description.count('⚠️'),1)

    async def test_unknown_schedule_or_missing_job_does_not_guess_future_support(self):
        data=fixture()
        data['shops'][0]['nextChangeAt']=None
        self.assertFalse(fresh(data,job='농부'))
        self.assertNotIn('오래된',render(data,'농부')[0].description)
        missing=render(data,'목수')[0].description
        self.assertIn('현재 공식 사이트',missing)
        self.assertNotIn('아직',missing)
        self.assertIn('조회에 실패',render(data,'목수',cached=True)[0].description)

    async def test_cleanup_scope_bots_replies_and_permissions(self):
        msg=SimpleNamespace(guild=SimpleNamespace(id=1,me=object()),
            author=SimpleNamespace(bot=False),webhook_id=None,
            type=discord.MessageType.default,interaction_metadata=None,delete=AsyncMock(),
            channel=SimpleNamespace(id=123,permissions_for=lambda _:SimpleNamespace(manage_messages=True)))
        await self.market.clean_message(msg)
        msg.delete.assert_not_called()
        with c.connect() as db:
            db.execute('INSERT INTO market_cleanup VALUES(1,123)')
        await self.market.clean_message(msg)
        self.assertEqual(msg.delete.call_count,1)
        msg.author.bot=True
        await self.market.clean_message(msg)
        self.assertEqual(msg.delete.call_count,1)
        msg.author.bot=False
        msg.type=discord.MessageType.reply
        await self.market.clean_message(msg)
        self.assertEqual(msg.delete.call_count,2)
        msg.channel.permissions_for=lambda _:SimpleNamespace(manage_messages=False)
        await self.market.clean_message(msg)
        self.assertEqual(msg.delete.call_count,2)
        msg.guild.id=2
        await self.market.clean_message(msg)
        self.assertEqual(msg.delete.call_count,2)

    async def test_long_list_pages_and_stable_digest(self):
        data=fixture()
        data['items']=[dict(data['items'][0],productId=i,name='가'*40+str(i)) for i in range(150)]
        embeds=render(data,'농부')
        self.assertGreater(len(embeds),1)
        self.assertTrue(all(len(e.description)<4096 for e in embeds))
        self.assertEqual(fingerprint(data['items']),fingerprint(data['items'][::-1]))

if __name__=='__main__':
    unittest.main()
