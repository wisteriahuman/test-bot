import discord
from discord.ext import commands
from discord import app_commands
import os
from dotenv import load_dotenv
import aiohttp
import asyncio
import hashlib
import pathlib
from bs4 import BeautifulSoup
import html
import re

from markdownify import markdownify as md


def _find_contest_panel(soup: BeautifulSoup):
    """『直近のコンテストの告知』パネルを探す。見出しテキストやidを手がかりに柔軟に探索する。"""
    for div in soup.find_all("div"):
        classes = div.get("class") or []
        if "panel" in classes:

            heading = div.find(
                lambda tag: tag.name in ("h1", "h2", "h3")
                and tag.get_text()
                and "直近のコンテストの告知" in tag.get_text()
            )
            if heading:
                return div

    panel = soup.find("div", id="contest-table-upcoming")
    if panel:
        return panel

    text_node = soup.find(string=lambda s: s and "直近のコンテストの告知" in s)
    if text_node and hasattr(text_node, "parent"):
        return text_node.parent

    return None


load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
client = bot

ATCODER_URL = os.getenv("ATCODER_URL", "https://atcoder.jp/home?lang=ja")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
LAST_HASH_FILE = pathlib.Path(__file__).parent / ".last_atcoder_hash"
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID")
SEND_LATEST_ON_STARTUP = os.getenv("SEND_LATEST_ON_STARTUP", "false").lower() in (
    "1",
    "true",
    "yes",
)
CONTESTS_URL = os.getenv("CONTESTS_URL", "https://atcoder.jp/contests/?lang=ja")
# 即時反映用（任意）：開発用ギルドIDを指定するとギルド単位で同期して即時反映
GUILD_ID = os.getenv("GUILD_ID")
GUILD_OBJ = (
    discord.Object(id=int(GUILD_ID)) if GUILD_ID and GUILD_ID.isdigit() else None
)

SERIES_ALIASES = {
    "ABC": "abc",
    "ARC": "arc",
    "AGC": "agc",
    "AHC": "ahc",
}

ABC_ROLE_ID = os.getenv("ABC_ROLE_ID")


def _extract_contest_slug(url: str) -> str:
    """contest URL からスラッグ (abc420 等) を抽出"""
    if not url:
        return ""
    m = re.search(r"/contests/([a-zA-Z0-9_\-]+)/?", url)
    return m.group(1).lower() if m else ""


def _role_mention_for_contest(url: str) -> str:
    """コンテストURLからシリーズを判定し、該当すればロールメンション文字列を返す"""
    slug = _extract_contest_slug(url)
    if slug.startswith("abc") and ABC_ROLE_ID and ABC_ROLE_ID.isdigit():
        return f"<@&{ABC_ROLE_ID}> "
    return ""


_ALLOWED_CHANNEL_IDS_ENV = os.getenv("ALLOWED_CHANNEL_IDS", "").strip()
ALLOWED_CHANNEL_IDS = {
    int(x)
    for x in [p.strip() for p in _ALLOWED_CHANNEL_IDS_ENV.split(",") if p.strip()]
    if x.isdigit()
}


@client.event
async def on_ready():
    try:
        if GUILD_OBJ:
            client.tree.copy_global_to(guild=GUILD_OBJ)
            await client.tree.sync(guild=GUILD_OBJ)
            print(f"Slash commands synced for guild {GUILD_ID}")
        else:
            await client.tree.sync()
            print("Slash commands synced (global)")
    except Exception as e:
        print("Slash command sync failed:", e)

    if getattr(client, "_atcoder_tasks_started", False):
        return
    client._atcoder_tasks_started = True

    print(f"{client.user.name}がログインしました")
    print(f"Bot ID: {client.user.id}")
    print("------")
    if SEND_LATEST_ON_STARTUP:
        client.loop.create_task(send_saved_post_on_startup())
    client.loop.create_task(check_atcoder_loop())


