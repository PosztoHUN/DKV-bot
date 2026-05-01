from urllib import response
from datetime import datetime, UTC
import discord
from discord.ext import commands, tasks
import aiohttp
import os
import sys
import io
import csv
import zipfile
import asyncio
import requests
from datetime import UTC, datetime, timedelta
from collections import defaultdict
from supabase import create_client
from google.transit import gtfs_realtime_pb2

# =======================
# BEÁLLÍTÁSOK
# =======================

TOKEN = os.getenv("TOKEN")

VEHICLES_API = "https://holajarmu.hu/debrecen/api/vehicles"
REQ_TIMEOUT = aiohttp.ClientTimeout(total=25)
ACTIVE_STATUSES = {"IN_TRANSIT_TO", "STOPPED_AT", "IN_PROGRESS"}



import aiohttp
import asyncio

async def fetch_mav_vehicles():
    """DKV járművek lekérése retry-val"""
    for attempt in range(3):  # 3 próbálkozás
        try:
            async with aiohttp.ClientSession(timeout=REQ_TIMEOUT) as session:
                async with session.get(VEHICLES_API) as resp:

                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("vehicles", [])

                    print(f"API hiba: {resp.status} (próba {attempt+1})")

        except Exception as e:
            print(f"Fetch hiba ({attempt+1}):", e)

        await asyncio.sleep(2)

    return []

LOCK_FILE = "/tmp/discord_bot.lock"
DISCORD_LIMIT = 1900

if os.path.exists(LOCK_FILE):
    print("A bot már fut, kilépés.")
    sys.exit(0)

active_today_villamos = {}
active_today_combino = {}
active_today_caf5 = {}
active_today_caf9 = {}
active_today_tatra = {}
today_data = {}

# =======================
# GTFS / HELYKITÖLTŐK
# =======================

GTFS_PATH = ""
TXT_URL = ""

TRIPS_META = {}
STOPS = {}
TRIP_START = {}
TRIP_STOPS = defaultdict(list)
SERVICE_DATES = defaultdict(dict)
ROUTES = defaultdict(lambda: defaultdict(list))

# =======================
# DISCORD INIT
# =======================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)

# ─────────────────────────────────────────────
# HTTP / FEED SEGÉD
# ─────────────────────────────────────────────

UA_HEADERS = {
    "User-Agent": "BKK-DiscordBot/1.0 (+https://discord.com)"
}

def _http_get(url: str, timeout: int = 15) -> requests.Response:
    r = requests.get(url, headers=UA_HEADERS, timeout=timeout)
    if r.status_code != 200:
        snippet = (r.text or "")[:200].replace("\n", " ").replace("\r", " ")
        raise RuntimeError(f"HTTP {r.status_code} {r.reason}. Válasz eleje: {snippet}")
    return r

def fetch_pb_feed() -> gtfs_realtime_pb2.FeedMessage:
    r = _http_get(PB_URL)
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(r.content)
    except Exception as e:
        snippet = (r.content[:200] or b"").decode("utf-8", errors="replace").replace("\n", " ").replace("\r", " ")
        raise RuntimeError(f"PB parse hiba: {e}. Tartalom eleje: {snippet}")
    return feed

def fetch_txt_raw() -> str:
    r = _http_get(TXT_URL)
    return r.text or ""


# =======================
# SEGÉDFÜGGVÉNYEK
# =======================

def fix_plate(reg: str) -> str:
    if not reg:
        return "Ismeretlen"

    reg = str(reg).upper().replace(" ", "")

    if reg.startswith("EJC"):
        return reg.replace("EJC", "AE-JC", 1)
    if reg.startswith("EHC"):
        return reg.replace("EHC", "AE-HC", 1)
    if reg.startswith("EBE"):
        return reg.replace("EBE", "AE-BE", 1)
    if reg.startswith("OMD"):
        return reg.replace("OMD", "AO-MD", 1)

    return reg

def fix_type_key(v: dict) -> dict:
    reg = str(v.get("VehicleRegistrationNumber", "")).upper().replace(" ", "")

    if reg.startswith(("EJC", "EHC", "OMD", "EBE")):
        v["_type_key"] = "bus"

    return v

