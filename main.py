import asyncio
import json
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
import httpx
from bs4 import BeautifulSoup

# ==================== GLOBAL VARIABLES ====================
history = []
last_buy = None
active_connections = set()
usd_idr_history = []

update_event = asyncio.Event()
usd_idr_update_event = asyncio.Event()

treasury_info = "Belum ada info treasury."
treasury_info_update_event = asyncio.Event()

telegram_app = None

# ==================== HELPER FUNCTIONS ====================
def format_rupiah(nominal):
    try:
        return "{:,}".format(int(nominal)).replace(",", ".")
    except:
        return str(nominal)

def parse_price_to_float(price_str):
    try:
        return float(price_str.replace('.', '').replace(',', '.'))
    except:
        return None

# ==================== BACKGROUND LOOPS - SUPER FAST ====================
async def api_loop():
    """Loop TERCEPAT untuk fetch harga emas dari Treasury API"""
    global last_buy, history
    api_url = "https://api.treasury.id/api/v1/antigrvty/gold/rate"
    shown_updates = set()
    
    # Persistent connection untuk kecepatan maksimal
    async with httpx.AsyncClient(
        timeout=5,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        http2=True  # HTTP/2 lebih cepat!
    ) as client:
        while True:
            try:
                response = await client.post(api_url)
                if response.status_code == 200:
                    data = response.json().get("data", {})
                    buying_rate = int(data.get("buying_rate", 0))
                    selling_rate = int(data.get("selling_rate", 0))
                    updated_at = data.get("updated_at")
                    
                    if updated_at and updated_at not in shown_updates:
                        status = "➖"
                        if last_buy is not None:
                            if buying_rate > last_buy:
                                status = "🚀"
                            elif buying_rate < last_buy:
                                status = "🔻"
                        
                        row = {
                            "buying_rate": buying_rate,
                            "selling_rate": selling_rate,
                            "status": status,
                            "created_at": updated_at
                        }
                        history.append(row)
                        history[:] = history[-1441:]
                        last_buy = buying_rate
                        shown_updates.add(updated_at)
                        
                        # Bersihkan memory
                        if len(shown_updates) > 5000:
                            shown_updates.clear()
                            shown_updates.add(updated_at)
                        
                        update_event.set()
                        print(f"⚡ GOLD: Beli={format_rupiah(buying_rate)} | Jual={format_rupiah(selling_rate)} | {status}")
                
                # 🚀 SUPER FAST: 100ms interval (10x per detik!)
                await asyncio.sleep(0.1)
                
            except Exception as e:
                print("Error api_loop:", e)
                await asyncio.sleep(0.5)

async def usd_idr_loop():
    """Loop TERCEPAT untuk fetch harga USD/IDR"""
    global usd_idr_history
    
    async with httpx.AsyncClient(
        timeout=5,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
    ) as client:
        while True:
            try:
                url = "https://www.google.com/finance/quote/USD-IDR"
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                price_div = soup.find("div", class_="YMlKec fxKbKc")
                
                if price_div:
                    price_str = price_div.text.strip()
                    if not usd_idr_history or usd_idr_history[-1]["price"] != price_str:
                        wib_now = datetime.utcnow() + timedelta(hours=7)
                        usd_idr_history.append({
                            "price": price_str,
                            "time": wib_now.strftime("%H:%M:%S")
                        })
                        usd_idr_history[:] = usd_idr_history[-11:]
                        usd_idr_update_event.set()
                        print(f"💵 USD/IDR: {price_str}")
                
                # 🚀 FAST: 300ms interval
                await asyncio.sleep(0.3)
                
            except Exception as e:
                print("Error usd_idr_loop:", e)
                await asyncio.sleep(0.5)

# ==================== TELEGRAM BOT ====================
async def start_telegram_bot():
    global telegram_app
    
    try:
        from telegram.ext import ApplicationBuilder, CommandHandler
        from telegram import Update
        from telegram.ext import ContextTypes
    except ImportError:
        print("❌ python-telegram-bot not installed!")
        return None

    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN not set!")
        return None

    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🤖 Bot aktif! Gunakan /atur <teks>")

    async def atur_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global treasury_info
        text = update.message.text.partition(' ')[2]
        if text:
            text = text.replace("  ", "&nbsp;&nbsp;").replace("\n", "<br>")
            treasury_info = text
            treasury_info_update_event.set()
            await update.message.reply_text("✅ Info Treasury diubah!")
        else:
            await update.message.reply_text("❌ Gunakan: /atur <kalimat>")

    try:
        telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_handler))
        telegram_app.add_handler(CommandHandler("atur", atur_handler))
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message"])
        print("✅ Telegram bot started!")
        return telegram_app
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return None

