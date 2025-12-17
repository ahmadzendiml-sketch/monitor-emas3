import asyncio
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
import httpx
from bs4 import BeautifulSoup
from collections import deque
from typing import Set
import time

# ==================== FAST JSON (10x faster) ====================
try:
    import orjson
    def json_dumps(obj):
        return orjson.dumps(obj).decode('utf-8')
    def json_loads(s):
        return orjson.loads(s)
    print("✅ Using orjson (fast mode)")
except ImportError:
    import json
    def json_dumps(obj):
        return json.dumps(obj, separators=(',', ':'))  # compact JSON
    def json_loads(s):
        return json.loads(s)
    print("⚠️ orjson not installed, using standard json. Install with: pip install orjson")

# ==================== GLOBAL VARIABLES (Optimized) ====================
# Menggunakan deque untuk O(1) append dan pop
history = deque(maxlen=1441)
usd_idr_history = deque(maxlen=11)

last_buy = None
treasury_info = "Belum ada info treasury."

# Connection management
active_connections: Set[WebSocket] = set()

# Broadcast queues - setiap connection punya queue sendiri
broadcast_queues: dict = {}

# Telegram app reference
telegram_app = None

# Pre-computed cache untuk mengurangi CPU usage
_cache = {
    "history_json": None,
    "usd_idr_json": None,
    "treasury_info": None,
    "last_update": 0
}

# ==================== HELPER FUNCTIONS (Optimized) ====================
# Pre-compute format untuk angka yang sering digunakan
_format_cache = {}

def format_rupiah(nominal):
    if nominal in _format_cache:
        return _format_cache[nominal]
    try:
        result = "{:,}".format(int(nominal)).replace(",", ".")
        if len(_format_cache) < 10000:  # Limit cache size
            _format_cache[nominal] = result
        return result
    except:
        return str(nominal)

def parse_price_to_float(price_str):
    try:
        return float(price_str.replace('.', '').replace(',', '.'))
    except:
        return None

def calc_profit(buying_rate, selling_rate, investment, fee):
    """Optimized profit calculation"""
    try:
        val = int((investment / buying_rate) * selling_rate - fee)
        if val > 0:
            return f"+{format_rupiah(val)} 🟢"
        elif val < 0:
            return f"-{format_rupiah(abs(val))} 🔴"
        return "0 ➖"
    except:
        return "-"

def prepare_history_item(h):
    """Pre-format history item untuk broadcast"""
    buying = h["buying_rate"]
    selling = h["selling_rate"]
    return {
        "buying_rate": format_rupiah(buying),
        "selling_rate": format_rupiah(selling),
        "status": h["status"],
        "created_at": h["created_at"],
        "jt20": calc_profit(buying, selling, 20000000, 19315000),
        "jt30": calc_profit(buying, selling, 30000000, 28980000)
    }

# ==================== ULTRA-FAST BROADCAST ====================
async def broadcast_to_all(message_dict):
    """Broadcast message ke semua connections secara parallel"""
    if not active_connections:
        return
    
    # Serialize sekali saja
    message = json_dumps(message_dict)
    
    # Broadcast parallel ke semua connections
    tasks = []
    dead_connections = []
    
    for ws in active_connections:
        try:
            tasks.append(ws.send_text(message))
        except:
            dead_connections.append(ws)
    
    # Cleanup dead connections
    for ws in dead_connections:
        active_connections.discard(ws)
    
    # Send all at once
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

async def broadcast_update():
    """Broadcast update terbaru ke semua clients"""
    msg = {
        "history": [prepare_history_item(h) for h in history],
        "usd_idr_history": list(usd_idr_history),
        "treasury_info": treasury_info
    }
    await broadcast_to_all(msg)

# ==================== FETCH FUNCTIONS (Optimized) ====================
# Reusable HTTP client dengan connection pooling
_http_client = None

async def get_http_client():
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            http2=True  # HTTP/2 untuk multiplexing
        )
    return _http_client

async def fetch_usd_idr_price():
    url = "https://www.google.com/finance/quote/USD-IDR"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        client = await get_http_client()
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")  # lxml lebih cepat dari html.parser
        price_div = soup.find("div", class_="YMlKec fxKbKc")
        if price_div:
            return price_div.text.strip()
    except Exception as e:
        print(f"Error fetching USD/IDR: {e}")
    return None

# ==================== BACKGROUND LOOPS (Optimized) ====================
async def api_loop():
    """Ultra-fast API polling loop"""
    global last_buy
    
    api_url = "https://api.treasury.id/api/v1/antigrvty/gold/rate"
    shown_updates = set()
    
    client = await get_http_client()
    
    # Gunakan minimal delay
    min_delay = 0.1  # 100ms between requests
    
    while True:
        start_time = time.monotonic()
        
        try:
            response = await client.post(api_url)
            
            if response.status_code == 200:
                data = response.json().get("data", {})
                buying_rate = int(data.get("buying_rate", 0))
                selling_rate = int(data.get("selling_rate", 0))
                updated_at = data.get("updated_at")
                
                if updated_at and updated_at not in shown_updates:
                    # Determine status
                    if last_buy is None:
                        status = "➖"
                    elif buying_rate > last_buy:
                        status = "🚀"
                    elif buying_rate < last_buy:
                        status = "🔻"
                    else:
                        status = "➖"
                    
                    # Add to history
                    history.append({
                        "buying_rate": buying_rate,
                        "selling_rate": selling_rate,
                        "status": status,
                        "created_at": updated_at
                    })
                    
                    last_buy = buying_rate
                    shown_updates.add(updated_at)
                    
                    # Limit shown_updates size
                    if len(shown_updates) > 2000:
                        shown_updates = set(list(shown_updates)[-1000:])
                    
                    # INSTANT BROADCAST!
                    asyncio.create_task(broadcast_update())
                    
        except Exception as e:
            print(f"API loop error: {e}")
        
        # Adaptive delay - faster when there's activity
        elapsed = time.monotonic() - start_time
        delay = max(0.05, min_delay - elapsed)  # Minimum 50ms
        await asyncio.sleep(delay)

async def usd_idr_loop():
    """USD/IDR price loop dengan instant broadcast"""
    min_delay = 0.5  # 500ms between requests
    
    while True:
        start_time = time.monotonic()
        
        try:
            price_str = await fetch_usd_idr_price()
            
            if price_str:
                # Check if price changed
                if not usd_idr_history or usd_idr_history[-1]["price"] != price_str:
                    wib_now = datetime.utcnow() + timedelta(hours=7)
                    usd_idr_history.append({
                        "price": price_str,
                        "time": wib_now.strftime("%H:%M:%S")
                    })
                    
                    # INSTANT BROADCAST!
                    asyncio.create_task(broadcast_update())
                    
        except Exception as e:
            print(f"USD/IDR loop error: {e}")
        
        elapsed = time.monotonic() - start_time
        delay = max(0.1, min_delay - elapsed)
        await asyncio.sleep(delay)

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
        await update.message.reply_text("🤖 Bot aktif! Gunakan /atur <teks> untuk mengubah info treasury.")

    async def atur_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global treasury_info
        text = update.message.text.partition(' ')[2]
        
        if text:
            text = text.replace("  ", "&nbsp;&nbsp;").replace("\n", "<br>")
            treasury_info = text
            
            # INSTANT BROADCAST!
            asyncio.create_task(broadcast_update())
            
            await update.message.reply_text("✅ Info Treasury berhasil diubah!")
        else:
            await update.message.reply_text("❌ Gunakan: /atur <kalimat info>")

    try:
        telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_handler))
        telegram_app.add_handler(CommandHandler("atur", atur_handler))
        
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        
        print("✅ Telegram bot started!")
        return telegram_app
        
    except Exception as e:
        print(f"❌ Telegram bot error: {e}")
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
    <title>Harga Emas Treasury</title>
    <link rel="stylesheet" type="text/css" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css"/>
    <style>
        body { font-family: Arial; margin: 40px; background: #fff; color: #222; transition: background 0.3s, color 0.3s; }
        table.dataTable thead th { font-weight: bold; }
        th.waktu, td.waktu {
            width: 150px;
            min-width: 100px;
            max-width: 180px;
            white-space: nowrap;
            text-align: left;
        }
        th.profit, td.profit {
            width: 90px;
            min-width: 80px;
            max-width: 100px;
            white-space: nowrap;
            text-align: left;
        }
        .dark-mode { background: #181a1b !important; color: #e0e0e0 !important; }
        .dark-mode #jam { color: #ffb300 !important; }
        .dark-mode table.dataTable { background: #23272b !important; color: #e0e0e0 !important; }
        .dark-mode table.dataTable thead th { background: #23272b !important; color: #ffb300 !important; }
        .dark-mode table.dataTable tbody td { background: #23272b !important; color: #e0e0e0 !important; }
        .theme-toggle-btn {
            padding: 0;
            border: none;
            border-radius: 50%;
            background: #222;
            color: #fff;
            font-weight: bold;
            cursor: pointer;
            transition: background 0.3s, color 0.3s;
            font-size: 1.5em;
            width: 44px;
            height: 44px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .theme-toggle-btn:hover {
            background: #444;
        }
        .dark-mode .theme-toggle-btn {
            background: #ffb300;
            color: #222;
        }
        .dark-mode .theme-toggle-btn:hover {
            background: #ffd54f;
        }
        .container-flex {
            display: flex;
            gap: 15px;
            margin-top: 10px;
        }
        #usdIdrWidget {
            overflow:hidden; height:370px; width:630px; border:1px solid #ccc; border-radius:6px;
        }
        #usdIdrRealtime {
            width: 248px;
            border: 1px solid #ccc;
            padding: 10px;
            height: 370px;
            overflow-y: auto;
        }
        #priceList li {
            margin-bottom: 1px;
        }
        .time {
            color: gray;
            font-size: 0.9em;
            margin-left: 10px;
        }
        #currentPrice {
        color: red;
        font-weight: bold;
        }
        .dark-mode #currentPrice {
            color: #00E124; text-shadow: 1px 1px #00B31C;
        }
        #tabel tbody tr:first-child td {
            color: red !important;
            font-weight: bold;
        }
        .dark-mode #tabel tbody tr:first-child td {
            color: #00E124 !important;
            font-weight: bold;
        }
        #footerApp {
            width: 100%;
            overflow: hidden;
            position: fixed;
            bottom: 0;
            left: 0;
            background: transparent;
            text-align: center;
            z-index: 100;
            padding: 8px 0;
        }
        .marquee-text {
            display: inline-block;
            color: #F5274D;
            animation: marquee 70s linear infinite;
            font-weight: bold;
        }
        .dark-mode .marquee-text {
            color: #B232B2;
            font-weight: bold;
        }
        @keyframes marquee {
            0%   { transform: translateX(100vw); }
            100% { transform: translateX(-100vw); }
        }
        #isiTreasury { 
            white-space: pre-line; 
            color: red;
            font-weight: bold;
            max-height: 376px;
            overflow-y: auto;
            scrollbar-width: none; 
            -ms-overflow-style: none;  
            word-break: break-word;
        }
        #isiTreasury::-webkit-scrollbar {
            display: none; 
        }
        .dark-mode #isiTreasury {
            color: #00E124; text-shadow: 1px 1px #00B31C;
        }
        #ingfo {
            width: 218px;
            border: 1px solid #ccc;
            padding: 10px;
            height: 378px;
            overflow-y: auto;
        }
        /* Status indicator */
        #wsStatus {
            position: fixed;
            top: 10px;
            right: 10px;
            padding: 5px 10px;
            border-radius: 5px;
            font-size: 12px;
            font-weight: bold;
            z-index: 1000;
        }
        .ws-connected { background: #4CAF50; color: white; }
        .ws-disconnected { background: #f44336; color: white; }
        .ws-connecting { background: #ff9800; color: white; }
    </style>
</head>
<body>
    <div id="wsStatus" class="ws-connecting">⏳ Connecting...</div>
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
        <h2 style="margin:0;">MONITORING Harga Emas Treasury</h2>
        <button class="theme-toggle-btn" id="themeBtn" onclick="toggleTheme()" title="Ganti Tema" style="margin-left:20px; font-size:1.5em; width:44px; height:44px; display:flex; align-items:center; justify-content:center;">
            🌙
        </button>
    </div>
    <div id="jam" style="font-size:1.3em; color:#ff1744; font-weight:bold; margin-bottom:15px;"></div>
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
            <h3 style="display:block; margin-top:30px;">Chart Harga USD/IDR Investing - Jangka Waktu 15 Menit</h3>
        <div style="overflow:hidden; height:370px; width:620px; border:1px solid #ccc; border-radius:6px;">
        <iframe 
            src="https://sslcharts.investing.com/index.php?force_lang=54&pair_ID=2138&timescale=900&candles=80&style=candles"
            width="618"
            height="430"
            style="margin-top:-62px; border:0;"
            scrolling="no"
            frameborder="0"
            allowtransparency="true">
        </iframe>
        </div>
        </div>

        <div>
            <h3>Harga USD/IDR Google Finance</h3>
            <div id="usdIdrRealtime" style="margin-top:0; padding-top:2px;">
            <p>Harga saat ini: <span id="currentPrice" style="color: red; font-weight: bold;">Loading...</span></p>
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
        </div></div>
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
        // ==================== OPTIMIZED JAVASCRIPT ====================
        var table = $('#tabel').DataTable({
            "pageLength": 4,
            "lengthMenu": [4, 8, 18, 48, 88, 888, 1441],
            "order": [],
            "columns": [
                { "data": "waktu" },
                { "data": "all" },
                { "data": "jt20" },
                { "data": "jt30" }
            ],
            "deferRender": true  // Optimasi render
        });

        // Cache DOM elements
        var wsStatusEl = document.getElementById('wsStatus');
        var currentPriceEl = document.getElementById("currentPrice");
        var priceListEl = document.getElementById("priceList");
        var isiTreasuryEl = document.getElementById("isiTreasury");
        var jamEl = document.getElementById("jam");

        // Debounce function untuk menghindari update berlebihan
        function debounce(func, wait) {
            var timeout;
            return function() {
                var context = this, args = arguments;
                clearTimeout(timeout);
                timeout = setTimeout(function() {
                    func.apply(context, args);
                }, wait);
            };
        }

        // Optimized table update dengan requestAnimationFrame
        var pendingTableUpdate = null;
        function updateTable(history) {
            if (pendingTableUpdate) {
                cancelAnimationFrame(pendingTableUpdate);
            }
            pendingTableUpdate = requestAnimationFrame(function() {
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
                table.page('first').draw(false);
                pendingTableUpdate = null;
            });
        }

        // WebSocket dengan auto-reconnect
        var ws = null;
        var reconnectAttempts = 0;
        var maxReconnectAttempts = 100;
        var reconnectDelay = 100; // Start dengan 100ms

        function updateWsStatus(status, text) {
            wsStatusEl.className = 'ws-' + status;
            wsStatusEl.textContent = text;
        }

        function connectWS() {
            updateWsStatus('connecting', '⏳ Connecting...');
            
            var protocol = location.protocol === "https:" ? "wss://" : "ws://";
            ws = new WebSocket(protocol + location.host + "/ws");
            
            ws.onopen = function() {
                console.log("✅ WebSocket connected!");
                updateWsStatus('connected', '🟢 Live');
                reconnectAttempts = 0;
                reconnectDelay = 100;
            };
            
            ws.onmessage = function(event) {
                try {
                    var data = JSON.parse(event.data);
                    
                    // Skip ping messages
                    if (data.ping) return;
                    
                    // Update components
                    if (data.history) updateTable(data.history);
                    if (data.usd_idr_history) updateUsdIdrPrice(data.usd_idr_history);
                    if (data.treasury_info !== undefined) updateTreasuryInfo(data.treasury_info);
                } catch(e) {
                    console.error("Parse error:", e);
                }
            };
            
            ws.onclose = function(e) {
                console.log("WebSocket closed:", e.code, e.reason);
                updateWsStatus('disconnected', '🔴 Disconnected');
                
                if (reconnectAttempts < maxReconnectAttempts) {
                    reconnectAttempts++;
                    console.log("Reconnecting in " + reconnectDelay + "ms... (attempt " + reconnectAttempts + ")");
                    setTimeout(connectWS, reconnectDelay);
                    reconnectDelay = Math.min(reconnectDelay * 1.5, 5000); // Max 5s
                }
            };
            
            ws.onerror = function(error) {
                console.error("WebSocket error:", error);
            };
        }
        
        connectWS();

        function updateTreasuryInfo(info) {
            isiTreasuryEl.innerHTML = info;
        }
        
        function parseHarga(str) {
            return parseFloat(str.trim().replace(/\\./g, '').replace(',', '.'));
        }
        
        function updateUsdIdrPrice(history) {
            if (!history || history.length === 0) return;
            
            var reversed = history.slice().reverse();
            
            // Current price with icon
            var rowIconCurrent = "➖";
            if (reversed.length > 1) {
                var now = parseHarga(reversed[0].price);
                var prev = parseHarga(reversed[1].price);
                if (now > prev) rowIconCurrent = "🚀";
                else if (now < prev) rowIconCurrent = "🔻";
            }
            currentPriceEl.innerHTML = reversed[0].price + " " + rowIconCurrent;

            // Price list - use DocumentFragment for better performance
            var fragment = document.createDocumentFragment();
            
            for (var i = 0; i < reversed.length; i++) {
                var rowIcon = "➖";
                if (i < reversed.length - 1) {
                    var now = parseHarga(reversed[i].price);
                    var next = parseHarga(reversed[i+1].price);
                    if (now > next) rowIcon = "🟢";
                    else if (now < next) rowIcon = "🔴";
                }
                
                var li = document.createElement("li");
                li.textContent = reversed[i].price + " ";
                
                var spanTime = document.createElement("span");
                spanTime.className = "time";
                spanTime.textContent = "(" + reversed[i].time + ")";
                li.appendChild(spanTime);
                
                var iconSpan = document.createElement("span");
                iconSpan.textContent = " " + rowIcon;
                li.appendChild(iconSpan);

                fragment.appendChild(li);
            }
            
            priceListEl.innerHTML = "";
            priceListEl.appendChild(fragment);
        }
        
        // Optimized clock update
        function updateJam() {
            var now = new Date();
            var tgl = now.toLocaleDateString('id-ID', { day: '2-digit', month: 'long', year: 'numeric' });
            var jam = now.toLocaleTimeString('id-ID', { hour12: false });
            jamEl.textContent = tgl + " " + jam + " WIB";
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
        
        // Initialize theme
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
        
        // Visibility API - pause updates when tab is hidden
        document.addEventListener('visibilitychange', function() {
            if (document.hidden) {
                console.log("Tab hidden - reducing updates");
            } else {
                console.log("Tab visible - resuming updates");
                // Force reconnect if disconnected
                if (!ws || ws.readyState !== WebSocket.OPEN) {
                    connectWS();
                }
            }
        });
    </script>
<footer id="footerApp">
    <span class="marquee-text">&copy;2025 ~ahmadkholil~</span>
</footer>
</body>
</html>
"""

# ==================== LIFESPAN ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 50)
    print("🚀 Starting ULTRA-FAST application...")
    print("=" * 50)
    
    # Start background tasks
    task1 = asyncio.create_task(api_loop())
    task2 = asyncio.create_task(usd_idr_loop())
    print("✅ API loop started (100ms interval)")
    print("✅ USD/IDR loop started (500ms interval)")
    
    # Start telegram bot
    await start_telegram_bot()
    
    print("=" * 50)
    print("🎉 All services running at MAXIMUM SPEED!")
    print("=" * 50)
    
    yield
    
    # Shutdown
    print("🛑 Shutting down...")
    task1.cancel()
    task2.cancel()
    await stop_telegram_bot()
    
    # Close HTTP client
    global _http_client
    if _http_client:
        await _http_client.aclose()
    
    try:
        await asyncio.gather(task1, task2, return_exceptions=True)
    except:
        pass
    print("✅ Application stopped!")

# ==================== FASTAPI APP ====================
app = FastAPI(lifespan=lifespan)

# ==================== ROUTES ====================
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(html)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    
    # Send initial data immediately
    try:
        initial_msg = {
            "history": [prepare_history_item(h) for h in history],
            "usd_idr_history": list(usd_idr_history),
            "treasury_info": treasury_info
        }
        await websocket.send_text(json_dumps(initial_msg))
    except Exception as e:
        print(f"Error sending initial data: {e}")
        active_connections.discard(websocket)
        return

    # Keep connection alive
    try:
        while True:
            # Wait for client messages (ping/pong)
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Send ping to keep alive
                await websocket.send_text(json_dumps({"ping": True}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        active_connections.discard(websocket)

# ==================== MAIN ====================
if __name__ == "__main__":
    import uvicorn
    
    # Optimized uvicorn settings
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        loop="uvloop",  # Faster event loop (install: pip install uvloop)
        http="httptools",  # Faster HTTP parser (install: pip install httptools)
        ws="websockets",  # WebSocket implementation
        log_level="warning",  # Reduce logging overhead
        access_log=False,  # Disable access log for speed
    )
