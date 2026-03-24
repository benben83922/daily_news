import discord
from discord.ext import tasks, commands
from dotenv import load_dotenv
import feedparser
import requests
import os
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


def hex_to_discord_color(hex_str):
    """輔助函式：將 #FFFFFF 轉為 Discord Color"""
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

    def is_link_sent(self, link):
        response = (
            supabase_client.table("sent_news").select("id").eq("link", link).execute()
        )
        return len(response.data) > 0

    def save_to_supabase(self, entry, source_data):
        data = {
            "link": entry.link,
            "title": entry.title,
            "source": source_data["name"],
            "source_id": source_data["id"],
            "published_at": entry.get("published", None),
        }
        supabase_client.table("sent_news").insert(data).execute()

    async def setup_hook(self):
        # 啟動時開始定時任務
        self.fetch_rss_task.start()

    async def on_ready(self):
        print(f"✅ {self.user} 已啟動，開始抓取所有來源...")

    def get_active_sources(self):
        response = (
            supabase_client.table("news_sources")
            .select("*")
            .eq("is_active", True)
            .execute()
        )
        return response.data

    def log_to_supabase(self, level, event, message, source_name=None):
        log_data = {
            "level": level,
            "event": event,
            "message": str(message),
            "source_name": source_name,
        }
        try:
            supabase_client.table("bot_logs").insert(log_data).execute()
            print(f"[{level}] {event}: {message}")
        except Exception as e:
            print(f"❌ 寫入日誌失敗: {e}")

    # --- 核心邏輯：自動定時巡邏 ---
    @tasks.loop(minutes=10)
    async def fetch_rss_task(self):
        channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)

        # --- 關鍵改動：動態抓取來源 ---
        current_sources = self.get_active_sources()
        self.log_to_supabase(
            "INFO", "Fetch_Cycle_Start", f"檢查 {len(current_sources)} 個來源"
        )

        for source in current_sources:
            try:
                response = requests.get(source["url"], headers=HEADERS, timeout=10)
                response.encoding = "utf-8"
                feed = feedparser.parse(response.text)

                for entry in reversed(feed.entries[:3]):
                    if not self.is_link_sent(entry.link):

                        color = hex_to_discord_color(source.get("color_hex", "#7289da"))

                        embed = discord.Embed(
                            title=entry.title, url=entry.link, color=color
                        )
                        embed.set_author(name=f"來源：{source['name']}")
                        embed.set_footer(
                            text=f"發佈時間：{entry.get('published', '未知')}"
                        )

                        view = NewsView(summary=entry.get("summary", "暫無摘要"))
                        await channel.send(embed=embed, view=view)

                        self.save_to_supabase(entry, source)
                        self.log_to_supabase(
                            "INFO", "News_Sent", entry.title, source["name"]
                        )

            except Exception as e:
                self.log_to_supabase("ERROR", "Fetch_Error", str(e), source["name"])

    @fetch_rss_task.before_loop
    async def before_fetch(self):
        """在循環開始前先等待 Bot 準備好"""
        await self.wait_until_ready()


# --- 在啟動 Bot 的地方修改 ---
if __name__ == "__main__":
    # 啟動偽裝網頁的執行緒
    threading.Thread(target=run_health_server, daemon=True).start()

    # 剩下的原本 Bot 啟動邏輯 ...
    bot = NewsBot()
    bot.run(TOKEN)
