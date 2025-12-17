import asyncio
import json
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
import httpx
from bs4 import BeautifulSoup

history = []
history_lock = asyncio.Lock()
last_buy = None
active_connections = set()
usd_idr_history = []
usd_idr_lock = asyncio.Lock()

update_event = asyncio.Event()
usd_idr_update_event = asyncio.Event()

treasury_info = "Belum ada info treasury."
treasury_info_update_event = asyncio.Event()

telegram_app = None

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

def calc_20jt(h):
    try:
        val = int((20000000 / h["buying_rate"]) * h["selling_rate"] - 19315000)
        if val > 0:
            return f"+{format_rupiah(val)} 🟢"
        elif val < 0:
            return f"-{format_rupiah(abs(val))} 🔴"
        else:
            return "0 ➖"
    except:
        return "-"

def calc_30jt(h):
    try:
        val = int((30000000 / h["buying_rate"]) * h["selling_rate"] - 28980000)
        if val > 0:
            return f"+{format_rupiah(val)} 🟢"
        elif val < 0:
            return f"-{format_rupiah(abs(val))} 🔴"
        else:
            return "0 ➖"
    except:
        return "-"

async def fetch_usd_idr_price(client):
    url = "https://www.google.com/finance/quote/USD-IDR"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
    }
    try:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        price_div = soup.find("div", class_="YMlKec fxKbKc")
        if price_div:
            return price_div.text.strip()
        data_div = soup.find("div", {"data-last-price": True})
        if data_div:
            return data_div.get("data-last-price")
    except Exception as e:
        print(f"Error fetching USD/IDR price: {e}")
    return None

async def api_loop():
    global last_buy
    
    api_url = "https://api.treasury.id/api/v1/antigrvty/gold/rate"
    shown_updates = set()
    
    limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
    
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        limits=limits,
        http2=True
    ) as client:
        poll_interval = 0.5
        
        print("API Loop started...")
        
        while True:
            loop_start = asyncio.get_event_loop().time()
            
            try:
                response = await client.post(api_url)
                print(f"API Response status: {response.status_code}")
                
                if response.status_code == 200:
                    json_data = response.json()
                    print(f"API Response: {json_data}")
                    
                    data = json_data.get("data", {})
                    buying_rate = int(data.get("buying_rate", 0))
                    selling_rate = int(data.get("selling_rate", 0))
                    updated_at = data.get("updated_at")
                    
                    print(f"Parsed - Buy: {buying_rate}, Sell: {selling_rate}, Updated: {updated_at}")
                    
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
                        
                        async with history_lock:
                            history.append(row)
                            if len(history) > 1441:
                                del history[:-1441]
                            print(f"History count: {len(history)}")
                        
                        last_buy = buying_rate
                        shown_updates.add(updated_at)
                        
                        if len(shown_updates) > 2000:
                            shown_updates.clear()
                            shown_updates.add(updated_at)
                        
                        update_event.set()
                    
                    poll_interval = 0.5
                else:
                    print(f"API returned non-200: {response.status_code}")
                    poll_interval = 2.0
                    
            except httpx.TimeoutException as e:
                print(f"API Timeout: {e}")
                poll_interval = 1.0
            except Exception as e:
                print(f"Error in api_loop: {e}")
                poll_interval = 2.0
            
            elapsed = asyncio.get_event_loop().time() - loop_start
            sleep_time = max(0.1, poll_interval - elapsed)
            await asyncio.sleep(sleep_time)

async def usd_idr_loop():
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        http2=False
    ) as client:
        print("USD/IDR Loop started...")
        
        while True:
            try:
                price_str = await fetch_usd_idr_price(client)
                print(f"USD/IDR price fetched: {price_str}")
                
                if price_str:
                    async with usd_idr_lock:
                        if not usd_idr_history or usd_idr_history[-1]["price"] != price_str:
                            wib_now = datetime.utcnow() + timedelta(hours=7)
                            usd_idr_history.append({
                                "price": price_str,
                                "time": wib_now.strftime("%H:%M:%S")
                            })
                            if len(usd_idr_history) > 11:
                                del usd_idr_history[:-11]
                            print(f"USD/IDR history count: {len(usd_idr_history)}")
                            usd_idr_update_event.set()
                
                await asyncio.sleep(2.0)
            except Exception as e:
                print(f"Error in usd_idr_loop: {e}")
                await asyncio.sleep(2.0)

async def broadcast_loop():
    print("Broadcast Loop started...")
    
    while True:
        try:
            tasks = [
                asyncio.create_task(update_event.wait()),
                asyncio.create_task(usd_idr_update_event.wait()),
                asyncio.create_task(treasury_info_update_event.wait())
            ]
            
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            update_event.clear()
            usd_idr_update_event.clear()
            treasury_info_update_event.clear()
            
            if active_connections:
                msg = await prepare_broadcast_message()
                print(f"Broadcasting to {len(active_connections)} clients")
                
                async def send_to_client(ws):
                    try:
                        await asyncio.wait_for(ws.send_text(msg), timeout=5.0)
                    except Exception as e:
                        print(f"Failed to send to client: {e}")
                        active_connections.discard(ws)
                
                await asyncio.gather(
                    *[send_to_client(ws) for ws in list(active_connections)],
                    return_exceptions=True
                )
                
        except Exception as e:
            print(f"Broadcast error: {e}")
            await asyncio.sleep(0.5)

async def prepare_broadcast_message():
    async with history_lock:
        history_snapshot = list(history[-1441:])
    
    async with usd_idr_lock:
        usd_idr_snapshot = list(usd_idr_history)
    
    return json.dumps({
        "history": [
            {
                "buying_rate": format_rupiah(h["buying_rate"]),
                "selling_rate": format_rupiah(h["selling_rate"]),
                "status": h["status"],
                "created_at": h["created_at"],
                "jt20": calc_20jt(h),
                "jt30": calc_30jt(h)
            }
            for h in history_snapshot
        ],
        "usd_idr_history": usd_idr_snapshot,
        "treasury_info": treasury_info
    })

async def start_telegram_bot():
    global telegram_app
    
    try:
        from telegram.ext import ApplicationBuilder, CommandHandler
        from telegram import Update
        from telegram.ext import ContextTypes
    except ImportError:
        print("python-telegram-bot not installed!")
        return None

    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN not set!")
        return None

    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Bot aktif! Gunakan /atur <teks> untuk mengubah info treasury.")

    async def atur_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global treasury_info
        text = update.message.text.partition(' ')[2]
        
        if text:
            text = text.replace("  ", "&nbsp;&nbsp;")
            text = text.replace("\n", "<br>")
            treasury_info = text
            treasury_info_update_event.set()
            await update.message.reply_text("Info Treasury berhasil diubah!")
        else:
            await update.message.reply_text("Gunakan: /atur <kalimat info>")

    try:
        telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_handler))
        telegram_app.add_handler(CommandHandler("atur", atur_handler))
        
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message"]
        )
        
        print("Telegram bot started!")
        return telegram_app
        
    except Exception as e:
        print(f"Error starting Telegram bot: {e}")
        return None

async def stop_telegram_bot():
    global telegram_app
    
    if telegram_app:
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception as e:
            print(f"Error stopping Telegram bot: {e}")

html = """
<!DOCTYPE html>
<html>
<head>
    <title>Harga Emas Treasury</title>
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
        .theme-toggle-btn { padding: 0; border: none; border-radius: 50%; background: #222; color: #fff; font-weight: bold; cursor: pointer; transition: background 0.3s, color 0.3s; font-size: 1.5em; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; }
        .theme-toggle-btn:hover { background: #444; }
        .dark-mode .theme-toggle-btn { background: #ffb300; color: #222; }
        .dark-mode .theme-toggle-btn:hover { background: #ffd54f; }
        .container-flex { display: flex; gap: 15px; margin-top: 10px; }
        #usdIdrWidget { overflow:hidden; height:370px; width:630px; border:1px solid #ccc; border-radius:6px; }
        #usdIdrRealtime { width: 248px; border: 1px solid #ccc; padding: 10px; height: 370px; overflow-y: auto; }
        #priceList li { margin-bottom: 1px; }
        .time { color: gray; font-size: 0.9em; margin-left: 10px; }
        #currentPrice { color: red; font-weight: bold; }
        .dark-mode #currentPrice { color: #00E124; text-shadow: 1px 1px #00B31C; }
        #tabel tbody tr:first-child td { color: red !important; font-weight: bold; }
        .dark-mode #tabel tbody tr:first-child td { color: #00E124 !important; font-weight: bold; }
        #footerApp { width: 100%; overflow: hidden; position: fixed; bottom: 0; left: 0; background: transparent; text-align: center; z-index: 100; padding: 8px 0; }
        .marquee-text { display: inline-block; color: #F5274D; animation: marquee 70s linear infinite; font-weight: bold; }
        .dark-mode .marquee-text { color: #B232B2; font-weight: bold; }
        @keyframes marquee { 0% { transform: translateX(100vw); } 100% { transform: translateX(-100vw); } }
        #isiTreasury { white-space: pre-line; color: red; font-weight: bold; max-height: 376px; overflow-y: auto; scrollbar-width: none; -ms-overflow-style: none; word-break: break-word; }
        #isiTreasury::-webkit-scrollbar { display: none; }
        .dark-mode #isiTreasury { color: #00E124; text-shadow: 1px 1px #00B31C; }
        #ingfo { width: 218px; border: 1px solid #ccc; padding: 10px; height: 378px; overflow-y: auto; }
        .live-indicator { display: inline-block; width: 10px; height: 10px; background: #00ff00; border-radius: 50%; margin-right: 8px; animation: pulse 1s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        #totalData { color: #ff5722; font-size: 1.1em; }
        .dark-mode #totalData { color: #ffb300; }
        #statusConnection { padding: 5px 10px; border-radius: 5px; font-weight: bold; }
        .connected { background: #4caf50; color: white; }
        .disconnected { background: #f44336; color: white; }
    </style>
</head>
<body>
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
        <h2 style="margin:0;"><span class="live-indicator"></span>MONITORING Harga Emas Treasury</h2>
        <div>
            <span id="statusConnection" class="disconnected">Disconnected</span>
            <button class="theme-toggle-btn" id="themeBtn" onclick="toggleTheme()" title="Ganti Tema">🌙</button>
        </div>
    </div>
    <div id="jam" style="font-size:1.3em; color:#ff1744; font-weight:bold; margin-bottom:15px;"></div>
    <p id="totalData">Total Data: 0 baris</p>
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
            "width": "100%",
            "height": 400,
            "symbol": "OANDA:XAUUSD",
            "interval": "15",
            "timezone": "Asia/Jakarta",
            "theme": "light",
            "style": "1",
            "locale": "id",
            "toolbar_bg": "#f1f3f6",
            "enable_publishing": false,
            "hide_top_toolbar": false,
            "save_image": false,
            "container_id": "tradingview_chart"
        });
        </script>
    </div>
    
    <div class="container-flex">
        <div>
            <h3 style="display:block; margin-top:30px;">Chart Harga USD/IDR Investing</h3>
            <div style="overflow:hidden; height:370px; width:620px; border:1px solid #ccc; border-radius:6px;">
                <iframe src="https://sslcharts.investing.com/index.php?force_lang=54&pair_ID=2138&timescale=900&candles=80&style=candles" width="618" height="430" style="margin-top:-62px; border:0;" scrolling="no" frameborder="0" allowtransparency="true"></iframe>
            </div>
        </div>
        <div>
            <h3>Harga USD/IDR Google Finance</h3>
            <div id="usdIdrRealtime" style="margin-top:0; padding-top:2px;">
                <p>Harga saat ini: <span id="currentPrice">Loading...</span></p>
                <h4>Harga Terakhir:</h4>
                <ul id="priceList" style="list-style:none; padding-left:0; max-height:275px; overflow-y:auto;"></ul>
            </div>
        </div>
    </div>
    
    <div class="container-flex">
        <div>
            <h3 style="display:block; margin-top:30px;">Kalender Ekonomi</h3>
            <div style="overflow:hidden; height:470px; width:100%; border:1px solid #ccc; border-radius:6px;">
                <iframe src="https://sslecal2.investing.com?columns=exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous&category=_employment,_economicActivity,_inflation,_centralBanks,_confidenceIndex&importance=3&features=datepicker,timezone,timeselector,filters&countries=5,37,48,35,17,36,26,12,72&calType=week&timeZone=27&lang=54" width="650" height="467" frameborder="0" allowtransparency="true" marginwidth="0" marginheight="0"></iframe>
            </div>
        </div>
        <div>
            <h3>Sekilas Ingfo Treasury</h3>
            <div id="ingfo" style="margin-top:0; padding-top:2px;">
                <ul id="isiTreasury" style="list-style:none; padding-left:0;"></ul>
            </div>
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

        function updateTable(history) {
            document.getElementById("totalData").textContent = "Total Data: " + history.length + " baris";
            
            if (history.length === 0) return;
            
            history.sort(function(a, b) {
                return new Date(b.created_at) - new Date(a.created_at);
            });
            var dataArr = history.map(function(d) {
                return {
                    waktu: d.created_at,
                    all: (d.status || "➖") + " | Harga Beli: " + d.buying_rate + " | Harga Jual: " + d.selling_rate,
                    jt20: d.jt20,
                    jt30: d.jt30
                };
            });
            table.clear();
            table.rows.add(dataArr);
            table.draw(false);
        }

        var ws = null;
        var reconnectDelay = 500;
        var statusEl = document.getElementById("statusConnection");

        function connectWS() {
            var wsUrl = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
            console.log("Connecting to:", wsUrl);
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = function() {
                console.log("WebSocket connected");
                statusEl.textContent = "Connected";
                statusEl.className = "connected";
                reconnectDelay = 500;
            };
            
            ws.onmessage = function(event) {
                try {
                    var data = JSON.parse(event.data);
                    console.log("Received:", data);
                    
                    if (data.ping) {
                        console.log("Ping received");
                        return;
                    }
                    
                    if (data.history && data.history.length > 0) {
                        console.log("History length:", data.history.length);
                        updateTable(data.history);
                    }
                    
                    if (data.usd_idr_history && data.usd_idr_history.length > 0) {
                        console.log("USD/IDR history length:", data.usd_idr_history.length);
                        updateUsdIdrPrice(data.usd_idr_history);
                    }
                    
                    if (data.treasury_info !== undefined) {
                        updateTreasuryInfo(data.treasury_info);
                    }
                } catch (e) {
                    console.error("Error parsing message:", e);
                }
            };
            
            ws.onclose = function(e) {
                console.log("WebSocket closed:", e.code, e.reason);
                statusEl.textContent = "Disconnected";
                statusEl.className = "disconnected";
                setTimeout(connectWS, reconnectDelay);
                reconnectDelay = Math.min(reconnectDelay * 1.5, 5000);
            };
            
            ws.onerror = function(err) {
                console.error("WebSocket error:", err);
                ws.close();
            };
        }
        
        connectWS();

        function updateTreasuryInfo(info) {
            document.getElementById("isiTreasury").innerHTML = info;
        }
        
        function updateUsdIdrPrice(history) {
            const currentPriceEl = document.getElementById("currentPrice");
            const priceListEl = document.getElementById("priceList");

            if (!history || history.length === 0) return;

            function parseHarga(str) {
                return parseFloat(str.trim().replace(/\./g, '').replace(',', '.'));
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
                const iconSpan = document.createElement("span");
                iconSpan.textContent = " " + rowIcon;
                li.appendChild(iconSpan);
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
            var body = document.body;
            var btn = document.getElementById('themeBtn');
            body.classList.toggle('dark-mode');
            if (body.classList.contains('dark-mode')) {
                btn.textContent = "☀️";
                localStorage.setItem('theme', 'dark');
            } else {
                btn.textContent = "🌙";
                localStorage.setItem('theme', 'light');
            }
        }
        (function() {
            var theme = localStorage.getItem('theme');
            var btn = document.getElementById('themeBtn');
            if (theme === 'dark') {
                document.body.classList.add('dark-mode');
                btn.textContent = "☀️";
            } else {
                btn.textContent = "🌙";
            }
        })();
    </script>
    <footer id="footerApp">
        <span class="marquee-text">&copy;2025 ~ahmadkholil~</span>
    </footer>
</body>
</html>
"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting background tasks...")
    
    task1 = asyncio.create_task(api_loop())
    task2 = asyncio.create_task(usd_idr_loop())
    task3 = asyncio.create_task(broadcast_loop())
    
    await start_telegram_bot()
    
    yield
    
    print("Shutting down...")
    
    task1.cancel()
    task2.cancel()
    task3.cancel()
    
    await stop_telegram_bot()
    
    try:
        await asyncio.gather(task1, task2, task3, return_exceptions=True)
    except:
        pass

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(html)

@app.get("/debug")
async def debug():
    async with history_lock:
        h_count = len(history)
    async with usd_idr_lock:
        u_count = len(usd_idr_history)
    return {
        "history_count": h_count,
        "usd_idr_count": u_count,
        "active_connections": len(active_connections),
        "treasury_info": treasury_info[:50] + "..." if len(treasury_info) > 50 else treasury_info
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    
    print(f"New WebSocket connection. Total: {len(active_connections)}")
    
    try:
        async with history_lock:
            history_snapshot = list(history[-1441:])
        
        async with usd_idr_lock:
            usd_idr_snapshot = list(usd_idr_history)
        
        print(f"Sending initial data - History: {len(history_snapshot)}, USD/IDR: {len(usd_idr_snapshot)}")
        
        initial_data = {
            "history": [
                {
                    "buying_rate": format_rupiah(h["buying_rate"]),
                    "selling_rate": format_rupiah(h["selling_rate"]),
                    "status": h["status"],
                    "created_at": h["created_at"],
                    "jt20": calc_20jt(h),
                    "jt30": calc_30jt(h)
                }
                for h in history_snapshot
            ],
            "usd_idr_history": usd_idr_snapshot,
            "treasury_info": treasury_info
        }
        
        await websocket.send_text(json.dumps(initial_data))
        
    except Exception as e:
        print(f"Error sending initial data: {e}")
        active_connections.discard(websocket)
        return

    try:
        while True:
            await asyncio.sleep(30)
            try:
                await websocket.send_text(json.dumps({"ping": True}))
            except:
                break
    except WebSocketDisconnect:
        pass
    except:
        pass
    finally:
        active_connections.discard(websocket)
        print(f"WebSocket disconnected. Total: {len(active_connections)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
