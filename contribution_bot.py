"""윤 슬 디스코드 납품·기여도 명령어 (기존 예약 게시 스크립트와 독립)."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or "0")
DB_PATH = Path(os.environ.get("CONTRIBUTION_DB_PATH", "contributions.sqlite3"))
EMBED_COLOR = discord.Color.from_rgb(205, 183, 233)
DEFAULT_GOAL = 1000


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("""CREATE TABLE IF NOT EXISTS materials (
        guild_id INTEGER NOT NULL, name TEXT NOT NULL, weight INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(guild_id, name))""")
    db.execute("""CREATE TABLE IF NOT EXISTS goals (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, goal INTEGER NOT NULL,
        PRIMARY KEY(guild_id, user_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, material TEXT NOT NULL, quantity INTEGER NOT NULL,
        weight INTEGER NOT NULL, points INTEGER NOT NULL,
        moderator_id INTEGER NOT NULL, proof TEXT,
        created_at TEXT NOT NULL, cancelled_at TEXT, cancelled_by INTEGER)""")
    db.commit()
    return db


def summary(db, guild_id, user_id):
    goal_row = db.execute(
        "SELECT goal FROM goals WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    goal = goal_row["goal"] if goal_row else DEFAULT_GOAL
    row = db.execute(
        """SELECT COALESCE(SUM(points),0) AS points, COALESCE(SUM(quantity),0) AS quantity,
                  COUNT(*) AS records FROM deliveries
           WHERE guild_id=? AND user_id=? AND cancelled_at IS NULL""",
        (guild_id, user_id),
    ).fetchone()
    points = row["points"]
    percentage = points * 100 / goal
    if percentage >= 75:
        benefit = "3단계 · 땅 밀기 면제 또는 세금 면제"
    elif percentage >= 50:
        benefit = "2단계 · 세금 10% 할인"
    elif percentage >= 25:
        benefit = "1단계 · 땅 밀기 쉬운 자리 우선 배정"
    else:
        benefit = "아직 달성한 혜택 없음"
    next_step = next((v for v in (25, 50, 75) if percentage < v), None)
    needed = max(0, (goal * next_step + 99) // 100 - points) if next_step else 0
    return goal, points, percentage, benefit, next_step, needed, row["records"]


def manager(interaction):
    return isinstance(interaction.user, discord.Member) and (
        interaction.user.guild_permissions.manage_guild
        or interaction.user.guild_permissions.administrator
    )


async def deny_if_not_manager(interaction):
    if manager(interaction):
        return False
    await interaction.response.send_message("관리자(서버 관리 권한)만 사용할 수 있습니다.", ephemeral=True)
    return True


async def reply_error(interaction, message):
    await interaction.response.send_message(message, ephemeral=True)


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"윤 슬 납품봇 로그인: {bot.user} / guild={GUILD_ID or 'global'}")


@bot.event
async def setup_hook():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
    else:
        synced = await bot.tree.sync()
    print(f"동기화된 명령어: {len(synced)}개")


@bot.tree.command(name="재료설정", description="재료 1개당 기여 점수를 설정합니다. (관리자)")
@app_commands.guild_only()
@app_commands.describe(재료="재료 이름", 점수="1개당 기여 점수 (기본 1점)")
async def material_config(interaction: discord.Interaction, 재료: str, 점수: app_commands.Range[int, 1, 100000]):
    if await deny_if_not_manager(interaction):
        return
    name = 재료.strip()
    if not (1 <= len(name) <= 60):
        return await reply_error(interaction, "재료명은 1~60자로 입력해 주세요.")
    with connect() as db:
        db.execute(
            """INSERT INTO materials(guild_id,name,weight) VALUES(?,?,?)
               ON CONFLICT(guild_id,name) DO UPDATE SET weight=excluded.weight""",
            (interaction.guild_id, name, 점수),
        )
    await interaction.response.send_message(
        f"✅ **{name}** 1개당 **{점수:,}점**으로 설정했어요. 이전 납품 기록은 변경되지 않습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="목표설정", description="마을원의 개인 납품 목표 점수를 설정합니다. (관리자)")
@app_commands.guild_only()
async def goal_config(interaction: discord.Interaction, 마을원: discord.Member, 목표점수: app_commands.Range[int, 1, 100000000]):
    if await deny_if_not_manager(interaction):
        return
    with connect() as db:
        db.execute(
            """INSERT INTO goals(guild_id,user_id,goal) VALUES(?,?,?)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET goal=excluded.goal""",
            (interaction.guild_id, 마을원.id, 목표점수),
        )
    await interaction.response.send_message(
        f"✅ {마을원.mention}의 개인 목표를 **{목표점수:,}점**으로 설정했어요.",
        ephemeral=True,
    )


@bot.tree.command(name="납품등록", description="인증된 납품을 등록합니다. (관리자)")
@app_commands.guild_only()
@app_commands.describe(증빙="인증 메시지 링크 또는 메모 (선택)")
async def register(
    interaction: discord.Interaction, 마을원: discord.Member, 재료: str,
    수량: app_commands.Range[int, 1, 10000000], 증빙: str = "",
):
    if await deny_if_not_manager(interaction):
        return
    name = 재료.strip()
    if not (1 <= len(name) <= 60):
        return await reply_error(interaction, "재료명은 1~60자로 입력해 주세요.")
    with connect() as db:
        mat = db.execute(
            "SELECT weight FROM materials WHERE guild_id=? AND name=?",
            (interaction.guild_id, name),
        ).fetchone()
        if mat is None:
            return await reply_error(interaction, "등록되지 않은 재료예요. 먼저 /재료설정으로 점수를 정해 주세요.")
        weight = mat["weight"]
        points = 수량 * weight
        cur = db.execute(
            """INSERT INTO deliveries
               (guild_id,user_id,material,quantity,weight,points,moderator_id,proof,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (interaction.guild_id, 마을원.id, name, 수량, weight, points,
             interaction.user.id, 증빙[:500], datetime.now(timezone.utc).isoformat()),
        )
        record_id = cur.lastrowid
        goal, earned, pct, benefit, _, _, _ = summary(db, interaction.guild_id, 마을원.id)
    await interaction.response.send_message(
        f"✅ 납품 **#{record_id}** 등록 완료\n"
        f"{마을원.mention} · {name} {수량:,}개 · **+{points:,}점**\n"
        f"현재 **{earned:,}/{goal:,}점 ({pct:.1f}%)** · {benefit}"
    )


@bot.tree.command(name="납품취소", description="잘못 등록한 납품 기록을 취소합니다. (관리자)")
@app_commands.guild_only()
async def cancel(interaction: discord.Interaction, 기록번호: app_commands.Range[int, 1, 2147483647]):
    if await deny_if_not_manager(interaction):
        return
    with connect() as db:
        row = db.execute(
            "SELECT user_id,points FROM deliveries WHERE id=? AND guild_id=? AND cancelled_at IS NULL",
            (기록번호, interaction.guild_id),
        ).fetchone()
        if not row:
            return await reply_error(interaction, "해당 서버에 취소 가능한 납품 기록이 없어요.")
        db.execute(
            "UPDATE deliveries SET cancelled_at=?,cancelled_by=? WHERE id=? AND guild_id=?",
            (datetime.now(timezone.utc).isoformat(), interaction.user.id, 기록번호, interaction.guild_id),
        )
    await interaction.response.send_message(
        f"🗑️ 납품 **#{기록번호}** 취소 완료 · <@{row['user_id']}>의 {row['points']:,}점을 차감했어요.",
        allowed_mentions=discord.AllowedMentions.none(),
        ephemeral=True,
    )


@bot.tree.command(name="기여도", description="본인 또는 지정한 마을원의 기여도를 확인합니다.")
@app_commands.guild_only()
async def progress(interaction: discord.Interaction, 마을원: discord.Member | None = None):
    target = 마을원 or interaction.user
    with connect() as db:
        goal, points, pct, benefit, next_step, needed, count = summary(db, interaction.guild_id, target.id)
    bar = "▰" * min(10, int(min(pct, 100) // 10)) + "▱" * (10 - min(10, int(min(pct, 100) // 10)))
    embed = discord.Embed(title="🌷 윤 슬 납품 기여도", color=EMBED_COLOR)
    embed.description = (
        f"**마을원** {target.mention}\n"
        f"**기여도** {points:,} / {goal:,}점 · **{pct:.1f}%**\n"
        f"{bar}\n"
        f"**혜택** {benefit}\n"
        + (f"**다음 단계 ({next_step}%)**까지 {needed:,}점" if next_step else "🎉 최고 단계 달성!")
    )
    embed.set_footer(text=f"납품 {count}건 · 혜택의 실제 적용은 운영진 확인 후 진행")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="납품순위", description="개인 목표 달성률 기준 상위 10명을 표시합니다.")
@app_commands.guild_only()
async def ranking(interaction: discord.Interaction):
    with connect() as db:
        rows = db.execute(
            """SELECT user_id,SUM(points) AS total FROM deliveries
               WHERE guild_id=? AND cancelled_at IS NULL
               GROUP BY user_id""", (interaction.guild_id,),
        ).fetchall()
        results = [(user_id, *summary(db, interaction.guild_id, user_id)[:3])
                   for user_id in (r["user_id"] for r in rows)]
    results.sort(key=lambda x: (-x[3], -x[2], x[0]))
    lines = [f"**{i}.** <@{user_id}> · {pct:.1f}% ({points:,}/{goal:,}점)"
             for i, (user_id, goal, points, pct) in enumerate(results[:10], 1)]
    await interaction.response.send_message(
        "🏆 **윤 슬 기여도 TOP 10**\n" + ("\n".join(lines) if lines else "아직 납품 기록이 없어요."),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="납품기록", description="마을원의 최근 납품 10건을 확인합니다.")
@app_commands.guild_only()
async def history(interaction: discord.Interaction, 마을원: discord.Member | None = None):
    target = 마을원 or interaction.user
    with connect() as db:
        rows = db.execute(
            """SELECT id,material,quantity,points,created_at FROM deliveries
               WHERE guild_id=? AND user_id=? AND cancelled_at IS NULL
               ORDER BY id DESC LIMIT 10""",
            (interaction.guild_id, target.id),
        ).fetchall()
    lines = [f"#{r['id']} · {discord.utils.escape_markdown(r['material'])} "
             f"{r['quantity']:,}개 · +{r['points']:,}점"
             for r in rows]
    await interaction.response.send_message(
        f"📦 **{target.display_name} 납품 기록**\n" +
        ("\n".join(lines) if lines else "등록된 납품 기록이 없어요."),
        ephemeral=True,
    )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("환경변수 DISCORD_TOKEN이 필요합니다.")
    if not GUILD_ID:
        print("주의: DISCORD_GUILD_ID 미설정 - 전역 명령어 동기화에는 시간이 걸릴 수 있습니다.")
    bot.run(TOKEN)