def chunk_embeds(title_base, entries, color=0x003200, max_fields=20):
    embeds = []
    embed = discord.Embed(title=title_base, color=color)
    field_count = 0

    for reg, info in sorted(entries.items(), key=lambda x: x[0]):
        lat = info.get('lat')
        lon = info.get('lon')
        dest = info.get('dest', 'Ismeretlen')

        lat_str = f"{lat:.5f}" if lat is not None else "Ismeretlen"
        lon_str = f"{lon:.5f}" if lon is not None else "Ismeretlen"

        value = f"Cél: {dest}\nPozíció: {lat_str}, {lon_str}"

        if field_count >= max_fields:
            embeds.append(embed)
            embed = discord.Embed(title=f"{title_base} (folytatás)", color=color)
            field_count = 0

        embed.add_field(name=reg, value=value, inline=False)
        field_count += 1

    embeds.append(embed)
    return embeds

async def fetch_mav_vehicles():
    """DKV járművek lekérése retry-val"""
    for attempt in range(3):  # 3 próbálkozás
        try:
            async with aiohttp.ClientSession(timeout=REQ_TIMEOUT) as session:
                async with session.get(VEHICLES_API) as resp:

                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("vehicles", [])

                    print(f"API hiba: {resp.status} (próba {attempt+1})")

        except Exception as e:
            print(f"Fetch hiba ({attempt+1}):", e)

        await asyncio.sleep(2)

    return []
        
        
# =======================
# Logger loop
# =======================

active_mav_vehicles = {}

active_mav_vehicles = {}

@tasks.loop(seconds=30)
async def logger_loop_mav():
    """Frissíti a MÁV járművek állapotát Magyarország teljes területére vonatkozóan."""
    try:
        vehicles = await fetch_mav_vehicles()  # a korábbi fetch függvény
    except Exception as e:
        print(f"Hiba a járművek lekérésekor: {e}")
        return

    now = datetime.now()
    active_mav_vehicles.clear()

    for vid, v in vehicles.items():
        lat = v.get("lat")
        lon = v.get("lon")
        dest = v.get("tripHeadsign") or "Ismeretlen"

        active_mav_vehicles[vid] = {
            "lat": lat,
            "lon": lon,
            "dest": dest,
            "vehicleModel": v.get("vehicleModel"),
            "uicCode": v.get("uicCode"),
            "tripShortName": v.get("tripShortName"),
            "mode": v.get("mode"),
            "nextStop": v.get("nextStop"),
            "last_seen": now
        }
        
# =======================
# PARANCSOK - Trolik
# =======================

@bot.command()
async def dkvtroli(ctx):
    """A DKV Troli buszai"""
    vehicles = await fetch_mav_vehicles()

    # Mercedes szűrés
    mercedes = [
        v for v in vehicles
        if v.get("_type_key") == "troli"
    ]

    if not mercedes:
        await ctx.send("Nincsenek trolibuszok.")
        return

    # Rendezés rendszám szerint
    mercedes.sort(key=lambda x: fix_plate(x.get("VehicleRegistrationNumber")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in mercedes:
        reg = fix_plate(v.get("VehicleRegistrationNumber"))
        tipus = v.get("full_type_label", "Ismeretlen")
        vonal = v.get("route_id", "—")
        cel = v.get("Destination", "Ismeretlen")
        megallo = v.get("StopAreaName", "Ismeretlen")

        delay_sec = v.get("delay_sec")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"{tipus}\n"
            f"Vonal: {vonal}\n"
            f"Cél: {cel}\n"
            f"Környék: {megallo}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embeds.append(discord.Embed(
                title="🚎 Trolibuszok",
                description=description,
                color=0x005B9F
            ))
            description = entry
        else:
            description += entry

    if description:
        embeds.append(discord.Embed(
            title="🚎 Trolibuszok",
            description=description,
            color=0x005B9F
        ))

    for e in embeds:
        await ctx.send(embed=e)

# =======================
# PARANCSOK - Buszok
# =======================

