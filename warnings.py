"""윤 슬 경고 관리: 누적 경고와 최초 부여일 기준 30일마다 1회 차감."""
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

log = logging.getLogger(__name__)
PERIOD = 30 * 24 * 60 * 60
MANAGER_ROLE_ID = int(os.getenv("WARNING_MANAGER_ROLE_ID", "1295038906039009421"))
MAYOR_ROLE_ID = int(os.getenv("WARNING_MAYOR_ROLE_ID", "1470511754021703730"))
LOG_CHANNEL_ID = int(os.getenv("WARNING_LOG_CHANNEL_ID", "0") or "0")


def display_time(timestamp):
    return f"<t:{int(timestamp)}:F>" if timestamp is not None else "없음"


class WarningStore:
    def __init__(self, connect):
        self.connect = connect
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS warning_state (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
                next_due REAL,
                PRIMARY KEY(guild_id,user_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS warning_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL, delta INTEGER NOT NULL,
                event_type TEXT NOT NULL, reason TEXT NOT NULL,
                created_at REAL NOT NULL, balance INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS warning_events_lookup ON warning_events(guild_id,user_id,id)")

    @staticmethod
    def _decay(db, now, guild_id=None):
        """Process every elapsed 30-day interval atomically. Each cycle removes one warning."""
        if guild_id is None:
            rows = db.execute(
                "SELECT * FROM warning_state WHERE count>0 AND next_due<=? ORDER BY next_due",
                (now,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM warning_state WHERE guild_id=? AND count>0 AND next_due<=?",
                (guild_id, now),
            ).fetchall()
        changes = []
        for row in rows:
            count = row["count"]
            due = row["next_due"]
            while count > 0 and due is not None and due <= now:
                count -= 1
                db.execute("""INSERT INTO warning_events
                    (guild_id,user_id,actor_id,delta,event_type,reason,created_at,balance)
                    VALUES(?,?,0,-1,'decay','30일 자동 차감',?,?)""",
                    (row["guild_id"], row["user_id"], due, count))
                changes.append((row["guild_id"], row["user_id"], count, due))
                due = due + PERIOD if count > 0 else None
            db.execute(
                "UPDATE warning_state SET count=?,next_due=? WHERE guild_id=? AND user_id=?",
                (count, due, row["guild_id"], row["user_id"]))
        return changes

    def decay(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._decay(db, now)

    def change(self, guild, user, actor, delta, reason, event_type, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._decay(db, now, guild)
            row = db.execute(
                "SELECT count,next_due FROM warning_state WHERE guild_id=? AND user_id=?",
                (guild,user)).fetchone()
            previous = row["count"] if row else 0
            if delta < 0 and previous < -delta:
                raise ValueError(f"현재 경고는 {previous}회라 {abs(delta)}회를 차감할 수 없어요.")
            new_count = previous + delta
            if new_count == 0:
                due = None
            elif previous == 0:
                due = now + PERIOD
            else:
                due = row["next_due"]  # 새 경고에도 기존 차감일 유지
            db.execute("""INSERT INTO warning_state(guild_id,user_id,count,next_due)
                VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id)
                DO UPDATE SET count=excluded.count,next_due=excluded.next_due""",
                (guild,user,new_count,due))
            db.execute("""INSERT INTO warning_events
                (guild_id,user_id,actor_id,delta,event_type,reason,created_at,balance)
                VALUES(?,?,?,?,?,?,?,?)""",
                (guild,user,actor,delta,event_type,reason,now,new_count))
            return new_count, due

    def get(self, guild, user, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._decay(db, now, guild)
            row = db.execute(
                "SELECT count,next_due FROM warning_state WHERE guild_id=? AND user_id=?",
                (guild,user)).fetchone()
            return (row["count"],row["next_due"]) if row else (0,None)

    def list_active(self, guild, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._decay(db, now, guild)
            return db.execute(
                "SELECT * FROM warning_state WHERE guild_id=? AND count>0 ORDER BY count DESC,next_due LIMIT 20",
                (guild,)).fetchall()

    def history(self, guild, user, limit=10):
        with self.connect() as db:
            return db.execute("""SELECT * FROM warning_events
                WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?""",
                (guild,user,limit)).fetchall()


class Warnings:
    def __init__(self, bot, connect):
        self.bot = bot
        self.store = WarningStore(connect)
        self.register()
        self.tick.start()

    @staticmethod
    def is_mayor(interaction):
        user = interaction.user
        if interaction.guild and user.id == interaction.guild.owner_id:
            return True
        return isinstance(user, discord.Member) and any(
            role.id == MAYOR_ROLE_ID for role in user.roles)

    @classmethod
    def is_manager(cls, interaction):
        user = interaction.user
        return cls.is_mayor(interaction) or (
            isinstance(user, discord.Member)
            and any(role.id == MANAGER_ROLE_ID for role in user.roles))

    async def send_log(self, guild_id, content):
        if not LOG_CHANNEL_ID:
            return
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(LOG_CHANNEL_ID) if guild else None
        if channel is None:
            return
        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("경고 로그 전송 실패: guild=%s",guild_id)

    def register(self):
        group = app_commands.Group(name="경고", description="윤 슬 경고 부여·조회·30일 자동 차감")

        @group.command(name="부여", description="마을원에게 경고를 부여합니다. 관리자 1회, 마을장은 여러 회 가능.")
        @app_commands.guild_only()
        @app_commands.describe(대상="경고를 받을 마을원", 사유="경고 사유", 횟수="부여할 경고 횟수 (기본 1)")
        async def grant(interaction: discord.Interaction, 대상: discord.Member, 사유: str,
                        횟수: app_commands.Range[int, 1, 10] = 1):
            if not self.is_manager(interaction):
                return await interaction.response.send_message("관리자 역할 이상만 사용할 수 있어요.",ephemeral=True)
            if not self.is_mayor(interaction):
                existing, _ = self.store.get(interaction.guild_id, 대상.id)
                if 횟수 > 1 or existing >= 1:
                    return await interaction.response.send_message(
                        "누적 경고가 2회 이상이 되는 경우에는 마을장의 사전 승인이 필요해요.", ephemeral=True)
            if 대상.bot or 대상.id == interaction.guild.owner_id:
                return await interaction.response.send_message("이 대상에게는 경고를 부여할 수 없어요.",ephemeral=True)
            사유 = 사유.strip()
            if not 1 <= len(사유) <= 400:
                return await interaction.response.send_message("사유는 1~400자로 입력해 주세요.",ephemeral=True)
            count, due = self.store.change(interaction.guild_id,대상.id,interaction.user.id,
                                           횟수,사유,"grant")
            await interaction.response.send_message(
                f"✅ {대상.mention} 경고 **+{횟수}회** (누적 **{count}회**)\n"
                f"사유: {discord.utils.escape_markdown(사유)}\n다음 차감일: {display_time(due)}"
                + ("\n⚠️ 3회 이상 누적: 마을장이 추방 여부를 판단해 주세요. 자동 추방하지 않습니다."
                   if count >= 3 else ""), ephemeral=True)
            await self.send_log(interaction.guild_id,
                f"⚠️ 경고 부여 | 대상: <@{대상.id}> | +{횟수} (누적 {count}) | "
                f"담당: <@{interaction.user.id}> | 사유: {discord.utils.escape_mentions(사유)}")

        @group.command(name="조회", description="자신 또는 다른 마을원의 누적 경고·차감일을 확인합니다.")
        @app_commands.guild_only()
        async def view(interaction: discord.Interaction, 대상: discord.Member = None):
            대상 = 대상 or interaction.user
            if 대상.id != interaction.user.id and not self.is_manager(interaction):
                return await interaction.response.send_message("다른 사람의 경고는 관리자만 조회할 수 있어요.",ephemeral=True)
            count,due = self.store.get(interaction.guild_id,대상.id)
            await interaction.response.send_message(
                f"📋 {대상.mention} | 누적 경고 **{count}회**\n다음 차감일: {display_time(due)}",
                ephemeral=True)

        @group.command(name="취소", description="마을장 전용: 누적 경고를 취소·차감합니다.")
        @app_commands.guild_only()
        @app_commands.describe(대상="대상 마을원", 횟수="취소할 경고 횟수", 사유="취소 사유")
        async def revoke(interaction: discord.Interaction, 대상: discord.Member,
                         횟수: app_commands.Range[int,1,10], 사유: str):
            if not self.is_mayor(interaction):
                return await interaction.response.send_message("경고 취소는 마을장 전용이에요.",ephemeral=True)
            사유 = 사유.strip()
            if not 1 <= len(사유) <= 400:
                return await interaction.response.send_message("사유는 1~400자로 입력해 주세요.",ephemeral=True)
            try:
                count,due = self.store.change(interaction.guild_id,대상.id,interaction.user.id,
                                              -횟수,사유,"revoke")
            except ValueError as exc:
                return await interaction.response.send_message(str(exc),ephemeral=True)
            await interaction.response.send_message(
                f"✅ {대상.mention} 경고 **-{횟수}회** (누적 **{count}회**)\n"
                f"다음 차감일: {display_time(due)}",ephemeral=True)
            await self.send_log(interaction.guild_id,
                f"✅ 경고 취소 | 대상: <@{대상.id}> | -{횟수} (누적 {count}) | "
                f"담당: <@{interaction.user.id}> | 사유: {discord.utils.escape_mentions(사유)}")

        @group.command(name="기록", description="경고 부여·취소·자동 차감 이력을 확인합니다.")
        @app_commands.guild_only()
        async def history(interaction: discord.Interaction, 대상: discord.Member = None):
            대상 = 대상 or interaction.user
            if 대상.id != interaction.user.id and not self.is_manager(interaction):
                return await interaction.response.send_message("다른 사람의 기록은 관리자만 확인할 수 있어요.",ephemeral=True)
            self.store.get(interaction.guild_id,대상.id)
            rows = self.store.history(interaction.guild_id,대상.id,10)
            labels = {"grant":"부여","revoke":"취소","decay":"자동 차감"}
            lines = [f"<t:{int(row['created_at'])}:d> {labels.get(row['event_type'],'변경')} "
                     f"{row['delta']:+d} (누적 {row['balance']}) · "
                     f"{discord.utils.escape_markdown(row['reason'][:80])}" for row in rows]
            await interaction.response.send_message(
                f"📜 {대상.mention} 경고 기록\n" + ("\n".join(lines) if lines else "기록이 없어요."),
                ephemeral=True)

        @group.command(name="목록", description="관리자 전용: 경고가 있는 마을원 목록")
        @app_commands.guild_only()
        async def listing(interaction: discord.Interaction):
            if not self.is_manager(interaction):
                return await interaction.response.send_message("관리자 역할 이상만 확인할 수 있어요.",ephemeral=True)
            rows = self.store.list_active(interaction.guild_id)
            lines = [f"<@{row['user_id']}> · {row['count']}회 · 다음 차감 {display_time(row['next_due'])}"
                     for row in rows]
            await interaction.response.send_message(
                "📋 **누적 경고 현황 (최대 20명)**\n" + ("\n".join(lines) if lines else "현재 경고가 없어요."),
                ephemeral=True)

        self.bot.tree.add_command(group)

    @tasks.loop(minutes=1)
    async def tick(self):
        try:
            changes = self.store.decay()
            for guild,user,count,due in changes:
                await self.send_log(guild,
                    f"🌷 경고 자동 차감 | 대상: <@{user}> | -1 (누적 {count}) | "
                    f"차감 기준: {display_time(due)}")
        except sqlite3.Error:
            log.exception("경고 자동 차감 데이터베이스 오류: 다음 실행 때 재시도")

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()