async def stop_telegram_bot():
    global telegram_app
    if telegram_app:
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except:
            pass

# ==================== HTML TEMPLATE ====================
html = """
<!DOCTYPE html>
<html>
<head>
    <title>⚡ Harga Emas Treasury - REALTIME</title>
    <link rel="stylesheet" type="text/css" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css"/>
    <style>
        body { font-family: Arial; margin: 40px; background: #fff; color: #222; transition: background 0.3s, color 0.3s; }
        table.dataTable thead th { font-weight: bold; }
        th.waktu, td.waktu { width: 150px; min-width: 100px; max-width: 180px; white-space: nowrap; text-align: left; }
        th.profit, td.profit { width: 90px; min-width: 80px; max-width: 100px; white-space: nowrap; text-align: left; }
        .dark-mode { background: #181a1b !important; color: #e0e0e0 !important; }
        .dark-mode #jam { color: #ffb300 !important; }
        .dark-mode table.dataTable { background: #23272b !important; color: #e0e0e0 !important; }
        .dark-mode table.dataTable thead th { background: #23272b !important; color: #ffb300 !important; }
        .dark-mode table.dataTable tbody td { background: #23272b !important; color: #e0e0e0 !important; }
        .theme-toggle-btn { padding: 0; border: none; border-radius: 50%; background: #222; color: #fff; cursor: pointer; font-size: 1.5em; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; }
        .theme-toggle-btn:hover { background: #444; }
        .dark-mode .theme-toggle-btn { background: #ffb300; color: #222; }
        .container-flex { display: flex; gap: 15px; margin-top: 10px; }
        #usdIdrRealtime { width: 248px; border: 1px solid #ccc; padding: 10px; height: 370px; overflow-y: auto; }
        #priceList li { margin-bottom: 1px; }
        .time { color: gray; font-size: 0.9em; margin-left: 10px; }
        #currentPrice { color: red; font-weight: bold; }
        .dark-mode #currentPrice { color: #00E124; text-shadow: 1px 1px #00B31C; }
        #tabel tbody tr:first-child td { color: red !important; font-weight: bold; }
        .dark-mode #tabel tbody tr:first-child td { color: #00E124 !important; font-weight: bold; }
        #footerApp { width: 100%; position: fixed; bottom: 0; left: 0; text-align: center; z-index: 100; padding: 8px 0; }
        .marquee-text { display: inline-block; color: #F5274D; animation: marquee 70s linear infinite; font-weight: bold; }
        .dark-mode .marquee-text { color: #B232B2; }
        @keyframes marquee { 0% { transform: translateX(100vw); } 100% { transform: translateX(-100vw); } }
        #isiTreasury { white-space: pre-line; color: red; font-weight: bold; max-height: 376px; overflow-y: auto; word-break: break-word; }
        .dark-mode #isiTreasury { color: #00E124; text-shadow: 1px 1px #00B31C; }
        #ingfo { width: 218px; border: 1px solid #ccc; padding: 10px; height: 378px; overflow-y: auto; }
        
        /* SPEED INDICATOR */
        .speed-badge { 
            background: linear-gradient(45deg, #ff0000, #ff6600); 
            color: white; 
            padding: 5px 15px; 
            border-radius: 20px; 
            font-weight: bold; 
            font-size: 0.9em;
            animation: glow 1s ease-in-out infinite alternate;
            margin-left: 15px;
        }
        @keyframes glow {
            from { box-shadow: 0 0 5px #ff0000; }
            to { box-shadow: 0 0 20px #ff6600; }
        }
        .live-dot {
            display: inline-block;
            width: 12px;
            height: 12px;
            background: #00ff00;
            border-radius: 50%;
            margin-right: 8px;
            animation: blink 0.5s infinite;
        }
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        #updateCounter { color: #888; font-size: 0.85em; margin-left: 10px; }
        #latency { color: #00aa00; font-weight: bold; }
    </style>
</head>
<body>
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
        <div style="display: flex; align-items: center;">
            <h2 style="margin:0;"><span class="live-dot"></span>MONITORING Harga Emas Treasury</h2>
            <span class="speed-badge">⚡ ULTRA FAST</span>
        </div>
        <button class="theme-toggle-btn" id="themeBtn" onclick="toggleTheme()" title="Ganti Tema">🌙</button>
    </div>
    <div id="jam" style="font-size:1.3em; color:#ff1744; font-weight:bold; margin-bottom:5px;"></div>
    <div style="margin-bottom:15px;">
        <span id="updateCounter">Updates: <strong id="totalUpdates">0</strong></span>
        <span style="margin-left:15px;">Latency: <span id="latency">-</span>ms</span>
        <span style="margin-left:15px;">Last: <span id="lastUpdateTime">-</span></span>
    </div>
    
    <table id="tabel" class="display" style="width:100%">
        <thead>
            <tr>
                <th class="waktu">Waktu</th>
                <th>Data Transaksi</th>
                <th class="profit">Est. cuan 20 JT</th>
                <th class="profit">Est. cuan 30 JT</th>
            </tr>
        </thead>
        <tbody></tbody>
    </table>
    
    <div style="margin-top:40px;">
        <h3>Chart Harga Emas (XAU/USD)</h3>
        <div id="tradingview_chart"></div>
        <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
        <script type="text/javascript">
        new TradingView.widget({
            "width": "100%", "height": 400, "symbol": "OANDA:XAUUSD", "interval": "15",
            "timezone": "Asia/Jakarta", "theme": "light", "style": "1", "locale": "id",
            "toolbar_bg": "#f1f3f6", "enable_publishing": false, "container_id": "tradingview_chart"
        });
        </script>
    </div>
    
    <div class="container-flex">
        <div>
            <h3 style="margin-top:30px;">Chart USD/IDR Investing - 15 Menit</h3>
            <div style="overflow:hidden; height:370px; width:620px; border:1px solid #ccc; border-radius:6px;">
                <iframe src="https://sslcharts.investing.com/index.php?force_lang=54&pair_ID=2138&timescale=900&candles=80&style=candles" width="618" height="430" style="margin-top:-62px; border:0;" scrolling="no" frameborder="0"></iframe>
            </div>
        </div>
        <div>
            <h3>Harga USD/IDR Google Finance</h3>
            <div id="usdIdrRealtime">
                <p>Harga saat ini: <span id="currentPrice">Loading...</span></p>
                <h4>Harga Terakhir:</h4>
                <ul id="priceList" style="list-style:none; padding-left:0; max-height:275px; overflow-y:auto;"></ul>
            </div>
        </div>
    </div>
    
    <div class="container-flex">
        <div>
            <h3 style="margin-top:30px;">Kalender Ekonomi</h3>
            <div style="overflow:hidden; height:470px; width:100%; border:1px solid #ccc; border-radius:6px;">
                <iframe src="https://sslecal2.investing.com?columns=exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous&category=_employment,_economicActivity,_inflation,_centralBanks,_confidenceIndex&importance=3&features=datepicker,timezone,timeselector,filters&countries=5,37,48,35,17,36,26,12,72&calType=week&timeZone=27&lang=54" width="650" height="467" frameborder="0"></iframe>
            </div>
        </div>
        <div>
            <h3>Sekilas Ingfo Treasury</h3>
            <div id="ingfo"><ul id="isiTreasury" style="list-style:none; padding-left:0;"></ul></div>
        </div>
    </div>
    
    <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>

    <script>
        var table = $('#tabel').DataTable({
            "pageLength": 4,
            "lengthMenu": [4, 8, 18, 48, 88, 888, 1441],
            "order": [],
            "columns": [
                { "data": "waktu" },
                { "data": "all" },
                { "data": "jt20" },
                { "data": "jt30" }
            ]
        });

        var totalUpdates = 0;
        var lastReceiveTime = null;

        function updateTable(history) {
            history.sort(function(a, b) {
                return new Date(b.created_at) - new Date(a.created_at);
            });
            var dataArr = history.map(function(d) {
                return {
                    waktu: d.created_at,
                    all: d.status + " | Harga Beli: " + d.buying_rate + " | Harga Jual: " + d.selling_rate,
                    jt20: d.jt20,
                    jt30: d.jt30
                };
            });
            table.clear();
            table.rows.add(dataArr);
            table.draw(false);
            table.page('first').draw(false);
            
            // Update counter
            totalUpdates++;
            document.getElementById("totalUpdates").textContent = totalUpdates;
            
            // Update last time
            if (history.length > 0) {
                document.getElementById("lastUpdateTime").textContent = history[0].created_at;
            }
        }

        function connectWS() {
            var ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
            
            ws.onopen = function() {
                console.log("⚡ WebSocket CONNECTED - Ultra Fast Mode!");
                lastReceiveTime = Date.now();
            };
            
            ws.onmessage = function(event) {
                // Hitung latency
                var now = Date.now();
                if (lastReceiveTime) {
                    var latency = now - lastReceiveTime;
                    document.getElementById("latency").textContent = latency;
                }
                lastReceiveTime = now;
                
                var data = JSON.parse(event.data);
                if (data.ping) return;
                if (data.history) updateTable(data.history);
                if (data.usd_idr_history) updateUsdIdrPrice(data.usd_idr_history);
                if (data.treasury_info !== undefined) updateTreasuryInfo(data.treasury_info);
            };
            
            ws.onclose = function() {
                console.log("WebSocket disconnected, reconnecting in 500ms...");
                setTimeout(connectWS, 500); // Reconnect cepat!
            };
        }
        connectWS();

        function updateTreasuryInfo(info) {
            document.getElementById("isiTreasury").innerHTML = info;
        }
        
        function updateUsdIdrPrice(history) {
            if (!history || history.length === 0) return;
            
            const currentPriceEl = document.getElementById("currentPrice");
            const priceListEl = document.getElementById("priceList");

            function parseHarga(str) {
                return parseFloat(str.trim().replace(/\\./g, '').replace(',', '.'));
            }
            
            const reversed = history.slice().reverse();
            
            let rowIconCurrent = "➖";
            if (reversed.length > 1) {
                let now = parseHarga(reversed[0].price);
                let prev = parseHarga(reversed[1].price);
                if (now > prev) rowIconCurrent = "🚀";
                else if (now < prev) rowIconCurrent = "🔻";
            }
            currentPriceEl.innerHTML = reversed[0].price + " " + rowIconCurrent;

            priceListEl.innerHTML = "";
            for (let i = 0; i < reversed.length; i++) {
                let rowIcon = "➖";
                if (i < reversed.length - 1) {
                    let now = parseHarga(reversed[i].price);
                    let next = parseHarga(reversed[i+1].price);
                    if (now > next) rowIcon = "🟢";
                    else if (now < next) rowIcon = "🔴";
                }
                const li = document.createElement("li");
                li.textContent = reversed[i].price + " ";
                const spanTime = document.createElement("span");
                spanTime.className = "time";
                spanTime.textContent = "(" + reversed[i].time + ")";
                li.appendChild(spanTime);
                li.appendChild(document.createTextNode(" " + rowIcon));
                priceListEl.appendChild(li);
            }
        }
        
        function updateJam() {
            var now = new Date();
            var tgl = now.toLocaleDateString('id-ID', { day: '2-digit', month: 'long', year: 'numeric' });
            var jam = now.toLocaleTimeString('id-ID', { hour12: false });
            document.getElementById("jam").textContent = tgl + " " + jam + " WIB";
        }
        setInterval(updateJam, 1000);
        updateJam();

        function toggleTheme() {
            document.body.classList.toggle('dark-mode');
            var btn = document.getElementById('themeBtn');
            if (document.body.classList.contains('dark-mode')) {
                btn.textContent = "☀️";
                localStorage.setItem('theme', 'dark');
            } else {
                btn.textContent = "🌙";
                localStorage.setItem('theme', 'light');
            }
        }
        (function() {
            if (localStorage.getItem('theme') === 'dark') {
                document.body.classList.add('dark-mode');
                document.getElementById('themeBtn').textContent = "☀️";
            }
        })();
    </script>
    <footer id="footerApp"><span class="marquee-text">&copy;2025 ~ahmadkholil~</span></footer>
</body>
</html>
"""