@bot.command()
async def dkvbusz(ctx):
    """A DKV Buszai"""
    vehicles = await fetch_mav_vehicles()

    # Mercedes szűrés
    vehicles = [fix_type_key(v) for v in vehicles]

    mercedes = [
        v for v in vehicles
        if v.get("_type_key") == "bus"
        and v.get("_type_label")
        and "MERCEDES" in v["_type_label"].upper() or (v.get("_type_label") and "Reform" in v["_type_label"].upper()) or (v.get("_type_label") and "ITK" in v["_type_label"].upper())
    ]

    if not mercedes:
        await ctx.send("Nincsenek buszok.")
        return

    # Rendezés rendszám szerint
    mercedes.sort(key=lambda x: fix_plate(x.get("VehicleRegistrationNumber")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in mercedes:
        reg = fix_plate(v.get("VehicleRegistrationNumber"))
        tipus = v.get("full_type_label", "Ismeretlen")
        vonal = v.get("route_id", "—")
        cel = v.get("Destination", "Ismeretlen")
        megallo = v.get("StopAreaName", "Ismeretlen")

        delay_sec = v.get("delay_sec")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"{tipus}\n"
            f"Vonal: {vonal}\n"
            f"Cél: {cel}\n"
            f"Környék: {megallo}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embeds.append(discord.Embed(
                title="🚍 Buszok",
                description=description,
                color=0x005B9F
            ))
            description = entry
        else:
            description += entry

    if description:
        embeds.append(discord.Embed(
            title="🚍 Buszok",
            description=description,
            color=0x005B9F
        ))

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def dkvmercedes(ctx):
    """A DKV Mercedes buszai"""

    vehicles = await fetch_mav_vehicles()

    # ───── 1. ADAT TISZTÍTÁS ─────
    def fix_vehicle(v: dict) -> dict:
        reg = str(v.get("VehicleRegistrationNumber", "")).upper().replace(" ", "")

        if reg.startswith(("EJC", "EHC", "OMD", "EBE")):
            v["_type_key"] = "bus"

        v["VehicleRegistrationNumber"] = reg
        return v

    def fix_plate(reg: str) -> str:
        if not reg:
            return "Ismeretlen"

        reg = str(reg).upper().replace(" ", "")

        if reg.startswith("EJC"):
            return reg.replace("EJC", "AE-JC", 1)
        if reg.startswith("EHC"):
            return reg.replace("EHC", "AE-HC", 1)
        if reg.startswith("EBE"):
            return reg.replace("EBE", "AE-BE", 1)
        if reg.startswith("OMD"):
            return reg.replace("OMD", "AO-MD", 1)

        return reg

    # ───── 2. FIX ALKALMAZÁS ─────
    vehicles = [fix_vehicle(v) for v in vehicles]

    # ───── 3. MERCEDES SZŰRÉS ─────
    mercedes = [
        v for v in vehicles
        if v.get("_type_label")
        and "MERCEDES" in v["_type_label"].upper()
    ]

    if not mercedes:
        await ctx.send("Nincsenek Mercedes buszok.")
        return

    # ───── 4. RENDEZÉS ─────
    mercedes.sort(
        key=lambda x: fix_plate(x.get("VehicleRegistrationNumber"))
    )

    # ───── 5. EMBED ÖSSZERAKÁS ─────
    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in mercedes:
        reg = fix_plate(v.get("VehicleRegistrationNumber"))
        tipus = v.get("full_type_label", "Ismeretlen")
        vonal = v.get("route_id", "—")
        cel = v.get("Destination", "Ismeretlen")
        megallo = v.get("StopAreaName", "Ismeretlen")

        delay_sec = v.get("delay_sec")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"{tipus}\n"
            f"Vonal: {vonal}\n"
            f"Cél: {cel}\n"
            f"Környék: {megallo}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embeds.append(discord.Embed(
                title="🚍 Mercedes buszok",
                description=description,
                color=0x005B9F
            ))
            description = entry
        else:
            description += entry

    if description:
        embeds.append(discord.Embed(
            title="🚍 Mercedes buszok",
            description=description,
            color=0x005B9F
        ))

    # ───── 6. KÜLDÉS ─────
    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def dkvreform(ctx):
    """A DKV ITK reform buszai"""
    vehicles = await fetch_mav_vehicles()

    # Mercedes szűrés
    mercedes = [
        v for v in vehicles
        if (v.get("_type_label") and "Reform" in v["_type_label"].upper()) or (v.get("_type_label") and "ITK" in v["_type_label"].upper())
    ]

    if not mercedes:
        await ctx.send("Nincsenek Reform buszok.")
        return

    # Rendezés rendszám szerint
    mercedes.sort(key=lambda x: fix_plate(x.get("VehicleRegistrationNumber")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in mercedes:
        reg = fix_plate(v.get("VehicleRegistrationNumber"))
        tipus = v.get("full_type_label", "Ismeretlen")
        vonal = v.get("route_id", "—")
        cel = v.get("Destination", "Ismeretlen")
        megallo = v.get("StopAreaName", "Ismeretlen")

        delay_sec = v.get("delay_sec")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"{tipus}\n"
            f"Vonal: {vonal}\n"
            f"Cél: {cel}\n"
            f"Környék: {megallo}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embeds.append(discord.Embed(
                title="🚍 Reform buszok",
                description=description,
                color=0x005B9F
            ))
            description = entry
        else:
            description += entry

    if description:
        embeds.append(discord.Embed(
            title="🚍 Reform buszok",
            description=description,
            color=0x005B9F
        ))

    for e in embeds:
        await ctx.send(embed=e)
        
