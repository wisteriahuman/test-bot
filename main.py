import discord
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
    # 1) パネル集合を走査して、内部に該当見出しを持つ外側の panel div を返す方式に変更
    for div in soup.find_all("div"):
        classes = div.get("class") or []
        if "panel" in classes:
            # div の内部に見出しがあるかチェック
            heading = div.find(
                lambda tag: tag.name in ("h1", "h2", "h3")
                and tag.get_text()
                and "直近のコンテストの告知" in tag.get_text()
            )
            if heading:
                return div

    # 2) id ベースの探索
    panel = soup.find("div", id="contest-table-upcoming")
    if panel:
        return panel

    # 3) 汎用フォールバック: テキストを探してその親を返す
    text_node = soup.find(string=lambda s: s and "直近のコンテストの告知" in s)
    if text_node and hasattr(text_node, "parent"):
        return text_node.parent

    return None


load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

ATCODER_URL = os.getenv("ATCODER_URL", "https://atcoder.jp/home?lang=ja")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
LAST_HASH_FILE = pathlib.Path(__file__).parent / ".last_atcoder_hash"
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID")
SEND_LATEST_ON_STARTUP = os.getenv("SEND_LATEST_ON_STARTUP", "false").lower() in (
    "1",
    "true",
    "yes",
)


@client.event
async def on_ready():
    if getattr(client, "_atcoder_tasks_started", False):
        return
    client._atcoder_tasks_started = True

    print(f"{client.user.name}がログインしました")
    print(f"Bot ID: {client.user.id}")
    print("------")
    if SEND_LATEST_ON_STARTUP:
        client.loop.create_task(send_saved_post_on_startup())
    client.loop.create_task(check_atcoder_loop())


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

    # パネル内のポストリンクを探す（/posts/）。ただし通知はその投稿ページ内に
    # /contests/ リンクが含まれる場合のみ行う（コンテスト告知のみを厳格化）。
    a = panel.find("a", href=lambda h: h and h.startswith("/posts/"))
    if not a:
        print("起動時: パネル内に投稿リンクが見つかりませんでした")
        return

    href = a["href"]
    post_url = f"https://atcoder.jp{href}"
    latest_post_id = href.rstrip("/").split("/")[-1]
    latest_title = a.get_text(strip=True)

    # 投稿ページを取得して、その中に /contests/ リンクが含まれるか確認する
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
        # それでも最新ポストIDは保存して、繰り返し評価されないようにする
        try:
            LAST_HASH_FILE.write_text(f"contest:{latest_post_id}")
        except Exception:
            pass
        return

    # 投稿がコンテスト告知であることが確認できたので状態を保存する
    try:
        LAST_HASH_FILE.write_text(f"contest:{latest_post_id}")
    except Exception:
        pass

    # 個別ページを取得して本文を送信
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
    # 投稿本文は panel-body.blog-post を期待するが、なければ body のテキスト全体を取得
    body = psoup.find("div", class_="panel-body blog-post")
    if not body:
        # フォールバックで最初の .panel-body を探す
        body = psoup.find("div", class_="panel-body")
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
    # タイムアウトと User-Agent を設定して堅牢化
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
                    # パネル内の投稿リンクを探す（/posts/）。その投稿ページに /contests/ が含まれるかで
                    # コンテスト告知か判定する（厳格化のため）。
                    a = panel.find("a", href=lambda h: h and h.startswith("/posts/"))
                    if a:
                        href = a["href"]
                        latest_id = href.rstrip("/").split("/")[-1]
                        latest_title = a.get_text(strip=True)
                        latest_url = f"https://atcoder.jp{href}"

                # LAST_HASH_FILE には接頭辞を付けて保存する（post:ID または hash:HEX）
                last_raw = ""
                if LAST_HASH_FILE.exists():
                    last_raw = LAST_HASH_FILE.read_text().strip()

                if latest_id:
                    # compare as contest:<id_or_path>
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
                                # 個別投稿ページを取得して、その中に /contests/ リンクがあるか確認する
                                post_text = ""
                                is_contest_post = False
                                try:
                                    async with session.get(latest_url) as post_resp:
                                        if post_resp.status == 200:
                                            post_html = await post_resp.text()
                                            psoup = BeautifulSoup(
                                                post_html, "html.parser"
                                            )
                                            # 本文抽出
                                            body = psoup.find(
                                                "div", class_="panel-body blog-post"
                                            )
                                            if not body:
                                                body = psoup.find(
                                                    "div", class_="panel-body"
                                                )
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
                                            # コンテストリンクの存在確認
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
                                        # Embed を使って見やすく送信、長さは切り詰め
                                        desc = post_text
                                        if len(desc) > 1900:
                                            desc = desc[:1900] + "…"
                                        embed = discord.Embed(
                                            title=latest_title,
                                            url=latest_url,
                                            description=desc,
                                        )
                                        await channel.send(
                                            content="【AtCoder 告知】", embed=embed
                                        )
                                    else:
                                        await channel.send(
                                            f"【AtCoder 告知】{latest_title}\n{latest_url}"
                                        )
                            else:
                                print("チャネルが見つかりません:", TARGET_CHANNEL_ID)
                        else:
                            print(
                                "TARGET_CHANNEL_ID が設定されていません。更新を検知:",
                                latest_url,
                            )

                    # 常に最新の contest:<id_or_path> を保存（初回は通知しない）
                    LAST_HASH_FILE.write_text(f"contest:{latest_id}")
                else:
                    # フォールバック: ページ全体ハッシュで検知
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

    # パネル内のポストリンクを探す（/posts/）。ただし通知はその投稿ページ内に
    # /contests/ リンクが含まれる場合のみ行う（コンテスト告知のみを厳格化）。
    a = panel.find("a", href=lambda h: h and h.startswith("/posts/"))
    if not a:
        print("起動時: パネル内に投稿リンクが見つかりませんでした")
        return

    href = a["href"]
    post_url = f"https://atcoder.jp{href}"
    latest_post_id = href.rstrip("/").split("/")[-1]
    latest_title = a.get_text(strip=True)

    # 投稿ページを取得して、その中に /contests/ リンクが含まれるか確認する
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
        # それでも最新ポストIDは保存して、繰り返し評価されないようにする
        try:
            LAST_HASH_FILE.write_text(f"contest:{latest_post_id}")
        except Exception:
            pass
        return

    # 個別ページを取得して本文を送信
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
    # 投稿本文は panel-body.blog-post を期待するが、なければ body のテキスト全体を取得
    body = psoup.find("div", class_="panel-body blog-post")
    if not body:
        # フォールバックで最初の .panel-body を探す
        body = psoup.find("div", class_="panel-body")
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
            content="直近のコンテスト告知を送信します", embed=embed
        )
    else:
        await channel.send(
            f"直近のコンテスト告知: {post_url} (本文が取得できませんでした)"
        )

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content == "!一号!Hello":
        await message.channel.send("Hello!")
        
    if message.content == "!一号!最新のコンテストは？":
        await send_latest_announcements(message.channel)

TOKEN = os.getenv("TOKEN")
client.run(TOKEN)