# ==================== LIFESPAN ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 60)
    print("⚡⚡⚡ ULTRA FAST MODE - Starting application... ⚡⚡⚡")
    print("=" * 60)
    
    task1 = asyncio.create_task(api_loop())
    task2 = asyncio.create_task(usd_idr_loop())
    print("🚀 API loop: 100ms interval (10 requests/detik)")
    print("🚀 USD/IDR loop: 300ms interval (3 requests/detik)")
    
    await start_telegram_bot()
    
    print("=" * 60)
    print("⚡ ALL SYSTEMS GO - MAXIMUM SPEED! ⚡")
    print("=" * 60)
    
    yield
    
    task1.cancel()
    task2.cancel()
    await stop_telegram_bot()
    await asyncio.gather(task1, task2, return_exceptions=True)
    print("✅ Application stopped!")

# ==================== FASTAPI APP ====================
app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(html)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    last_sent_updated_at = None
    last_usd_idr_price = None
    last_treasury_info = treasury_info

    if history:
        last_sent_updated_at = history[-1]["created_at"]
    if usd_idr_history:
        last_usd_idr_price = usd_idr_history[-1]["price"]

    def calc_20jt(h):
        try:
            val = int((20000000 / h["buying_rate"]) * h["selling_rate"] - 19315000)
            if val > 0: return f"+{format_rupiah(val)} 🟢"
            elif val < 0: return f"-{format_rupiah(abs(val))} 🔴"
            else: return "0 ➖"
        except: return "-"

    def calc_30jt(h):
        try:
            val = int((30000000 / h["buying_rate"]) * h["selling_rate"] - 28980000)
            if val > 0: return f"+{format_rupiah(val)} 🟢"
            elif val < 0: return f"-{format_rupiah(abs(val))} 🔴"
            else: return "0 ➖"
        except: return "-"

    # Kirim data awal
    try:
        await websocket.send_text(json.dumps({
            "history": [{"buying_rate": format_rupiah(h["buying_rate"]), "selling_rate": format_rupiah(h["selling_rate"]), "status": h["status"], "created_at": h["created_at"], "jt20": calc_20jt(h), "jt30": calc_30jt(h)} for h in history[-1441:]],
            "usd_idr_history": usd_idr_history,
            "treasury_info": treasury_info
        }))
    except:
        active_connections.discard(websocket)
        return

    try:
        while True:
            # Wait for ANY event - no timeout for maximum speed
            wait_tasks = [
                asyncio.create_task(update_event.wait()),
                asyncio.create_task(usd_idr_update_event.wait()),
                asyncio.create_task(treasury_info_update_event.wait())
            ]
            done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

            if update_event.is_set(): update_event.clear()
            if usd_idr_update_event.is_set(): usd_idr_update_event.clear()
            if treasury_info_update_event.is_set(): treasury_info_update_event.clear()

            current_updated_at = history[-1]["created_at"] if history else None
            current_usd_idr_price = usd_idr_history[-1]["price"] if usd_idr_history else None
            current_treasury_info = treasury_info

            send_update = False
            if current_updated_at != last_sent_updated_at:
                send_update = True
                last_sent_updated_at = current_updated_at
            if current_usd_idr_price != last_usd_idr_price:
                send_update = True
                last_usd_idr_price = current_usd_idr_price
            if current_treasury_info != last_treasury_info:
                send_update = True
                last_treasury_info = current_treasury_info

            if send_update:
                msg = {
                    "history": [{"buying_rate": format_rupiah(h["buying_rate"]), "selling_rate": format_rupiah(h["selling_rate"]), "status": h["status"], "created_at": h["created_at"], "jt20": calc_20jt(h), "jt30": calc_30jt(h)} for h in history[-1441:]],
                    "usd_idr_history": usd_idr_history,
                    "treasury_info": treasury_info
                }
                await websocket.send_text(json.dumps(msg))
                
    except WebSocketDisconnect:
        pass
    except:
        pass
    finally:
        active_connections.discard(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