# =======================
# PARANCSOK - Villamosok
# =======================

@bot.command()
async def dkvvillamos(ctx):
    """A DKV villamosai"""
    vehicles = await fetch_mav_vehicles()

    # Mercedes szűrés
    mercedes = [
        v for v in vehicles
        if v.get("_type_key") == "tram"
    ]

    if not mercedes:
        await ctx.send("Nincsenek villamosok.")
        return

    # Rendezés rendszám szerint
    mercedes.sort(key=lambda x: fix_plate(x.get("VehicleRegistrationNumber")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in mercedes:
        reg = fix_plate(v.get("VehicleRegistrationNumber"))
        tipus = v.get("full_type_label", "Ismeretlen")
        vonal = v.get("route_id", "—")
        cel = v.get("Destination", "Ismeretlen")
        megallo = v.get("StopAreaName", "Ismeretlen")

        delay_sec = v.get("delay_sec")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"{tipus}\n"
            f"Vonal: {vonal}\n"
            f"Cél: {cel}\n"
            f"Környék: {megallo}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embeds.append(discord.Embed(
                title="🚍 Villamosok",
                description=description,
                color=0xE8AD15
            ))
            description = entry
        else:
            description += entry

    if description:
        embeds.append(discord.Embed(
            title="🚍 Villamosok",
            description=description,
            color=0xE8AD15
        ))

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def dkvkcsv(ctx):
    """A DKV KCSV6 villamosai"""
    vehicles = await fetch_mav_vehicles()

    # Mercedes szűrés
    mercedes = [
        v for v in vehicles
        if v.get("_type_label") and "KCSV" in v["_type_label"].upper()
    ]

    if not mercedes:
        await ctx.send("Nincsenek KCSV6 villamosok.")
        return

    # Rendezés rendszám szerint
    mercedes.sort(key=lambda x: fix_plate(x.get("VehicleRegistrationNumber")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in mercedes:
        reg = fix_plate(v.get("VehicleRegistrationNumber"))
        tipus = v.get("full_type_label", "Ismeretlen")
        vonal = v.get("route_id", "—")
        cel = v.get("Destination", "Ismeretlen")
        megallo = v.get("StopAreaName", "Ismeretlen")

        delay_sec = v.get("delay_sec")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"{tipus}\n"
            f"Vonal: {vonal}\n"
            f"Cél: {cel}\n"
            f"Környék: {megallo}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embeds.append(discord.Embed(
                title="🚊 Ganz KCSV6-1 villamosok",
                description=description,
                color=0xE8AD15
            ))
            description = entry
        else:
            description += entry

    if description:
        embeds.append(discord.Embed(
            title="🚊 Ganz KCSV6-1 villamosok",
            description=description,
            color=0xE8AD15
        ))

    for e in embeds:
        await ctx.send(embed=e)
        
