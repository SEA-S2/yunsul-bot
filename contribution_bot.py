"""윤 슬 디스코드 납품·기여도 명령어 (기존 예약 게시 스크립트와 독립)."""
import os
import sqlite3
import tempfile
from contextlib import closing
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


@bot.tree.command(name="목표초기화", description="마을원의 개인 목표 설정을 초기화합니다. (관리자)")
@app_commands.guild_only()
async def reset_goal(interaction: discord.Interaction, 마을원: discord.Member):
    if await deny_if_not_manager(interaction):
        return
    with connect() as db:
        result = db.execute(
            "DELETE FROM goals WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, 마을원.id),
        )
        removed = result.rowcount
    if not removed:
        return await reply_error(
            interaction, f"{마을원.mention}은 별도로 설정된 목표가 없어요. 기본 목표는 {DEFAULT_GOAL:,}점입니다."
        )
    await interaction.response.send_message(
        f"✅ {마을원.mention}의 개인 목표를 초기화했어요. 이제 기본 목표 **{DEFAULT_GOAL:,}점**이 적용됩니다. "
        "기존 납품 기록은 유지돼요.",
        ephemeral=True,
    )


@bot.tree.command(name="재료삭제", description="등록된 재료의 점수 설정을 삭제합니다. (관리자)")
@app_commands.guild_only()
async def delete_material(interaction: discord.Interaction, 재료: str):
    if await deny_if_not_manager(interaction):
        return
    name = 재료.strip()
    with connect() as db:
        result = db.execute(
            "DELETE FROM materials WHERE guild_id=? AND name=?",
            (interaction.guild_id, name),
        )
        removed = result.rowcount
    if not removed:
        return await reply_error(interaction, "해당 이름으로 등록된 재료가 없어요.")
    await interaction.response.send_message(
        f"🗑️ **{discord.utils.escape_markdown(name)}** 재료 설정을 삭제했어요. "
        "기존 납품 기록과 이미 적립된 점수는 유지됩니다. 다시 납품하려면 /재료설정이 필요해요.",
        ephemeral=True,
    )


class ResetAllConfirm(discord.ui.Modal, title="윤 슬 전체 데이터 초기화 최종 확인"):
    confirm = discord.ui.TextInput(
        label="정확히 전체초기화 라고 입력하세요",
        placeholder="전체초기화",
        min_length=5,
        max_length=5,
        required=True,
    )

    def __init__(self, guild_id: int, requester_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.requester_id = requester_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.requester_id:
            return await reply_error(interaction, "이 초기화 요청을 실행할 권한이 없어요.")
        if await deny_if_not_manager(interaction):
            return
        if str(self.confirm.value).strip() != "전체초기화":
            return await reply_error(interaction, "확인 문구가 일치하지 않아 아무것도 삭제하지 않았어요.")
        with connect() as db:
            records = db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE guild_id=?", (self.guild_id,),
            ).fetchone()[0]
            material_count = db.execute(
                "SELECT COUNT(*) FROM materials WHERE guild_id=?", (self.guild_id,),
            ).fetchone()[0]
            goal_count = db.execute(
                "SELECT COUNT(*) FROM goals WHERE guild_id=?", (self.guild_id,),
            ).fetchone()[0]
            db.execute("DELETE FROM deliveries WHERE guild_id=?", (self.guild_id,))
            db.execute("DELETE FROM materials WHERE guild_id=?", (self.guild_id,))
            db.execute("DELETE FROM goals WHERE guild_id=?", (self.guild_id,))
        await interaction.response.send_message(
            f"🗑️ 이 서버의 납품 기록 **{records:,}건**, 재료 설정 **{material_count:,}개**, "
            f"개인 목표 설정 **{goal_count:,}개**를 모두 삭제했어요. "
            f"개인 목표는 기본값 {DEFAULT_GOAL:,}점으로 돌아갑니다. 이 작업은 되돌릴 수 없어요.",
            ephemeral=True,
        )


class ResetAllView(discord.ui.View):
    def __init__(self, guild_id: int, requester_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id or interaction.guild_id != self.guild_id:
            await reply_error(interaction, "이 확인 창은 명령어를 실행한 관리자만 사용할 수 있어요.")
            return False
        if await deny_if_not_manager(interaction):
            return False
        return True

    @discord.ui.button(label="삭제 확인 문구 입력", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ResetAllConfirm(self.guild_id, self.requester_id))

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="✅ 전체초기화를 취소했어요. 데이터는 변경되지 않았습니다.", view=self)



