import asyncio
import json
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
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

async def test_treasury_api():
    api_url = "https://api.treasury.id/api/v1/antigrvty/gold/rate"
    results = {}
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.post(api_url)
            results["post"] = {
                "status": response.status_code,
                "body": response.text[:500]
            }
        except Exception as e:
            results["post"] = {"error": str(e)}
        
        try:
            response = await client.get(api_url)
            results["get"] = {
                "status": response.status_code,
                "body": response.text[:500]
            }
        except Exception as e:
            results["get"] = {"error": str(e)}
    
    return results

async def test_usd_idr():
    url = "https://www.google.com/finance/quote/USD-IDR"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(url, headers=headers, follow_redirects=True)
            soup = BeautifulSoup(response.text, "html.parser")
            price_div = soup.find("div", class_="YMlKec fxKbKc")
            
            return {
                "status": response.status_code,
                "price_found": price_div.text.strip() if price_div else None,
                "html_snippet": response.text[:1000]
            }
        except Exception as e:
            return {"error": str(e)}

async def fetch_treasury_price(client):
    api_url = "https://api.treasury.id/api/v1/antigrvty/gold/rate"
    
    try:
        response = await client.post(api_url)
        if response.status_code == 200:
            return response.json()
    except:
        pass
    
    try:
        response = await client.get(api_url)
        if response.status_code == 200:
            return response.json()
    except:
        pass
    
    return None

async def fetch_usd_idr_price(client):
    url = "https://www.google.com/finance/quote/USD-IDR"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = await client.get(url, headers=headers, follow_redirects=True)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            price_div = soup.find("div", class_="YMlKec fxKbKc")
            if price_div:
                return price_div.text.strip()
    except Exception as e:
        print(f"USD/IDR fetch error: {e}")
    
    return None

async def api_loop():
    global last_buy
    
    shown_updates = set()
    
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
        print("API Loop started...")
        
        while True:
            try:
                json_data = await fetch_treasury_price(client)
                
                if json_data:
                    data = json_data.get("data", {})
                    buying_rate = int(data.get("buying_rate", 0))
                    selling_rate = int(data.get("selling_rate", 0))
                    updated_at = data.get("updated_at")
                    
                    print(f"Treasury: Buy={buying_rate}, Sell={selling_rate}, Time={updated_at}")
                    
                    if buying_rate > 0 and selling_rate > 0:
                        if updated_at is None:
                            wib_now = datetime.utcnow() + timedelta(hours=7)
                            updated_at = wib_now.strftime("%Y-%m-%d %H:%M:%S")
                        
                        if updated_at not in shown_updates:
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
                                print(f"History added. Total: {len(history)}")
                            
                            last_buy = buying_rate
                            shown_updates.add(updated_at)
                            
                            if len(shown_updates) > 2000:
                                shown_updates.clear()
                            
                            update_event.set()
                else:
                    print("Treasury API returned no data")
                    
            except Exception as e:
                print(f"API Loop error: {e}")
            
            await asyncio.sleep(1.0)

async def usd_idr_loop():
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
        print("USD/IDR Loop started...")
        
        while True:
            try:
                price_str = await fetch_usd_idr_price(client)
                
                if price_str:
                    print(f"USD/IDR: {price_str}")
                    
                    async with usd_idr_lock:
                        if not usd_idr_history or usd_idr_history[-1]["price"] != price_str:
                            wib_now = datetime.utcnow() + timedelta(hours=7)
                            usd_idr_history.append({
                                "price": price_str,
                                "time": wib_now.strftime("%H:%M:%S")
                            })
                            if len(usd_idr_history) > 11:
                                del usd_idr_history[:-11]
                            print(f"USD/IDR added. Total: {len(usd_idr_history)}")
                            usd_idr_update_event.set()
                else:
                    print("USD/IDR fetch returned None")
                
            except Exception as e:
                print(f"USD/IDR Loop error: {e}")
            
            await asyncio.sleep(3.0)