@bot.command()
async def dkvcaf(ctx):
    """A DKV CAF villamosai"""
    vehicles = await fetch_mav_vehicles()

    # Mercedes szűrés
    mercedes = [
        v for v in vehicles
        if v.get("_type_label") and "CAF" in v["_type_label"].upper()
    ]

    if not mercedes:
        await ctx.send("Nincsenek CAF villamosok.")
        return

    # Rendezés rendszám szerint
    mercedes.sort(key=lambda x: fix_plate(x.get("VehicleRegistrationNumber")))

    MAX_CHARS = 4000
    description = ""
    embeds = []

    for v in mercedes:
        reg = fix_plate(v.get("VehicleRegistrationNumber"))
        tipus = v.get("full_type_label", "Ismeretlen")
        vonal = v.get("route_id", "—")
        cel = v.get("Destination", "Ismeretlen")
        megallo = v.get("StopAreaName", "Ismeretlen")

        delay_sec = v.get("delay_sec")
        delay_min = f"{int(delay_sec / 60)} perc" if delay_sec is not None else "—"

        entry = (
            f"**{reg}**\n"
            f"{tipus}\n"
            f"Vonal: {vonal}\n"
            f"Cél: {cel}\n"
            f"Környék: {megallo}\n"
            f"Késés: {delay_min}\n\n"
        )

        if len(description) + len(entry) > MAX_CHARS:
            embeds.append(discord.Embed(
                title="🚊 CAF Urbos 3 villamosok",
                description=description,
                color=0xE8AD15
            ))
            description = entry
        else:
            description += entry

    if description:
        embeds.append(discord.Embed(
            title="🚊 CAF Urbos 3 villamosok",
            description=description,
            color=0xE8AD15
        ))

    for e in embeds:
        await ctx.send(embed=e)

# =======================
# PARANCSOK - Egyébbek
# =======================

# @bot.command()
# async def vehhist(ctx, vehicle: str, date: str = None):
#     vehicle = vehicle.upper()  # 🔥 EZ A LÉNYEG

#     day = resolve_date(date)
#     if day is None:
#         return await ctx.send("❌ Hibás dátumformátum. Használd így: `YYYY-MM-DD`")

#     day_str = day.strftime("%Y-%m-%d")
#     veh_file = f"logs/veh/{vehicle}.txt"

#     if not os.path.exists(veh_file):
#         return await ctx.send("❌ Nincs ilyen jármű a naplóban.")

#     entries = []
#     with open(veh_file, "r", encoding="utf-8") as f:
#         for l in f:
#             if not l.startswith(day_str):
#                 continue
#             try:
#                 ts, rest = l.strip().split(" - ", 1)
#                 dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
#                 trip_id = rest.split("ID ")[1].split(" ")[0]
#                 line = rest.split("Vonal ")[1].split(" ")[0]
#                 dest = rest.split(" - ")[-1]
#                 entries.append((dt, line, trip_id, dest))
#             except Exception:
#                 continue

#     if not entries:
#         return await ctx.send(f"❌ {vehicle} nem közlekedett ezen a napon ({day_str}).")

#     entries.sort(key=lambda x: x[0])

#     runs = []
#     current = None

#     for dt, line, trip_id, dest in entries:
#         if not current or trip_id != current["trip_id"] or line != current["line"]:
#             if current:
#                 runs.append(current)
#             current = {
#                 "line": line,
#                 "trip_id": trip_id,
#                 "start": dt,
#                 "end": dt,
#                 "dest": dest
#             }
#         else:
#             current["end"] = dt

#     if current:
#         runs.append(current)

#     lines = [f"🚎 {vehicle} – vehhist ({day_str})"]
#     for r in runs:
#         lines.append(f"{r['start'].strftime('%H:%M')} – {r['line']} / {r['trip_id']} – {r['dest']}")

#     msg = "\n".join(lines)
#     for i in range(0, len(msg), 1900):
#         await ctx.send(msg[i:i + 1900])
        
# @bot.command()
# async def vehicleinfo(ctx, vehicle: str):
#     path = f"logs/veh/{vehicle}.txt"
#     if not os.path.exists(path):
#         return await ctx.send(f"❌ Nincs adat a(z) {vehicle} járműről.")

#     with open(path, "r", encoding="utf-8") as f:
#         lines = [l.strip() for l in f if l.strip()]

#     if not lines:
#         return await ctx.send(f"❌ Nincs adat a(z) {vehicle} járműről.")

#     last = lines[-1]
#     await ctx.send(f"🚊 **{vehicle} utolsó menete**\n```{last}```")
        