@bot.tree.command(name="백업", description="납품 데이터베이스 전체를 백업 파일로 내려받습니다. (관리자)")
@app_commands.guild_only()
async def backup_database(interaction: discord.Interaction):
    if await deny_if_not_manager(interaction):
        return

    # Only the requesting administrator sees the file. Defer within Discord's
    # interaction deadline, since a larger SQLite database may take time.
    await interaction.response.defer(ephemeral=True, thinking=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_UTC")
    filename = f"yunsul_backup_{stamp}.sqlite3"

    try:
        # SQLite online backup produces a consistent snapshot even if writes
        # are occurring. The live database and existing records are untouched.
        with tempfile.TemporaryDirectory(prefix="yunsul_backup_") as tmpdir:
            backup_path = Path(tmpdir) / filename
            with closing(connect()) as source:
                with closing(sqlite3.connect(backup_path)) as destination:
                    source.backup(destination)
            size = backup_path.stat().st_size
            if size > 8 * 1024 * 1024:
                await interaction.followup.send(
                    "⚠️ 백업 파일이 8MB를 넘어 디스코드에 안전하게 첨부할 수 없어요. "
                    "Railway에서 파일을 따로 내려받아 주세요. 원본 데이터는 그대로 유지됩니다.",
                    ephemeral=True,
                )
                return
            with backup_path.open("rb") as backup_fp:
                await interaction.followup.send(
                    "💾 **윤 슬 납품 데이터 백업 완료**\\n"
                    "첨부된 파일을 다운로드해 컴퓨터의 안전한 위치에 보관해 주세요. "
                    "납품 기록·재료 설정·개인 목표·취소 이력이 모두 들어 있어요.\\n"
                    "⚠️ 관리자 전용 파일이므로 타인에게 공유하지 마세요. "
                    "디스코드에서 이 메시지가 사라지기 전에 꼭 저장해 주세요. "
                    "백업 파일만으로 자동 복원되지는 않습니다.",
                    file=discord.File(backup_fp, filename=filename),
                    ephemeral=True,
                )
    except (sqlite3.Error, OSError, discord.HTTPException) as exc:
        print(f"백업 실패: {type(exc).__name__}: {exc}")
        await interaction.followup.send(
            "❌ 백업 파일 생성 또는 전송에 실패했어요. Railway 로그를 확인해 주세요. "
            "기존 데이터는 변경하지 않았습니다.",
            ephemeral=True,
        )


@bot.tree.command(name="전체초기화", description="이 서버의 모든 납품 기록·목표·재료 설정을 삭제합니다. (관리자)")
@app_commands.guild_only()
async def reset_all(interaction: discord.Interaction):
    if await deny_if_not_manager(interaction):
        return
    with connect() as db:
        records = db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE guild_id=?", (interaction.guild_id,),
        ).fetchone()[0]
        materials = db.execute(
            "SELECT COUNT(*) FROM materials WHERE guild_id=?", (interaction.guild_id,),
        ).fetchone()[0]
        goals = db.execute(
            "SELECT COUNT(*) FROM goals WHERE guild_id=?", (interaction.guild_id,),
        ).fetchone()[0]
    await interaction.response.send_message(
        f"⚠️ **윤 슬 전체 초기화**\n"
        f"이 서버의 납품 기록(취소 내역 포함) **{records:,}건**, 재료 **{materials:,}개**, "
        f"개인 목표 **{goals:,}개**를 **영구 삭제**합니다.\n"
        "마을원들의 기여도가 0점이 되고, 목표는 기본 1,000점으로 돌아갑니다. "
        "복구할 수 없으니 정말 필요한 경우에만 진행하세요.",
        view=ResetAllView(interaction.guild_id, interaction.user.id),
        ephemeral=True,
    )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("환경변수 DISCORD_TOKEN이 필요합니다.")
    if not GUILD_ID:
        print("주의: DISCORD_GUILD_ID 미설정 - 전역 명령어 동기화에는 시간이 걸릴 수 있습니다.")
    bot.run(TOKEN)
