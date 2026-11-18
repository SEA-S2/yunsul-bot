"""윤 슬 개인 타이머: 슬래시 입력, 영구 저장, 채널 멘션 알림."""
import logging
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import tasks

log = logging.getLogger(__name__)
WIKI = 'https://giraffemc.com/wiki/'
# 2026-11-18 위키 확인. 아래 시간은 버프/숙련도 차감 전 기준 또는 근삿값입니다.
PRESETS = {
    '농사': (None, '작물·제철별로 달라 고정 시간 미설정. 실제 남은 시간을 입력하세요.', 'life/proficiency/farmer/crops'),
    '요리': (86, '일반 요리 1회 기본 1분 26초. 숙련도·손질 등의 시간 감소 전입니다.', 'life/proficiency/cook'),
    '간단요리': (30, '감자 수프·호박죽 등 위키에 30초로 표시된 요리 기본 시간입니다.', 'life/proficiency/cook'),
    '연금': (56, '1~3단계 비약 한 병 약 56초. 숙련도·분쇄 등의 시간 감소 전입니다.', 'life/proficiency/alchemist'),
    '바닐라비약': (30, '바닐라 비약 한 병 기본 약 30초입니다.', 'life/proficiency/alchemist'),
    '광부화로': (None, '위키에서 일괄 시간을 확인하지 못했습니다. 실제 남은 시간을 입력하세요.', 'life/proficiency/miner'),
    '떡밥': (300, '한 번 뿌린 떡밥은 5분. 중첩해서 뿌렸다면 총 남은 시간을 입력하세요.', 'life/proficiency/fisher/groundbait'),
    '구리스프링클러': (720, '물을 가득 채웠을 때 약 12분입니다.', 'life/proficiency/farmer/crops'),
    '강철스프링클러': (1080, '물을 가득 채웠을 때 약 18분입니다.', 'life/proficiency/farmer/crops'),
    '황금스프링클러': (1800, '물을 가득 채웠을 때 약 30분입니다.', 'life/proficiency/farmer/crops'),
    '네더스프링클러': (2700, '물을 가득 채웠을 때 약 45분입니다.', 'life/proficiency/farmer/crops'),
    '개척지해산물': (120, '채집한 자리의 재생성 약 2분입니다.', 'life/frontier'),
    '개척지광물': (450, '채집한 자리의 재생성 약 7분 30초입니다.', 'life/frontier'),
    '개척지나무': (150, '쓰러뜨린 자리의 재성장 약 2분 30초입니다.', 'life/frontier'),
    '개척지향신료': (150, '채집한 자리의 재생성 약 2분 30초입니다.', 'life/frontier'),
    '개척지약초': (300, '채집한 자리의 재생성 약 5분입니다.', 'life/frontier'),
    '직접설정': (None, '알림 이름과 시간을 직접 정할 수 있습니다.', ''),
}


def duration(kind, minutes, seconds):
    if kind not in PRESETS:
        raise ValueError('알 수 없는 타이머입니다.')
    if minutes is None and seconds == 0:
        value = PRESETS[kind][0]
        if value is None:
            raise ValueError('이 타이머는 고정 시간이 없습니다. 분 또는 초를 입력해 주세요.')
    else:
        value = (minutes or 0) * 60 + seconds
    if not 1 <= value <= 604800:
        raise ValueError('시간은 1초 이상 7일 이하로 입력해 주세요.')
    return value