@bot.command()
async def all(ctx, line: str):
    """Kiírja az adott vonalon közlekedő összes járművet"""

    TRAM_LINES = {"1","2"}
    TROLLEY_LINES = {"3", "3A", "4", "5", "5A"}
    NIGHT_LINES = {"90Y","91A","91Y","92","93","94","971","972","973","974"}

    BUS_LINES = {
        "10","10Y","11","12","13","14","14Y","15","15G","15H","15Y","15YH",
        "16","17","17A","18","18Y","19","21","22","22Y","23","23Y","24","24Y",
        "25","25Y","30","30A","30H","33","33E","34","34A","35","35A","35E","35Y",
        "35YA","36","36A","36R","37","37A","39","41","41Y","42","43","44","45",
        "46","46H","46Y","47","47Y","48","49","51E","52E","60","60H","61","70",
        "71","71A","72","72A","73","73A","125","125Y","146","J1"
    }

    AP_LINES = {"AIRPORT1"}
    STORE_LINE = {"AUCHAN1"}

    def normalize(x):
        return "".join(str(x or "").upper().split())

    line = normalize(line)

    # ───── ALAP (vonal szerinti) ─────
    if line in TRAM_LINES:
        color = 0xe8ad15
        title_prefix = "🚊 Aktív járművek – Villamos"
        base_type = "tram"
    elif line in TROLLEY_LINES:
        color = 0x6ab53f
        title_prefix = "🚎 Aktív járművek – Trolibusz"
        base_type = "trolley"
    elif line in NIGHT_LINES:
        color = 0x2596be
        title_prefix = "🌙 Aktív járművek – Éjszakai"
        base_type = "night"
    elif line in AP_LINES:
        color = 0x13d9e2
        title_prefix = "✈️ Aktív járművek – Airport"
        base_type = "bus"
    elif line in STORE_LINE:
        color = 0xdb3d37
        title_prefix = "🛒 Aktív járművek – Store"
        base_type = "bus"
    elif line in BUS_LINES:
        color = 0x005b9f
        title_prefix = "🚍 Aktív járművek – Busz"
        base_type = "bus"

    vehicles = await fetch_mav_vehicles()
    vehicles = [fix_type_key(v) for v in vehicles]

    active = []
    detected_types = set()

    for v in vehicles:
        line_code = normalize(v.get("lineCode") or v.get("route_id"))

        if line_code != line:
            continue

        lat = v.get("Latitude")
        lon = v.get("Longitude")

        if lat is None or lon is None:
            continue

        t = v.get("_type_key")

        if t == "tram":
            detected_types.add("tram")
        elif t == "troli":
            detected_types.add("trolley")
        elif t == "bus":
            detected_types.add("bus")

        active.append({
            "reg": fix_plate(v.get("VehicleRegistrationNumber")),
            "dest": v.get("Destination", "Ismeretlen"),
            "lat": lat,
            "lon": lon,
            "delay": v.get("delay_sec") or v.get("Delay"),
            "type": v.get("_type_label", "Ismeretlen")
        })

    if not active:
        return await ctx.send(f"❗ Nincs aktív jármű a **{line}** vonalon.")

    # ───── FELÜLÍRÁS HA ELTÉR ─────
    if len(detected_types) == 1:
        real_type = list(detected_types)[0]

        if real_type != base_type:
            if real_type == "tram":
                color = 0xe8ad15
                title_prefix = "🚊 Aktív járművek – Villamos"
            elif real_type == "trolley":
                color = 0x6ab53f
                title_prefix = "🚎 Aktív járművek – Trolibusz"
            elif real_type == "bus":
                color = 0x005b9f
                title_prefix = "🚍 Aktív járművek – Busz"

    elif len(detected_types) > 1:
        color = 0x666666
        title_prefix = "🚦 Vegyes járművek"

    # ───── EMBED ─────
    embed = discord.Embed(
        title=f"{title_prefix} {line}",
        color=color
    )

    for v in active:
        delay = v["delay"]
        if delay is not None:
            delay = f"{int(delay / 60)} perc"
        else:
            delay = "—"

        embed.add_field(
            name=v["reg"],
            value=(
                f"Típus: {v['type']}\n"
                f"Cél: {v['dest']}\n"
                f"Késés: {delay}\n"
                f"Pozíció: {v['lat']:.5f}, {v['lon']:.5f}\n\n"
            ),
            inline=False
        )

    await ctx.send(embed=embed)

# =======================
# START
# =======================

@bot.event
async def on_ready():
    print(f"Bejelentkezve mint {bot.user}")

try:
    bot.run(TOKEN)
finally:
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass
