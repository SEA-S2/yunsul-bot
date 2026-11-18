import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import contribution_bot as c
from timers import Timers,TimerStore,duration


class TimerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old=c.DB_PATH
        c.DB_PATH=Path(self.tmp.name)/'test.db'
        c.bot.tree.remove_command('타이머')
        self.timers=Timers(c.bot,c.connect,c.manager)
        self.timers.tick.cancel()

    async def asyncTearDown(self):
        self.timers.tick.cancel()
        c.DB_PATH=self.old
        self.tmp.cleanup()

    async def test_presets_and_custom_duration_validation(self):
        self.assertEqual(duration('요리',None,0),86)
        self.assertEqual(duration('연금',None,0),56)
        self.assertEqual(duration('네더스프링클러',None,0),2700)
        self.assertEqual(duration('농사',16,0),960)
        self.assertEqual(duration('요리',0,20),20)
        for kind,mins,secs in [('농사',None,0),('직접설정',0,0),('호감도',None,0),('요리',10080,1)]:
            with self.assertRaises(ValueError):duration(kind,mins,secs)

    async def test_persist_replace_and_cancel_ownership(self):
        store=self.timers.store
        due,replaced=store.start(1,10,'농사',100,960,now=100)
        self.assertEqual(due,1060)
        self.assertFalse(replaced)
        due,replaced=store.start(1,10,'농사',100,600,now=200)
        self.assertEqual(due,800)
        self.assertTrue(replaced)
        self.assertEqual(TimerStore(c.connect).list(1,10)[0]['due'],800)
        self.assertFalse(store.cancel(1,11,'농사'))
        self.assertFalse(store.cancel(2,10,'농사'))
        self.assertEqual(store.cancel(1,10,'농사'),1)
        self.assertEqual(store.list(1,10),[])

    async def test_limit_and_replace_at_limit(self):
        for i in range(10):self.timers.store.start(1,10,str(i),100,60)
        with self.assertRaises(ValueError):self.timers.store.start(1,10,'new',100,60)
        self.timers.store.start(1,10,'0',100,30)
        self.timers.store.start(1,11,'new',100,60)

    async def test_overdue_delivery_mentions_once_and_future_retained(self):
        channel=SimpleNamespace(send=AsyncMock(),permissions_for=lambda _:SimpleNamespace(view_channel=True,send_messages=True))
        guild=SimpleNamespace(me=object(),get_channel=lambda _:channel)
        old=c.bot.get_guild
        c.bot.get_guild=lambda _:guild
        try:
            self.timers.store.start(1,10,'완료예정',100,1,now=time.time()-100)
            self.timers.store.start(1,11,'future',100,1000)
            await self.timers.tick.coro(self.timers)
            await self.timers.tick.coro(self.timers)
            self.assertEqual(channel.send.call_count,1)
            self.assertIn('<@10>',channel.send.call_args.args[0])
            self.assertIn('늦게',channel.send.call_args.args[0])
            mentions=channel.send.call_args.kwargs['allowed_mentions']
            self.assertEqual(mentions.users[0].id,10)
            self.assertFalse(mentions.everyone)
            self.assertEqual(len(self.timers.store.list(1,11)),1)
        finally:c.bot.get_guild=old

    async def test_permission_failure_retains_timer(self):
        channel=SimpleNamespace(send=AsyncMock(),permissions_for=lambda _:SimpleNamespace(view_channel=True,send_messages=False))
        guild=SimpleNamespace(me=object(),get_channel=lambda _:channel)
        old=c.bot.get_guild;c.bot.get_guild=lambda _:guild
        try:
            self.timers.store.start(1,10,'test',100,1,now=1)
            await self.timers.tick.coro(self.timers)
            channel.send.assert_not_called()
            self.assertEqual(len(self.timers.store.list(1,10)),1)
        finally:c.bot.get_guild=old

    async def test_slash_group_registration(self):
        group=c.bot.tree.get_command('타이머')
        self.assertEqual({x.name for x in group.commands},{'시작','목록','취소','채널설정','채널해제','도움말'})

if __name__=='__main__':unittest.main()
