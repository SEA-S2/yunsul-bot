"""기린 시세 조회 및 직업별 알림. 기존 납품 DB에 독립 테이블만 추가합니다."""
import asyncio
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

URL = 'https://giraffemc.com/prices.json'
KST = timezone(timedelta(hours=9))
JOBS = {'농부': '농부 상점', '어부': '어부 상점', '광부': '광부 상점',
        '요리사': '요리사 상점', '연금': '연금술사 상점', '목수': '목수 상점'}
ALIASES = {'연금': {'연금 상점', '연금술사 상점'}, '요리사': {'요리 상점', '요리사 상점'}}
log = logging.getLogger(__name__)


def moment(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt


def fresh(data, now=None):
    try:
        age = ((now or datetime.now(timezone.utc)) - moment(data['updatedAt'])).total_seconds()
        return -60 <= age <= 180
    except (KeyError, ValueError, TypeError):
        return False


def matching(job, name):
    return name in ALIASES.get(job, {JOBS[job]})


def fingerprint(items):
    rows = sorted((str(x.get('productId')), str(x.get('price')), str(x.get('basePrice')))
                  for x in items)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def render(data, job, threshold=None):
    items = [x for x in data['items'] if matching(job, x.get('shop'))]
    title = f'📈 {job} 변동상점'
    if not items:
        return [discord.Embed(title=title, description='아직 이 직업의 시세 데이터가 제공되지 않습니다.', color=0xcdb7e9)]
    rising = sorted((x for x in items if x['price'] > x['basePrice']),
                    key=lambda x: x['changePercent'], reverse=True)
    lines = []
    if not fresh(data):
        lines += ['⚠️ 오래된 시세입니다. 현재 판매가는 게임에서 확인해 주세요.', '']
    high = [x for x in rising if threshold is not None and x['changePercent'] >= threshold]
    recommended = [x for x in rising if x not in high]
    for heading, rows in [('🔥 고점', high), ('⭐ 상승 품목', recommended)]:
        if not rows:
            continue
        lines.append(heading)
        for x in rows:
            name = discord.utils.escape_markdown(str(x['name']))
            grade = discord.utils.escape_markdown(str(x.get('grade') or '등급 없음'))
            lines.append(f"• {name} [{grade}] · **{x['price']:,} {x.get('currency', '')}** · +{x['changePercent']:g}%")
        lines.append('')
    if not rising:
        lines.append('현재 기준가보다 상승한 품목이 없습니다.')
    if threshold is None:
        lines.append('※ 고점 기준 미설정 · 기준가보다 오른 품목만 표시합니다.')
    else:
        lines.append(f'※ 고점: 기준가 대비 +{threshold:g}% 이상 (관리자 설정)')
    chunks, chunk = [], ''
    for line in lines:
        if len(chunk) + len(line) > 3400:
            chunks.append(chunk)
            chunk = ''
        chunk += line + '\n'
    if chunk:
        chunks.append(chunk)
    embeds = []
    for n, text in enumerate(chunks):
        e = discord.Embed(title=title + (f' ({n+1}/{len(chunks)})' if len(chunks)>1 else ''), description=text, color=0xcdb7e9)
        e.set_footer(text=f"시세 기준: {data.get('updatedAt', '알 수 없음')} KST · 기린월드 제공")
        embeds.append(e)
    return embeds


class Market:
    def __init__(self, bot, connect, is_manager):
        self.bot, self.connect, self.is_manager = bot, connect, is_manager
        self.data = None
        self.fetched = 0
        self.lock = asyncio.Lock()
        with connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS market_settings (
                guild_id INTEGER NOT NULL, job TEXT NOT NULL, channel_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0, threshold REAL,
                PRIMARY KEY(guild_id,job))''')
            db.execute('''CREATE TABLE IF NOT EXISTS market_events (
                guild_id INTEGER NOT NULL, job TEXT NOT NULL, event_key TEXT NOT NULL,
                PRIMARY KEY(guild_id,job,event_key))''')
            db.execute('''CREATE TABLE IF NOT EXISTS market_baselines (
                job TEXT PRIMARY KEY, digest TEXT NOT NULL, due TEXT)''')
        with connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS market_cleanup (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                PRIMARY KEY(guild_id,channel_id))''')
        self.bot.add_listener(self.clean_message, 'on_message')
        self.register()
        self.poll.start()

    async def clean_message(self, message):
        # 메시지 본문을 읽지 않으므로 Message Content Intent는 필요하지 않습니다.
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if getattr(message, 'interaction_metadata', None) is not None:
            return
        with self.connect() as db:
            enabled = db.execute(
                'SELECT 1 FROM market_cleanup WHERE guild_id=? AND channel_id=?',
                (message.guild.id, message.channel.id),
            ).fetchone()
        if not enabled:
            return
        me = message.guild.me
        if me is None or not message.channel.permissions_for(me).manage_messages:
            return
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning('일반 메시지 삭제 실패: channel=%s', message.channel.id)

    async def fetch(self):
        async with self.lock:
            now = asyncio.get_running_loop().time()
            if self.data is not None and now - self.fetched < 30:
                return self.data
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(URL) as response:
                    response.raise_for_status()
                    data = await response.json()
            if not isinstance(data, dict) or not isinstance(data.get('items'), list) or not isinstance(data.get('shops'), list):
                raise ValueError('시세 데이터 형식 오류')
            moment(data['updatedAt'])
            for x in data['items']:
                if not all(isinstance(x.get(k), (int,float)) for k in ('price','basePrice','changePercent')):
                    raise ValueError('시세 가격 형식 오류')
            self.data, self.fetched = data, now
            return data

    def threshold(self, guild, job):
        with self.connect() as db:
            row = db.execute('SELECT threshold FROM market_settings WHERE guild_id=? AND job=?', (guild,job)).fetchone()
        return row['threshold'] if row else None

    async def lookup(self, interaction, job):
        await interaction.response.defer(thinking=True)
        try:
            data = await self.fetch()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
            if self.data is None:
                await interaction.followup.send('시세 사이트에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.')
                return
            await interaction.followup.send('⚠️ 연결 실패로 마지막으로 받은 시세를 표시합니다.')
            data = dict(self.data)
            data['updatedAt'] = self.data['updatedAt']
        for embed in render(data, job, self.threshold(interaction.guild_id, job)):
            await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def register(self):
        for job in JOBS:
            def make_callback(selected):
                async def callback(interaction: discord.Interaction):
                    await self.lookup(interaction, selected)
                return callback
            command = app_commands.Command(name=job, description=f'{job} 상점의 상승 품목과 시세를 확인합니다.', callback=make_callback(job))
            command.guild_only = True
            self.bot.tree.add_command(command)
        choices = [app_commands.Choice(name=x, value=x) for x in JOBS]

        @self.bot.tree.command(name='시세설정', description='직업별 시세 채널과 갱신 알림을 설정합니다. (관리자)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.choices(직업=choices)
        async def configure(interaction: discord.Interaction, 직업: str, 채널: discord.TextChannel, 알림: bool = False):
            if not self.is_manager(interaction):
                return await interaction.response.send_message('서버 관리 권한이 필요합니다.', ephemeral=True)
            perms = 채널.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                return await interaction.response.send_message('봇에 해당 채널 보기·메시지 보내기·링크 첨부 권한이 필요합니다.', ephemeral=True)
            with self.connect() as db:
                db.execute('''INSERT INTO market_settings(guild_id,job,channel_id,enabled) VALUES(?,?,?,?)
                    ON CONFLICT(guild_id,job) DO UPDATE SET channel_id=excluded.channel_id,enabled=excluded.enabled''',
                    (interaction.guild_id,직업,채널.id,int(알림)))
            await interaction.response.send_message(f"✅ {직업} 시세 채널: {채널.mention} · 알림 {'켜짐' if 알림 else '꺼짐'}\n알림을 켜면 갱신 10분 전 안내와 실제 갱신 후 시세를 게시합니다.", ephemeral=True)

        @self.bot.tree.command(name='시세채팅정리', description='지정 채널의 일반 메시지만 자동 삭제합니다. (관리자)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        async def cleanup(interaction: discord.Interaction, 채널: discord.TextChannel, 사용: bool):
            if not self.is_manager(interaction):
                return await interaction.response.send_message('서버 관리 권한이 필요합니다.', ephemeral=True)
            perms = 채널.permissions_for(interaction.guild.me)
            if 사용 and not (perms.view_channel and perms.manage_messages):
                return await interaction.response.send_message(
                    '봇에 해당 채널 보기·메시지 관리 권한을 허용한 뒤 다시 설정해 주세요.', ephemeral=True)
            with self.connect() as db:
                if 사용:
                    db.execute('INSERT OR IGNORE INTO market_cleanup VALUES(?,?)', (interaction.guild_id, 채널.id))
                else:
                    db.execute('DELETE FROM market_cleanup WHERE guild_id=? AND channel_id=?', (interaction.guild_id, 채널.id))
            await interaction.response.send_message(
                f"✅ {채널.mention} 일반 메시지 자동 삭제 {'켜짐' if 사용 else '꺼짐'}\n"
                '켜진 채널에서는 관리자 메시지·첨부파일도 삭제됩니다. 슬래시 명령어 응답과 봇 메시지는 유지합니다. 기존 메시지는 삭제하지 않습니다.',
                ephemeral=True)

        @self.bot.tree.command(name='시세설정확인', description='직업별 시세 채널과 알림 설정을 확인합니다.')
        @app_commands.guild_only()
        async def show(interaction: discord.Interaction):
            with self.connect() as db:
                rows = db.execute('SELECT * FROM market_settings WHERE guild_id=?',(interaction.guild_id,)).fetchall()
            text = '\n'.join(f"• {r['job']}: <#{r['channel_id']}> · 알림 {'켜짐' if r['enabled'] else '꺼짐'}" for r in rows)
            await interaction.response.send_message(text or '아직 설정된 시세 채널이 없습니다.',ephemeral=True)

        @self.bot.tree.command(name='고점기준', description='확인된 기준가 대비 상승률을 고점 기준으로 설정합니다. (관리자)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.choices(직업=choices)
        async def high(interaction: discord.Interaction, 직업: str, 상승률: app_commands.Range[float,0,10000]):
            if not self.is_manager(interaction):
                return await interaction.response.send_message('서버 관리 권한이 필요합니다.',ephemeral=True)
            with self.connect() as db:
                cur = db.execute('UPDATE market_settings SET threshold=? WHERE guild_id=? AND job=?',(상승률 or None,interaction.guild_id,직업))
            await interaction.response.send_message(('✅ 고점 기준을 변경했습니다. 0은 기준 해제입니다.' if cur.rowcount else '먼저 /시세설정으로 직업 채널을 지정해 주세요.'),ephemeral=True)

    async def emit(self, row, key, text, data=None):
        with self.connect() as db:
            exists = db.execute('SELECT 1 FROM market_events WHERE guild_id=? AND job=? AND event_key=?',(row['guild_id'],row['job'],key)).fetchone()
        if exists:
            return
        channel = self.bot.get_channel(row['channel_id'])
        if channel is None:
            return
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        if data:
            for embed in render(data,row['job'],row['threshold']):
                await channel.send(embed=embed,allowed_mentions=discord.AllowedMentions.none())
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO market_events VALUES(?,?,?)',(row['guild_id'],row['job'],key))

    @tasks.loop(seconds=60)
    async def poll(self):
        try:
            with self.connect() as db:
                settings = db.execute('SELECT * FROM market_settings WHERE enabled=1').fetchall()
            if not settings:
                return
            data = await self.fetch()
            if not fresh(data):
                return  # 오픈 전/사이트 정지 시 오래된 시각으로 알림을 보내지 않습니다.
            now = datetime.now(timezone.utc)
            for job in JOBS:
                shop = next((s for s in data['shops'] if matching(job,s.get('name'))),None)
                if not shop:
                    continue
                due = shop.get('nextChangeAt')
                items = [x for x in data['items'] if matching(job,x.get('shop'))]
                digest = fingerprint(items)
                with self.connect() as db:
                    prior = db.execute('SELECT * FROM market_baselines WHERE job=?',(job,)).fetchone()
                failed = False
                for row in (r for r in settings if r['job']==job):
                    try:
                        if due and 0 < (moment(due)-now).total_seconds() <= 600:
                            await self.emit(row,'pre:'+due,f'🔔 {job} 변동상점 갱신까지 약 {max(1,int((moment(due)-now).total_seconds()/60))}분 남았습니다!')
                        advanced = prior and due and prior['due'] and moment(due)>moment(prior['due']) and now>=moment(prior['due'])
                        if prior and (digest != prior['digest'] or advanced):
                            await self.emit(row,'update:'+str(due)+':'+digest,f'📈 {job} 변동상점 시세가 갱신되었습니다!',data)
                    except (discord.HTTPException, ValueError):
                        failed = True
                        log.exception('시세 알림 실패: %s',job)
                if failed:
                    continue
                with self.connect() as db:
                    db.execute('INSERT INTO market_baselines VALUES(?,?,?) ON CONFLICT(job) DO UPDATE SET digest=excluded.digest,due=excluded.due',(job,digest,due))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, sqlite3.Error):
            log.exception('시세 갱신 확인 실패; 다음 주기에 재시도합니다.')

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()
