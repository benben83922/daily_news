import asyncio
import discord
from discord.ext import tasks, commands
from dotenv import load_dotenv
import feedparser
import requests
import os
import re
from supabase import create_client, Client
import supabase
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


# --- 新增這段：偽裝網頁伺服器 ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, format, *args):
        return  # 隱藏網頁日誌，保持終端機乾淨


def run_health_server():
    # 讀取 Koyeb 給的 Port，預設 8000
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    print(f"🌍 Health Check Server started on port {port}")
    server.serve_forever()


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 偽裝瀏覽器標頭，防止被網站封鎖
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}


def clean_html(raw_html):
    """移除 HTML 標籤並限制長度"""
    if not raw_html:
        return "暫無摘要"
    cleanr = re.compile("<.*?>")
    cleantext = re.sub(cleanr, "", raw_html)
    # Discord Embed 描述上限為 4096，但按鈕訊息建議 1000 字以內
    return cleantext[:1000] + "..." if len(cleantext) > 1000 else cleantext


def hex_to_discord_color(hex_str):
    """輔助函式：將 #FFFFFF 轉為 Discord Color"""
    if not hex_str:
        return discord.Color.blue()
    hex_str = hex_str.lstrip("#")
    return discord.Color(int(hex_str, 16))


class NewsView(discord.ui.View):
    def __init__(self, summary):
        super().__init__(timeout=None)
        self.summary = summary

    @discord.ui.button(label="想看更多?", style=discord.ButtonStyle.gray)
    async def show_summary(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # clean_summary = (
        #     self.summary[:500] + "..." if len(self.summary) > 500 else self.summary
        # )
        # 當使用者點擊按鈕時，回傳一封「只有他看得到」的訊息（Ephemeral）
        await interaction.response.send_message(
            content=f"📝 **新聞摘要：**\n{self.summary}",
            ephemeral=True,  # 關鍵！這行能確保頻道不會被洗板
        )


class NewsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    # --- 同步方法 (將由 asyncio.to_thread 呼叫) ---
    def sync_is_link_sent(self, link):
        res = supabase_client.table("sent_news").select("id").eq("link", link).execute()
        return len(res.data) > 0

    def sync_save_to_supabase(self, data):
        supabase_client.table("sent_news").insert(data).execute()

    def sync_get_active_sources(self):
        res = (
            supabase_client.table("news_sources")
            .select("*")
            .eq("is_active", True)
            .execute()
        )
        return res.data

    def sync_log_to_supabase(self, log_data):
        try:
            supabase_client.table("bot_logs").insert(log_data).execute()
        except Exception as e:
            print(f"❌ 寫入日誌失敗: {e}")

    # --- 非同步封裝 ---
    async def log_event(self, level, event, message, source_name=None):
        log_data = {
            "level": level,
            "event": event,
            "message": str(message),
            "source_name": source_name,
        }
        print(f"[{level}] {event}: {message}")
        await asyncio.to_thread(self.sync_log_to_supabase, log_data)

    # --- 核心巡邏任務 ---
    @tasks.loop(minutes=10)
    async def fetch_rss_task(self):
        try:
            channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(
                CHANNEL_ID
            )
            sources = await asyncio.to_thread(self.sync_get_active_sources)
            await self.log_event(
                "INFO", "Fetch_Start", f"開始檢查 {len(sources)} 個來源"
            )

            for source in sources:
                try:
                    # 使用執行緒抓取網頁，避免卡住心跳
                    response = await asyncio.to_thread(
                        requests.get, source["url"], headers=HEADERS, timeout=15
                    )
                    response.encoding = "utf-8"
                    feed = feedparser.parse(response.text)

                    # 遍歷前 10 則新聞
                    for entry in reversed(feed.entries[:10]):
                        is_sent = await asyncio.to_thread(
                            self.sync_is_link_sent, entry.link
                        )

                        if not is_sent:
                            # 準備 Embed 訊息
                            color = hex_to_discord_color(
                                source.get("color_hex", "#7289da")
                            )
                            embed = discord.Embed(
                                title=entry.title, url=entry.link, color=color
                            )
                            embed.set_author(name=f"來源：{source['name']}")
                            embed.set_footer(
                                text=f"發佈時間：{entry.get('published', '未知')}"
                            )

                            # 摘要處理
                            raw_summary = entry.get(
                                "summary", entry.get("description", "暫無摘要")
                            )
                            summary_text = clean_html(raw_summary)

                            view = NewsView(summary=summary_text)
                            await channel.send(embed=embed, view=view)

                            # 存入資料庫
                            db_data = {
                                "link": entry.link,
                                "title": entry.title,
                                "source": source["name"],
                                "source_id": source["id"],
                                "published_at": entry.get("published", None),
                            }
                            await asyncio.to_thread(self.sync_save_to_supabase, db_data)
                            await self.log_event(
                                "INFO", "News_Sent", entry.title, source["name"]
                            )

                            # 防刷頻延遲
                            await asyncio.sleep(1)

                except Exception as e:
                    await self.log_event(
                        "ERROR", "Source_Error", str(e), source["name"]
                    )

        except Exception as e:
            await self.log_event("ERROR", "Global_Error", str(e))

    async def setup_hook(self):
        self.fetch_rss_task.start()

    async def on_ready(self):
        await self.log_event("INFO", "Bot_Startup", f"{self.user} 已上線並接管任務")

    @fetch_rss_task.before_loop
    async def before_fetch(self):
        await self.wait_until_ready()


if __name__ == "__main__":
    # 啟動 Koyeb Health Check 執行緒
    threading.Thread(target=run_health_server, daemon=True).start()

    # 啟動 Discord Bot
    bot = NewsBot()
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ 機器人崩潰: {e}")