# @client.tree.command(name="recent_contest", description="直近のコンテストを告知します")
# async def slash_latest_contest(interaction: discord.Interaction):

#     if ALLOWED_CHANNEL_IDS:
#         ch_id = getattr(interaction.channel, "id", None)
#         parent_id = getattr(interaction.channel, "parent_id", None)
#         if (ch_id not in ALLOWED_CHANNEL_IDS) and (
#             parent_id not in ALLOWED_CHANNEL_IDS
#         ):
#             await interaction.response.send_message(
#                 "このチャンネルでは使用できません。", ephemeral=True
#             )
#             return
#     await interaction.response.defer(thinking=True)
#     await send_latest_announcements(interaction.channel)
#     await interaction.followup.send("送信しました。", ephemeral=True)


@client.tree.command(
    name="contest-info", description="直近のコンテスト告知を送ります（abc/arc/agc/ahc）"
)
@app_commands.describe(series="abc / arc / agc / ahc のいずれか")
async def slash_latest_series(interaction: discord.Interaction, series: str):
    key = series.upper()
    sp = SERIES_ALIASES.get(key)
    if not sp:
        await interaction.response.send_message(
            "シリーズは abc/arc/agc/ahc から指定してください。", ephemeral=True
        )
        return
    if ALLOWED_CHANNEL_IDS:
        ch_id = getattr(interaction.channel, "id", None)
        parent_id = getattr(interaction.channel, "parent_id", None)
        if (ch_id not in ALLOWED_CHANNEL_IDS) and (
            parent_id not in ALLOWED_CHANNEL_IDS
        ):
            await interaction.response.send_message(
                "このチャンネルでは使用できません。", ephemeral=True
            )
            return
    await interaction.response.defer(thinking=True)
    await send_series_announcement(sp, interaction.channel)
    await interaction.followup.send(
        f"{series.upper()} の告知を送信しました。", ephemeral=True
    )


# @client.tree.command(name="ping", description="pingを返します")
# async def slash_ping(interaction: discord.Interaction):
#     # 監視チャンネル制限
#     if ALLOWED_CHANNEL_IDS:
#         ch_id = getattr(interaction.channel, "id", None)
#         parent_id = getattr(interaction.channel, "parent_id", None)
#         if (ch_id not in ALLOWED_CHANNEL_IDS) and (
#             parent_id not in ALLOWED_CHANNEL_IDS
#         ):
#             await interaction.response.send_message(
#                 "このチャンネルでは使用できません。", ephemeral=True
#             )
#             return
#     await interaction.response.send_message("pong", ephemeral=True)