async def broadcast_loop():
    print("Broadcast Loop started...")
    
    while True:
        try:
            tasks = [
                asyncio.create_task(update_event.wait()),
                asyncio.create_task(usd_idr_update_event.wait()),
                asyncio.create_task(treasury_info_update_event.wait())
            ]
            
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            
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
                
                for ws in list(active_connections):
                    try:
                        await asyncio.wait_for(ws.send_text(msg), timeout=5.0)
                    except:
                        active_connections.discard(ws)
                
        except Exception as e:
            print(f"Broadcast error: {e}")
            await asyncio.sleep(1.0)

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
        await telegram_app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message"])
        
        print("Telegram bot started!")
        return telegram_app
        
    except Exception as e:
        print(f"Telegram bot error: {e}")
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

html = """
<!DOCTYPE html>
<html>
<head>
    <title>Harga Emas Treasury</title>
    <link rel="stylesheet" type="text/css" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css"/>
    <style>
        body { font-family: Arial; margin: 40px; background: #fff; color: #222; }
        .dark-mode { background: #181a1b !important; color: #e0e0e0 !important; }
        .dark-mode #jam { color: #ffb300 !important; }
        .dark-mode table.dataTable { background: #23272b !important; color: #e0e0e0 !important; }
        .dark-mode table.dataTable thead th { background: #23272b !important; color: #ffb300 !important; }
        .dark-mode table.dataTable tbody td { background: #23272b !important; color: #e0e0e0 !important; }
        .theme-toggle-btn { padding: 0; border: none; border-radius: 50%; background: #222; color: #fff; cursor: pointer; font-size: 1.5em; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; }
        .dark-mode .theme-toggle-btn { background: #ffb300; color: #222; }
        .container-flex { display: flex; gap: 15px; margin-top: 10px; }
        #usdIdrRealtime { width: 248px; border: 1px solid #ccc; padding: 10px; height: 370px; overflow-y: auto; }
        .time { color: gray; font-size: 0.9em; margin-left: 10px; }
        #currentPrice { color: red; font-weight: bold; }
        .dark-mode #currentPrice { color: #00E124; }
        #tabel tbody tr:first-child td { color: red !important; font-weight: bold; }
        .dark-mode #tabel tbody tr:first-child td { color: #00E124 !important; }
        #isiTreasury { white-space: pre-line; color: red; font-weight: bold; }
        .dark-mode #isiTreasury { color: #00E124; }
        #ingfo { width: 218px; border: 1px solid #ccc; padding: 10px; height: 378px; overflow-y: auto; }
        .live-indicator { display: inline-block; width: 10px; height: 10px; background: #00ff00; border-radius: 50%; margin-right: 8px; animation: pulse 1s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        #totalData { color: #ff5722; font-size: 1.1em; }
        .dark-mode #totalData { color: #ffb300; }
        .status-box { padding: 5px 10px; border-radius: 5px; font-weight: bold; margin-left: 10px; }
        .connected { background: #4caf50; color: white; }
        .disconnected { background: #f44336; color: white; }
        #footerApp { width: 100%; position: fixed; bottom: 0; left: 0; text-align: center; padding: 8px 0; }
        .marquee-text { color: #F5274D; font-weight: bold; }
        .dark-mode .marquee-text { color: #B232B2; }
        .debug-btn { background: #2196f3; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; margin-left: 10px; }
    </style>
</head>
<body>
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
        <h2 style="margin:0;"><span class="live-indicator"></span>MONITORING Harga Emas Treasury</h2>
        <div style="display: flex; align-items: center;">
            <span id="statusConnection" class="status-box disconnected">Disconnected</span>
            <button class="debug-btn" onclick="testAPI()">Test API</button>
            <button class="debug-btn" onclick="addDummy()">Add Dummy</button>
            <button class="theme-toggle-btn" id="themeBtn" onclick="toggleTheme()">🌙</button>
        </div>
    </div>
    <div id="jam" style="font-size:1.3em; color:#ff1744; font-weight:bold; margin-bottom:15px;"></div>
    <p id="totalData">Total Data: 0 baris</p>
    <div id="debugInfo" style="background:#ffffcc; padding:10px; margin-bottom:10px; display:none; white-space:pre-wrap; font-family:monospace; font-size:12px;"></div>
    
    <table id="tabel" class="display" style="width:100%">
        <thead>
            <tr>
                <th>Waktu</th>
                <th>Data Transaksi</th>
                <th>Est. cuan 20 JT</th>
                <th>Est. cuan 30 JT</th>
            </tr>
        </thead>
        <tbody></tbody>
    </table>
    
    <div style="margin-top:40px;">
        <h3>Chart Harga Emas (XAU/USD)</h3>
        <div id="tradingview_chart"></div>
        <script src="https://s3.tradingview.com/tv.js"></script>
        <script>
        new TradingView.widget({
            "width": "100%",
            "height": 400,
            "symbol": "OANDA:XAUUSD",
            "interval": "15",
            "timezone": "Asia/Jakarta",
            "theme": "light",
            "style": "1",
            "locale": "id",
            "container_id": "tradingview_chart"
        });
        </script>
    </div>
    
    <div class="container-flex">
        <div>
            <h3 style="margin-top:30px;">Chart Harga USD/IDR Investing</h3>
            <div style="overflow:hidden; height:370px; width:620px; border:1px solid #ccc; border-radius:6px;">
                <iframe src="https://sslcharts.investing.com/index.php?force_lang=54&pair_ID=2138&timescale=900&candles=80&style=candles" width="618" height="430" style="margin-top:-62px; border:0;"></iframe>
            </div>
        </div>
        <div>
            <h3>Harga USD/IDR Google Finance</h3>
            <div id="usdIdrRealtime">
                <p>Harga saat ini: <span id="currentPrice">Loading...</span></p>
                <h4>Harga Terakhir:</h4>
                <ul id="priceList" style="list-style:none; padding-left:0;"></ul>
            </div>
        </div>
    </div>
    
    <div class="container-flex">
        <div>
            <h3 style="margin-top:30px;">Kalender Ekonomi</h3>
            <div style="overflow:hidden; height:470px; width:650px; border:1px solid #ccc; border-radius:6px;">
                <iframe src="https://sslecal2.investing.com?columns=exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous&category=_employment,_economicActivity,_inflation,_centralBanks,_confidenceIndex&importance=3&features=datepicker,timezone,timeselector,filters&countries=5,37,48,35,17,36,26,12,72&calType=week&timeZone=27&lang=54" width="650" height="467" frameborder="0"></iframe>
            </div>
        </div>
        <div>
            <h3>Sekilas Ingfo Treasury</h3>
            <div id="ingfo">
                <div id="isiTreasury"></div>
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
        var statusEl = document.getElementById("statusConnection");

        function connectWS() {
            var wsUrl = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
            console.log("Connecting:", wsUrl);
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = function() {
                console.log("Connected");
                statusEl.textContent = "Connected";
                statusEl.className = "status-box connected";
            };
            
            ws.onmessage = function(event) {
                var data = JSON.parse(event.data);
                console.log("Received:", data);
                
                if (data.ping) return;
                if (data.history) updateTable(data.history);
                if (data.usd_idr_history) updateUsdIdrPrice(data.usd_idr_history);
                if (data.treasury_info !== undefined) {
                    document.getElementById("isiTreasury").innerHTML = data.treasury_info;
                }
            };
            
            ws.onclose = function() {
                console.log("Disconnected");
                statusEl.textContent = "Disconnected";
                statusEl.className = "status-box disconnected";
                setTimeout(connectWS, 2000);
            };
            
            ws.onerror = function(err) {
                console.error("WS Error:", err);
                ws.close();
            };
        }
        connectWS();

        function updateUsdIdrPrice(history) {
            if (!history || history.length === 0) return;
            
            var currentPriceEl = document.getElementById("currentPrice");
            var priceListEl = document.getElementById("priceList");
            var reversed = history.slice().reverse();
            
            currentPriceEl.textContent = reversed[0].price;
            priceListEl.innerHTML = "";
            
            reversed.forEach(function(item) {
                var li = document.createElement("li");
                li.textContent = item.price + " (" + item.time + ")";
                priceListEl.appendChild(li);
            });
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
            btn.textContent = document.body.classList.contains('dark-mode') ? "☀️" : "🌙";
        }
        
        function testAPI() {
            var debugEl = document.getElementById("debugInfo");
            debugEl.style.display = "block";
            debugEl.textContent = "Testing APIs...";
            
            fetch("/test-api")
                .then(r => r.json())
                .then(data => {
                    debugEl.textContent = JSON.stringify(data, null, 2);
                })
                .catch(err => {
                    debugEl.textContent = "Error: " + err;
                });
        }
        
        function addDummy() {
            fetch("/add-dummy")
                .then(r => r.json())
                .then(data => {
                    alert("Dummy data added: " + JSON.stringify(data));
                });
        }
    </script>
    
    <footer id="footerApp">
        <span class="marquee-text">&copy;2025 ~ahmadkholil~</span>
    </footer>
</body>
</html>
"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting...")
    
    task1 = asyncio.create_task(api_loop())
    task2 = asyncio.create_task(usd_idr_loop())
    task3 = asyncio.create_task(broadcast_loop())
    
    await start_telegram_bot()
    
    yield
    
    task1.cancel()
    task2.cancel()
    task3.cancel()
    await stop_telegram_bot()

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(html)

@app.get("/test-api")
async def test_api_endpoint():
    treasury = await test_treasury_api()
    usd_idr = await test_usd_idr()
    
    async with history_lock:
        h_count = len(history)
    async with usd_idr_lock:
        u_count = len(usd_idr_history)
    
    return {
        "treasury_api": treasury,
        "usd_idr_scrape": usd_idr,
        "current_history_count": h_count,
        "current_usd_idr_count": u_count
    }

@app.get("/add-dummy")
async def add_dummy():
    wib_now = datetime.utcnow() + timedelta(hours=7)
    
    async with history_lock:
        history.append({
            "buying_rate": 1850000,
            "selling_rate": 1870000,
            "status": "🚀",
            "created_at": wib_now.strftime("%Y-%m-%d %H:%M:%S")
        })
    
    async with usd_idr_lock:
        usd_idr_history.append({
            "price": "16.250,50",
            "time": wib_now.strftime("%H:%M:%S")
        })
    
    update_event.set()
    usd_idr_update_event.set()
    
    return {"status": "ok", "message": "Dummy data added"}

@app.get("/debug")
async def debug():
    async with history_lock:
        h_data = list(history[-5:])
    async with usd_idr_lock:
        u_data = list(usd_idr_history)
    
    return {
        "history_count": len(history),
        "history_last_5": h_data,
        "usd_idr_count": len(usd_idr_history),
        "usd_idr_data": u_data,
        "connections": len(active_connections)
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    print(f"WS Connected. Total: {len(active_connections)}")
    
    try:
        async with history_lock:
            history_snapshot = list(history[-1441:])
        async with usd_idr_lock:
            usd_idr_snapshot = list(usd_idr_history)
        
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
        print(f"Sent initial: {len(history_snapshot)} history, {len(usd_idr_snapshot)} usd_idr")
        
    except Exception as e:
        print(f"Initial send error: {e}")
        active_connections.discard(websocket)
        return

    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_text(json.dumps({"ping": True}))
    except:
        pass
    finally:
        active_connections.discard(websocket)
        print(f"WS Disconnected. Total: {len(active_connections)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