class TimerStore:
    def __init__(self, connect):
        self.connect = connect
        with connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS personal_timers (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                channel_id INTEGER NOT NULL, due REAL NOT NULL, created REAL NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(guild_id,user_id,name))''')
            db.execute('''CREATE TABLE IF NOT EXISTS timer_channels (
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)''')

    def start(self, guild, user, name, channel, seconds, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            exists = db.execute('SELECT 1 FROM personal_timers WHERE guild_id=? AND user_id=? AND name=?',(guild,user,name)).fetchone()
            count = db.execute('SELECT COUNT(*) FROM personal_timers WHERE guild_id=? AND user_id=?',(guild,user)).fetchone()[0]
            if not exists and count >= 10:
                raise ValueError('동시 실행은 10개까지 가능합니다. /타이머 취소 후 다시 시작해 주세요.')
            db.execute('''INSERT INTO personal_timers VALUES(?,?,?,?,?,?,1)
                ON CONFLICT(guild_id,user_id,name) DO UPDATE SET channel_id=excluded.channel_id,
                due=excluded.due,created=excluded.created,version=personal_timers.version+1''',
                (guild,user,name,channel,now+seconds,now))
        return now+seconds, bool(exists)

    def cancel(self, guild, user, name):
        with self.connect() as db:
            return db.execute('DELETE FROM personal_timers WHERE guild_id=? AND user_id=? AND name=?',(guild,user,name)).rowcount

    def list(self, guild, user):
        with self.connect() as db:
            return db.execute('SELECT * FROM personal_timers WHERE guild_id=? AND user_id=? ORDER BY due',(guild,user)).fetchall()


class Timers:
    def __init__(self, bot, connect, manager):
        self.bot, self.connect, self.manager = bot, connect, manager
        self.store = TimerStore(connect)
        self.register()
        self.tick.start()

    def register(self):
        group = app_commands.Group(name='타이머', description='기린월드 개인 타이머 · 완료 시 채널에서 본인 멘션', guild_only=True)
        choices = [app_commands.Choice(name=k, value=k) for k in PRESETS]

        @group.command(name='시작', description='타이머 시작. 분/초를 입력하면 기본 시간 대신 사용합니다.')
        @app_commands.choices(종류=choices)
        @app_commands.describe(분='실제 남은 시간 또는 원하는 시간. 입력하면 기본 시간 대신 적용', 초='추가 초 (0~59)', 이름='구분할 이름. 같은 이름으로 다시 시작하면 초기화')
        async def start(interaction: discord.Interaction, 종류: str,
                        분: app_commands.Range[int,0,10080] = None,
                        초: app_commands.Range[int,0,59] = 0, 이름: str = ''):
            try:
                seconds = duration(종류,분,초)
                name = 이름.strip() or 종류
                if not 1 <= len(name) <= 40:
                    raise ValueError('이름은 1~40자로 입력해 주세요.')
            except ValueError as exc:
                return await interaction.response.send_message(str(exc), ephemeral=True)
            with self.connect() as db:
                setting = db.execute('SELECT channel_id FROM timer_channels WHERE guild_id=?',(interaction.guild_id,)).fetchone()
            channel = interaction.guild.get_channel(setting['channel_id']) if setting else interaction.channel
            if not isinstance(channel,discord.TextChannel):
                return await interaction.response.send_message('일반 텍스트 채널에서 실행하거나 /타이머 채널설정으로 알림 채널을 지정해 주세요.',ephemeral=True)
            user_perms=channel.permissions_for(interaction.user)
            bot_perms=channel.permissions_for(interaction.guild.me)
            if not user_perms.view_channel:
                return await interaction.response.send_message('타이머 알림 채널을 볼 권한이 없습니다.',ephemeral=True)
            if not (bot_perms.view_channel and bot_perms.send_messages):
                return await interaction.response.send_message('봇에 알림 채널 보기·메시지 보내기 권한이 필요합니다.',ephemeral=True)
            try:
                due, replaced = self.store.start(interaction.guild_id,interaction.user.id,name,channel.id,seconds)
            except ValueError as exc:
                return await interaction.response.send_message(str(exc),ephemeral=True)
            note = PRESETS[종류][1]
            if 분 is not None or 초:
                note = '직접 입력한 시간을 적용했습니다.'
            safe_name=discord.utils.escape_markdown(name)
            await interaction.response.send_message(
                f"⏰ **{safe_name}** {'다시 시작' if replaced else '시작'} · {seconds//60}분 {seconds%60}초\n"
                f"완료: <t:{int(due)}:R> · 알림 채널: {channel.mention}\n{note}\n"
                '게임 상태와 자동 연동하지 않습니다. 완료 시 한 번만 알립니다.',ephemeral=True)

        @group.command(name='목록', description='내 타이머와 남은 시간을 확인합니다.')
        async def listing(interaction: discord.Interaction):
            rows=self.store.list(interaction.guild_id,interaction.user.id)
            text='\n'.join(f"• **{discord.utils.escape_markdown(r['name'])}** · <t:{int(r['due'])}:R> · <#{r['channel_id']}>" for r in rows)
            await interaction.response.send_message(text or '현재 실행 중인 타이머가 없습니다.',ephemeral=True)

        @group.command(name='취소', description='내 타이머를 이름으로 취소합니다.')
        async def cancel(interaction: discord.Interaction, 이름: str):
            changed=self.store.cancel(interaction.guild_id,interaction.user.id,이름.strip())
            await interaction.response.send_message('✅ 타이머를 취소했습니다.' if changed else '해당 이름의 내 타이머가 없습니다. /타이머 목록에서 확인해 주세요.',ephemeral=True)

        @group.command(name='채널설정', description='완료 알림 채널을 지정합니다. 미설정이면 실행 채널에 알립니다. (관리자)')
        async def channel(interaction: discord.Interaction, 채널: discord.TextChannel):
            if not self.manager(interaction):
                return await interaction.response.send_message('서버 관리 권한이 필요합니다.',ephemeral=True)
            perms=채널.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages):
                return await interaction.response.send_message('봇에 채널 보기·메시지 보내기 권한을 주세요.',ephemeral=True)
            with self.connect() as db:
                db.execute('INSERT INTO timer_channels VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id',(interaction.guild_id,채널.id))
            await interaction.response.send_message(f'✅ 새 타이머의 알림 채널: {채널.mention}\n이미 시작한 타이머의 채널은 유지됩니다.',ephemeral=True)

        @group.command(name='채널해제', description='알림 채널 지정을 해제합니다. 이후 실행한 채널에 알립니다. (관리자)')
        async def clear(interaction: discord.Interaction):
            if not self.manager(interaction):
                return await interaction.response.send_message('서버 관리 권한이 필요합니다.',ephemeral=True)
            with self.connect() as db:
                db.execute('DELETE FROM timer_channels WHERE guild_id=?',(interaction.guild_id,))
            await interaction.response.send_message('✅ 이후 시작하는 타이머는 실행한 채널에 알립니다.',ephemeral=True)

        @group.command(name='도움말', description='기린 위키 기준 시간과 사용법을 확인합니다.')
        async def help_(interaction: discord.Interaction):
            lines=['**⏰ 윤 슬 개인 타이머**',
                   '`/타이머 시작` → 종류 선택 → 필요하면 분·초·이름 입력',
                   '`/타이머 목록` · `/타이머 취소 이름:…`',
                   '같은 이름은 재시작, 다른 이름은 별도 실행 (1인 최대 10개).',
                   '농사는 분·초 직접 입력이 필요합니다. 예: `/타이머 시작 종류:농사 분:16`은 사용자가 16분을 지정하는 예시이며 기린 고정 시간이 아닙니다.',
                   '요리·연금은 1회 기본 시간으로 실제 버프·숙련도·대기열 시간을 반영하지 않습니다.',
                   '스프링클러는 가득 채운 기준, 개척지 재생성은 근삿값입니다.',
                   '디스코드 타이머는 로그아웃 중에도 흐릅니다. 게임 제작 정지와 자동 동기화되지 않습니다.',
                   '동글의 호감도 6시간·다이버 1시간은 기린 근거를 확인하지 못해 넣지 않았습니다.','']
            for name,(seconds,note,path) in PRESETS.items():
                if seconds:
                    lines.append(f'• {name}: {seconds//60}분 {seconds%60}초')
            lines+=['',f'[농사 위키]({WIKI}life/proficiency/farmer/crops) · [요리 위키]({WIKI}life/proficiency/cook) · [연금 위키]({WIKI}life/proficiency/alchemist) · [개척지 위키]({WIKI}life/frontier)']
            await interaction.response.send_message('\n'.join(lines),ephemeral=True)
        self.bot.tree.add_command(group)

    @tasks.loop(seconds=5)
    async def tick(self):
        try:
            with self.connect() as db:
                rows=db.execute('SELECT * FROM personal_timers WHERE due<=? ORDER BY due LIMIT 50',(time.time(),)).fetchall()
            for row in rows:
                guild=self.bot.get_guild(row['guild_id'])
                if guild is None:
                    continue
                channel=guild.get_channel(row['channel_id'])
                if channel is None:
                    continue
                # 권한을 잃어도 기록은 유지하고 권한 복구 후 알립니다.
                perms=channel.permissions_for(guild.me)
                if not (perms.view_channel and perms.send_messages):
                    continue
                try:
                    delay = '（봇 중단 등으로 늦게 전달된 알림）' if time.time()-row['due']>60 else ''
                    name=discord.utils.escape_markdown(row['name'])
                    await channel.send(f"<@{row['user_id']}> ⏰ **{name}** 설정한 시간이 됐어요! {delay}\n게임에서 상태를 확인해 주세요.",
                        allowed_mentions=discord.AllowedMentions(everyone=False,roles=False,users=[discord.Object(id=row['user_id'])]))
                except discord.HTTPException:
                    log.warning('타이머 알림 실패: channel=%s',row['channel_id'])
                    continue
                with self.connect() as db:
                    db.execute('DELETE FROM personal_timers WHERE guild_id=? AND user_id=? AND name=? AND version=? AND due=?',
                               (row['guild_id'],row['user_id'],row['name'],row['version'],row['due']))
        except sqlite3.Error:
            log.exception('타이머 저장소 오류; 다음 주기에 재시도합니다.')

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()