async def send_saved_post_on_startup():
    """起動時テスト送信: 常に /home を取得して「直近のコンテストの告知」パネルの最新投稿を送信する。"""
    if not TARGET_CHANNEL_ID:
        print("SEND_LATEST_ON_STARTUP が有効ですが TARGET_CHANNEL_ID が未設定です")
        return

    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "AtCoderWatchBot/1.0 (+https://example.local/)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            async with session.get(ATCODER_URL) as resp:
                if resp.status != 200:
                    print("起動時 /home 取得失敗 status=", resp.status)
                    return
                html_text = await resp.text()
        except Exception as e:
            print("起動時 /home 取得エラー:", e)
            return

    soup = BeautifulSoup(html_text, "html.parser")
    panel = _find_contest_panel(soup)
    if not panel:
        print("起動時: 直近のコンテストの告知パネルが見つかりませんでした")
        return

    a = panel.find("a", href=lambda h: h and h.startswith("/posts/"))
    if not a:
        print("起動時: パネル内に投稿リンクが見つかりませんでした")
        return

    href = a["href"]
    post_url = f"https://atcoder.jp{href}"
    latest_post_id = href.rstrip("/").split("/")[-1]
    latest_title = a.get_text(strip=True)

    is_contest_post = False
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "AtCoderWatchBot/1.0 (+https://example.local/)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session2:
        try:
            async with session2.get(post_url) as resp:
                if resp.status == 200:
                    post_html = await resp.text()
                    psoup = BeautifulSoup(post_html, "html.parser")
                    ca = psoup.find(
                        "a",
                        href=lambda h: h
                        and (
                            h.startswith("/contests/")
                            or (h.startswith("https://atcoder.jp/contests/"))
                        ),
                    )
                    if ca:
                        is_contest_post = True
                else:
                    print("起動時の投稿取得失敗 status=", resp.status)
        except Exception as e:
            print("起動時の投稿取得エラー:", e)

    if not is_contest_post:
        print("起動時: この投稿はコンテスト告知ではありません（/contests/ リンクなし）")
        try:
            LAST_HASH_FILE.write_text(f"contest:{latest_post_id}")
        except Exception:
            pass
        return
    try:
        LAST_HASH_FILE.write_text(f"contest:{latest_post_id}")
    except Exception:
        pass

    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "AtCoderWatchBot/1.0 (+https://example.local/)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            async with session.get(post_url) as resp:
                if resp.status != 200:
                    print("起動時の投稿取得失敗 status=", resp.status)
                    return
                post_html = await resp.text()
        except Exception as e:
            print("起動時の投稿取得エラー:", e)
            return

    psoup = BeautifulSoup(post_html, "html.parser")
    body = psoup.select_one("div.panel-body.blog-post") or psoup.select_one(
        "div.panel-body"
    )
    body_html = html.unescape(str(body)) if body else ""
    text = md(body_html, strip=["span", "time", "div"]) if body_html else ""
    pat_img = r"!\[[^\]]*\]\([^)]*\)\s*"
    text = re.sub(pat_img, "", text)
    pat_user = r"\((/users/[^)]*)\)"
    text = re.sub(pat_user, r"(https://atcoder.jp\1)", text)

    channel = client.get_channel(int(TARGET_CHANNEL_ID))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(TARGET_CHANNEL_ID))
        except Exception:
            channel = None

    if not channel:
        print("指定チャンネルが見つかりません:", TARGET_CHANNEL_ID)
        return

    if text:
        desc = text if len(text) <= 1900 else text[:1900] + "…"
        embed = discord.Embed(
            title=f"直近のコンテスト告知: {latest_title}",
            url=post_url,
            description=desc,
        )
        await channel.send(
            content="【テスト送信】直近のコンテスト告知を送信します", embed=embed
        )
    else:
        await channel.send(
            f"【テスト送信】直近のコンテスト告知: {post_url} (本文が取得できませんでした)"
        )


async def check_atcoder_loop():
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "AtCoderWatchBot/1.0 (+https://example.local/)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        while True:
            try:
                async with session.get(ATCODER_URL) as resp:
                    if resp.status != 200:
                        print("AtCoder取得失敗 status=", resp.status)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                    text = await resp.text()

                soup = BeautifulSoup(text, "html.parser")
                panel = _find_contest_panel(soup)
                latest_id = None
                latest_title = None
                latest_url = None
                if panel:
                    a = panel.find("a", href=lambda h: h and h.startswith("/posts/"))
                    if a:
                        href = a["href"]
                        latest_id = href.rstrip("/").split("/")[-1]
                        latest_title = a.get_text(strip=True)
                        latest_url = f"https://atcoder.jp{href}"
                last_raw = ""
                if LAST_HASH_FILE.exists():
                    last_raw = LAST_HASH_FILE.read_text().strip()

                if latest_id:
                    last_contest = (
                        last_raw[8:] if last_raw.startswith("contest:") else ""
                    )

                    if last_contest and latest_id != last_contest:
                        if TARGET_CHANNEL_ID:
                            channel = client.get_channel(int(TARGET_CHANNEL_ID))
                            if channel is None:
                                try:
                                    channel = await client.fetch_channel(
                                        int(TARGET_CHANNEL_ID)
                                    )
                                except Exception:
                                    channel = None
                            if channel:
                                post_text = ""
                                is_contest_post = False
                                try:
                                    async with session.get(latest_url) as post_resp:
                                        if post_resp.status == 200:
                                            post_html = await post_resp.text()
                                            psoup = BeautifulSoup(
                                                post_html, "html.parser"
                                            )
                                            body = psoup.select_one(
                                                "div.panel-body.blog-post"
                                            ) or psoup.select_one("div.panel-body")
                                            if body:
                                                body_html = (
                                                    html.unescape(str(body))
                                                    if body
                                                    else ""
                                                )
                                                text = (
                                                    md(
                                                        body_html,
                                                        strip=["span", "time", "div"],
                                                    )
                                                    if body_html
                                                    else ""
                                                )
                                                pat_img = r"!\[[^\]]*\]\([^)]*\)\s*"
                                                text = re.sub(pat_img, "", text)
                                                pat_user = r"\((/users/[^)]*)\)"
                                                post_text = re.sub(
                                                    pat_user,
                                                    r"(https://atcoder.jp\1)",
                                                    text,
                                                )
                                            ca = psoup.find(
                                                "a",
                                                href=lambda h: h
                                                and (
                                                    h.startswith("/contests/")
                                                    or (
                                                        h.startswith(
                                                            "https://atcoder.jp/contests/"
                                                        )
                                                    )
                                                ),
                                            )
                                            if ca:
                                                is_contest_post = True
                                        else:
                                            print(
                                                "投稿ページ取得失敗 status=",
                                                post_resp.status,
                                            )
                                except Exception as e:
                                    print("投稿取得エラー:", e)

                                if not is_contest_post:
                                    print(
                                        "検出された投稿はコンテスト告知ではありません（/contests/ リンクなし）: ",
                                        latest_url,
                                    )
                                else:
                                    if post_text:

                                        desc = post_text
                                        if len(desc) > 1900:
                                            desc = desc[:1900] + "…"
                                        embed = discord.Embed(
                                            title=latest_title,
                                            url=latest_url,
                                            description=desc,
                                        )
                                        # 追加: コンテストURLからシリーズ判定してロールメンション
                                        contest_link = None
                                        if ca:
                                            href2 = ca.get("href")
                                            if href2:
                                                contest_link = (
                                                    href2
                                                    if href2.startswith("http")
                                                    else f"https://atcoder.jp{href2}"
                                                )
                                        role_prefix = _role_mention_for_contest(
                                            contest_link or latest_url
                                        )
                                        await channel.send(
                                            content=f"{role_prefix}【AtCoder 告知】",
                                            embed=embed,
                                            allowed_mentions=discord.AllowedMentions(
                                                roles=True
                                            ),
                                        )
                                    else:
                                        contest_link = None
                                        if ca:
                                            href2 = ca.get("href")
                                            if href2:
                                                contest_link = (
                                                    href2
                                                    if href2.startswith("http")
                                                    else f"https://atcoder.jp{href2}"
                                                )
                                        role_prefix = _role_mention_for_contest(
                                            contest_link or latest_url
                                        )
                                        await channel.send(
                                            content=f"{role_prefix}【AtCoder 告知】{latest_title}\n{latest_url}",
                                            allowed_mentions=discord.AllowedMentions(
                                                roles=True
                                            ),
                                        )
                            else:
                                print("チャネルが見つかりません:", TARGET_CHANNEL_ID)
                        else:
                            print(
                                "TARGET_CHANNEL_ID が設定されていません。更新を検知:",
                                latest_url,
                            )

                    LAST_HASH_FILE.write_text(f"contest:{latest_id}")
                else:
                    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    last_hash = last_raw[5:] if last_raw.startswith("hash:") else ""

                    if last_hash and h != last_hash:
                        if TARGET_CHANNEL_ID:
                            channel = client.get_channel(int(TARGET_CHANNEL_ID))
                            if channel is None:
                                try:
                                    channel = await client.fetch_channel(
                                        int(TARGET_CHANNEL_ID)
                                    )
                                except Exception:
                                    channel = None
                            if channel:
                                await channel.send(
                                    f"AtCoderのページが更新されました: {ATCODER_URL}"
                                )
                            else:
                                print("チャネルが見つかりません:", TARGET_CHANNEL_ID)
                        else:
                            print(
                                "TARGET_CHANNEL_ID が設定されていません。更新を検知しました:",
                                ATCODER_URL,
                            )

                    LAST_HASH_FILE.write_text(f"hash:{h}")

            except Exception as e:
                print("AtCoderチェックエラー:", e)
            await asyncio.sleep(POLL_INTERVAL)


async def send_latest_announcements(channel):
    """/home から『直近のコンテストの告知』パネルの最新投稿を取得し、本文HTMLをMarkdownに変換して指定チャンネルへ送信する。"""

    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "AtCoderWatchBot/1.0 (+https://example.local/)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            async with session.get(ATCODER_URL) as resp:
                if resp.status != 200:
                    await channel.send(
                        f"/home の取得に失敗しました (status={resp.status})"
                    )
                    return
                html_text = await resp.text()
        except Exception as e:
            await channel.send(f"/home 取得エラー: {e}")
            return

    soup = BeautifulSoup(html_text, "html.parser")
    panel = _find_contest_panel(soup)
    if not panel:
        await channel.send("『直近のコンテストの告知』パネルが見つかりませんでした。")
        return

    a = panel.find("a", href=lambda h: h and h.startswith("/posts/"))
    if not a:
        await channel.send("パネル内に投稿リンクが見つかりませんでした。")
        return

    href = a["href"]
    post_url = f"https://atcoder.jp{href}"
    latest_post_id = href.rstrip("/").split("/")[-1]
    latest_title = a.get_text(strip=True)

    is_contest_post = False
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "AtCoderWatchBot/1.0 (+https://example.local/)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session2:
        try:
            async with session2.get(post_url) as resp:
                if resp.status == 200:
                    post_html = await resp.text()
                    psoup = BeautifulSoup(post_html, "html.parser")
                    ca = psoup.find(
                        "a",
                        href=lambda h: h
                        and (
                            h.startswith("/contests/")
                            or (h.startswith("https://atcoder.jp/contests/"))
                        ),
                    )
                    if ca:
                        is_contest_post = True
                else:
                    await channel.send(
                        f"投稿ページの取得に失敗しました (status={resp.status})"
                    )
        except Exception as e:
            await channel.send(f"投稿取得エラー: {e}")

    if not is_contest_post:
        await channel.send(
            "この投稿はコンテスト告知ではありません（/contests/ リンクなし）"
        )

        try:
            LAST_HASH_FILE.write_text(f"contest:{latest_post_id}")
        except Exception:
            pass
        return

    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "AtCoderWatchBot/1.0 (+https://example.local/)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            async with session.get(post_url) as resp:
                if resp.status != 200:
                    await channel.send(
                        f"投稿ページの取得に失敗しました (status={resp.status})"
                    )
                    return
                post_html = await resp.text()
        except Exception as e:
            await channel.send(f"投稿取得エラー: {e}")
            return

    psoup = BeautifulSoup(post_html, "html.parser")

    body = psoup.select_one("div.panel-body.blog-post") or psoup.select_one(
        "div.panel-body"
    )
    body_html = html.unescape(str(body)) if body else ""
    text = md(body_html, strip=["span", "time", "div"]) if body_html else ""
    pat_img = r"!\[[^\]]*\]\([^)]*\)\s*"
    text = re.sub(pat_img, "", text)
    pat_user = r"\((/users/[^)]*)\)"
    text = re.sub(pat_user, r"(https://atcoder.jp\1)", text)

    if text:
        desc = text if len(text) <= 1900 else text[:1900] + "…"
        embed = discord.Embed(
            title=f"直近のコンテスト告知: {latest_title}",
            url=post_url,
            description=desc,
        )
        await channel.send(
            content="【AtCoder 告知】直近のコンテスト告知を送信します", embed=embed
        )
    else:
        await channel.send(
            f"直近のコンテスト告知: {post_url} (本文が取得できませんでした)"
        )


async def _fetch_latest_series_announcement(
    session: aiohttp.ClientSession, series_prefix: str
):
    """
    /home の『直近のコンテストの告知』パネル内の投稿を新しい順に辿り、
    各 /posts/ ページを開いて /contests/{series_prefix} へのリンクを含むものを探し、
    本文HTMLをmd変換して返す。
    戻り値: dict(title, post_url, text) または None
    """

    async with session.get(ATCODER_URL) as resp:
        if resp.status != 200:
            return None
        html_text = await resp.text()

    soup = BeautifulSoup(html_text, "html.parser")
    panel = _find_contest_panel(soup)

    links = []
    if panel:
        links = panel.find_all("a", href=lambda h: h and h.startswith("/posts/"))

    if not links:
        links = soup.find_all("a", href=lambda h: h and h.startswith("/posts/"))

    seen = set()
    post_hrefs = []
    for a in links:
        href = a.get("href")
        if href and href not in seen:
            seen.add(href)
            title = a.get_text(strip=True) or "Announcement"
            post_hrefs.append((title, f"https://atcoder.jp{href}"))

    if not post_hrefs:
        return None

    for title, post_url in post_hrefs[:40]:
        try:
            async with session.get(post_url) as pr:
                if pr.status != 200:
                    continue
                post_html = await pr.text()
        except Exception:
            continue

        psoup = BeautifulSoup(post_html, "html.parser")
        anchor_ok = (
            psoup.find(
                "a",
                href=lambda h: h
                and (
                    h.startswith(f"/contests/{series_prefix}")
                    or h.startswith(f"https://atcoder.jp/contests/{series_prefix}")
                ),
            )
            is not None
        )
        text_all = psoup.get_text(" ", strip=True)
        plain_ok = (
            re.search(
                rf"https?://atcoder\.jp/contests/{series_prefix}[a-z0-9\-_/]*",
                text_all,
                flags=re.IGNORECASE,
            )
            is not None
        )
        if not (anchor_ok or plain_ok):
            continue
        body = psoup.select_one("div.panel-body.blog-post") or psoup.select_one(
            "div.panel-body"
        )
        body_html = html.unescape(str(body)) if body else ""
        text = md(body_html, strip=["span", "time", "div"]) if body_html else ""
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)\s*", "", text)
        text = re.sub(r"\((/users/[^)]*)\)", r"(https://atcoder.jp\1)", text)

        return {"title": title, "post_url": post_url, "text": text}

    return None


async def send_series_announcement(series_prefix: str, channel):
    """直近の {series_prefix} の告知投稿本文を md 変換して送信する。"""
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "AtCoderWatchBot/1.0 (+https://example.local/)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        data = await _fetch_latest_series_announcement(session, series_prefix)

    if not data:
        await channel.send(
            f"直近の {series_prefix.upper()} の告知投稿は見つかりませんでした。"
        )
        return

    title, post_url, text = data["title"], data["post_url"], data["text"]
    desc = text if len(text) <= 1900 else text[:1900] + "…"
    embed = discord.Embed(
        title=f"直近の {series_prefix.upper()} 告知: {title}",
        url=post_url,
        description=desc,
    )
    await channel.send(content="【AtCoder 告知】", embed=embed)


@client.event
async def on_message(message):
    # スラッシュ専用運用。テキストメッセージは処理しない
    return


# プレフィックスコマンドは廃止（スラッシュコマンドのみ）


TOKEN = os.getenv("TOKEN")
client.run(TOKEN)
