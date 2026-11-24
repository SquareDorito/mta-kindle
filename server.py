"""
MTA Departure Board — TRMNL BYOS Server
Serves real-time NYC subway departures for stations near
37-25 32nd St, Long Island City as a Kindle-optimized portrait e-ink image.
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import datetime
import hashlib
import io
import logging
import os
import time

from flask import Flask, jsonify, make_response, request, send_file
from nyct_gtfs import NYCTFeed
from PIL import Image, ImageDraw, ImageFont
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WIDTH, HEIGHT = 1072, 1448  # Native portrait resolution for Kindle (300 PPI)
PORT = int(os.environ.get("PORT", 8080))

# Nearby stations: (stop_id_base, display_name, walk_minutes, fallback_lines)
STATIONS = [
    ("R08", "39 Av–Dutch Kills", 3, "N/W"),
    ("G20", "36 St", 3, "M/R"),
    ("G21", "Queens Plaza", 9, "E/F/M/R"),
]

# MTA GTFS-RT feed groups to query concurrently:
#   "N" -> N, Q, R, W   |   "E" -> A, C, E   |   "M" -> B, D, F, M
FEED_KEYS = ("N", "E", "M")

# Direction labels
DIRECTION_LABELS = {
    "R08S": "Manhattan",
    "R08N": "Astoria",
    "G20S": "Manhattan",
    "G20N": "Jamaica / Forest Hills",
    "G21S": "Manhattan",
    "G21N": "Jamaica / Forest Hills",
}

TRAINS_PER_DIRECTION = 2
REFRESH_SECONDS = 30
CACHE_TTL = 20  # seconds

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Arrival:
    station_id: str
    station_name: str
    route: str
    direction: str
    minutes_away: float
    arrival_time: datetime.datetime


def _fetch_single_feed(feed_key: str) -> tuple[str, NYCTFeed | None]:
    try:
        return feed_key, NYCTFeed(feed_key)
    except Exception as e:
        logging.warning(f"Failed to fetch feed {feed_key}: {e}")
        return feed_key, None


def fetch_arrivals() -> list[Arrival]:
    """Fetch real-time arrivals concurrently for all target stations."""
    now = datetime.datetime.now()
    arrivals: list[Arrival] = []
    seen = set()

    station_names = {s[0]: s[1] for s in STATIONS}
    all_target_ids = set(station_names.keys())

    feeds: list[NYCTFeed] = []
    with ThreadPoolExecutor(max_workers=len(FEED_KEYS)) as pool:
        futures = [pool.submit(_fetch_single_feed, fk) for fk in FEED_KEYS]
        for fut in as_completed(futures):
            _, feed = fut.result()
            if feed is not None:
                feeds.append(feed)

    if not feeds:
        raise RuntimeError("All MTA feeds failed")

    for feed in feeds:
        for trip in feed.trips:
            for stu in trip.stop_time_updates:
                if stu.arrival is None:
                    continue
                stop_id = stu.stop_id
                base_id = stop_id[:-1] if stop_id[-1] in ("N", "S") else stop_id
                if base_id not in all_target_ids:
                    continue

                mins = (stu.arrival - now).total_seconds() / 60
                if mins < -1 or mins > 90:
                    continue

                dedup_key = (trip.route_id, stop_id, stu.arrival)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                direction = DIRECTION_LABELS.get(stop_id, stop_id[-1])
                arrivals.append(Arrival(
                    station_id=base_id,
                    station_name=station_names.get(base_id, stu.stop_name),
                    route=trip.route_id,
                    direction=direction,
                    minutes_away=round(max(mins, 0), 1),
                    arrival_time=stu.arrival,
                ))

    arrivals.sort(key=lambda a: a.minutes_away)
    return arrivals


# ---------------------------------------------------------------------------
# Image rendering (native grayscale for Kindle e-ink)
# ---------------------------------------------------------------------------

def _try_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Helvetica (Regular or Bold) on macOS, or DejaVu on Linux."""
    if bold:
        candidates = [
            ("/System/Library/Fonts/Helvetica.ttc", 1),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
        ]
    else:
        candidates = [
            ("/System/Library/Fonts/Helvetica.ttc", 0),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
        ]
    for path, idx in candidates:
        try:
            return ImageFont.truetype(path, size, index=idx)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _format_clock(dt: datetime.datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def render_board(arrivals: list[Arrival]) -> Image.Image:
    """Render the departure board as a 1072×1448 portrait grayscale image."""
    img = Image.new("L", (WIDTH, HEIGHT), color=255)
    draw = ImageDraw.Draw(img)

    font_title = _try_font(50, bold=True)
    font_clock_hdr = _try_font(36, bold=True)
    font_station = _try_font(36, bold=True)
    font_dir = _try_font(28, bold=True)
    font_mins = _try_font(40, bold=True)
    font_row = _try_font(36, bold=True)
    font_leave = _try_font(30, bold=True)
    font_bullet = _try_font(28, bold=True)
    font_small = _try_font(28)

    # --- Header ---
    now = datetime.datetime.now()
    draw.rectangle([0, 0, WIDTH, 90], fill=0)
    draw.text((28, 18), "MTA Departures", fill=255, font=font_title)
    time_str = _format_clock(now)
    bbox = draw.textbbox((0, 0), time_str, font=font_clock_hdr)
    draw.text((WIDTH - (bbox[2] - bbox[0]) - 28, 25), time_str, fill=255, font=font_clock_hdr)

    # Subtitle (high contrast)
    draw.text((28, 102), "37-25 32nd St, Long Island City  •  Catchable trains only", fill=25, font=font_small)

    if not arrivals:
        draw.text((WIDTH // 2 - 170, HEIGHT // 2 - 30),
                  "No trains available", fill=0, font=font_station)
        return img

    # --- Group arrivals by station & direction ---
    by_station: dict[str, dict[str, list[Arrival]]] = defaultdict(lambda: defaultdict(list))
    for a in arrivals:
        by_station[a.station_id][a.direction].append(a)

    y = 146
    bullet_r = 25

    for station_id, station_name, walk_min, fallback_lines in STATIONS:
        if y > HEIGHT - 100:
            break

        directions = by_station.get(station_id, {})

        # Active routes currently serving this station
        active_routes = sorted({a.route for d in directions.values() for a in d})
        dynamic_lines = "/".join(active_routes) if active_routes else fallback_lines

        # --- Station header bar ---
        draw.rectangle([0, y, WIDTH, y + 54], fill=0)
        header_text = f"{station_name}  ({walk_min} min walk)"
        draw.text((25, y + 7), header_text, fill=255, font=font_station)
        lb = draw.textbbox((0, 0), dynamic_lines, font=font_dir)
        draw.text((WIDTH - (lb[2] - lb[0]) - 25, y + 12), dynamic_lines, fill=255, font=font_dir)
        y += 66

        # Always show both configured directions for this station (Manhattan first)
        expected_dirs = [
            DIRECTION_LABELS[f"{station_id}S"],
            DIRECTION_LABELS[f"{station_id}N"],
        ]

        for direction in expected_dirs:
            if y > HEIGHT - 70:
                break

            all_dir_trains = sorted(directions.get(direction, []), key=lambda a: a.minutes_away)
            catchable = [a for a in all_dir_trains if a.minutes_away >= walk_min][:TRAINS_PER_DIRECTION]

            # Direction subheader (crisp dark text on light tint with thin bottom rule)
            draw.rectangle([25, y, WIDTH - 25, y + 42], fill=235)
            draw.line([(25, y + 42), (WIDTH - 25, y + 42)], fill=160, width=2)
            draw.text((38, y + 5), f"To {direction}", fill=0, font=font_dir)
            y += 50

            if not catchable:
                draw.text((60, y + 6), "No catchable trains", fill=40, font=font_small)
                y += 54
                continue

            for a in catchable:
                if y > HEIGHT - 54:
                    break

                # Route bullet
                bx, by_c = 58 + bullet_r, y + 26
                draw.ellipse([bx - bullet_r, by_c - bullet_r,
                              bx + bullet_r, by_c + bullet_r], fill=0)
                rb = draw.textbbox((0, 0), a.route, font=font_bullet)
                draw.text(
                    (bx - (rb[2] - rb[0]) // 2, by_c - (rb[3] - rb[1]) // 2 - 2),
                    a.route,
                    fill=255,
                    font=font_bullet,
                )

                # Train label
                draw.text((126, y + 6), f"{a.route} train", fill=0, font=font_row)

                # Arrival countdown
                mins_int = int(round(a.minutes_away))
                draw.text((340, y + 4), f"{mins_int} min", fill=0, font=font_mins)

                # Leave-in badge (consistent with displayed mins_int, high-contrast)
                leave_in = mins_int - walk_min
                if leave_in <= 1:
                    badge_text = "Leave NOW"
                    bb = draw.textbbox((0, 0), badge_text, font=font_leave)
                    bw = bb[2] - bb[0]
                    draw.rounded_rectangle([545, y + 4, 545 + bw + 28, y + 48], radius=8, fill=0)
                    draw.text((559, y + 8), badge_text, fill=255, font=font_leave)
                else:
                    draw.text((555, y + 8), f"Leave in {leave_in}m", fill=0, font=font_leave)

                # Exact clock time (right-aligned, solid black)
                clock = _format_clock(a.arrival_time)
                cb = draw.textbbox((0, 0), clock, font=font_small)
                draw.text((WIDTH - (cb[2] - cb[0]) - 28, y + 10), clock, fill=20, font=font_small)

                y += 58

            y += 8

        y += 14

    # --- Footer ---
    draw.line([(0, HEIGHT - 50), (WIDTH, HEIGHT - 50)], fill=120, width=2)
    footer = f"Updated {now.strftime('%I:%M:%S %p').lstrip('0')}  •  Refresh every {REFRESH_SECONDS}s"
    draw.text((28, HEIGHT - 40), footer, fill=20, font=font_small)

    return img


def render_board_png_bytes() -> tuple[bytes, str]:
    """Fetch data, render grayscale e-ink PNG, return (png_bytes, hash_filename)."""
    arrivals = fetch_arrivals()
    img = render_board(arrivals)

    buf = io.BytesIO()
    # Clean 16-level grayscale palette (sharp on Kindle e-ink, tiny PNG size)
    img_q = img.quantize(colors=16, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    img_q.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    h = hashlib.md5(png_bytes).hexdigest()[:12]
    filename = f"mta-{h}.png"
    return png_bytes, filename


# ---------------------------------------------------------------------------
# Flask App / BYOS Server
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
logging.basicConfig(level=logging.INFO)

_cache: dict = {"bytes": None, "filename": None, "ts": 0.0}


def _get_or_render() -> tuple[bytes, str]:
    now = time.time()
    if _cache["bytes"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["bytes"], _cache["filename"]
    try:
        png_bytes, filename = render_board_png_bytes()
        _cache["bytes"] = png_bytes
        _cache["filename"] = filename
        _cache["ts"] = now
        return png_bytes, filename
    except Exception as e:
        logging.exception("Render failed")
        if _cache["bytes"]:
            logging.warning("Serving last good cached image after transient error")
            return _cache["bytes"], _cache["filename"]
        img = Image.new("L", (WIDTH, HEIGHT), color=255)
        draw = ImageDraw.Draw(img)
        draw.text((40, 200), f"MTA Feed Error: {e}", fill=0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), "error.png"


# --- TRMNL BYOS Endpoints ---

@app.route("/api/setup", methods=["GET"])
def api_setup():
    """Device registration/pairing."""
    mac = request.headers.get("ID", "unknown")
    logging.info(f"Device setup: MAC={mac}")
    return jsonify({
        "status": 0,
        "api_key": "mta-kindle-local",
        "friendly_id": "MTA Board",
        "image_url": "",
        "message": "Welcome! Your MTA departure board is ready.",
    })


@app.route("/api/display", methods=["GET"])
@app.route("/api/display/", methods=["GET"])
def api_display():
    """Core endpoint: returns the next screen for the TRMNL/KOReader device."""
    mac = request.headers.get("ID", "unknown")
    fw = request.headers.get("FW-Version", "?")
    battery = request.headers.get("Battery-Voltage", "?")
    logging.info(f"Display request: MAC={mac} FW={fw} Battery={battery}")

    _, filename = _get_or_render()
    image_url = request.host_url.rstrip("/") + "/image/" + filename

    resp = make_response(jsonify({
        "status": 0,
        "image_url": image_url,
        "filename": filename,
        "refresh_rate": REFRESH_SECONDS,
        "reset_firmware": False,
        "update_firmware": False,
        "firmware_url": "",
        "special_function": "",
    }))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/log", methods=["POST"])
def api_log():
    """Receive device telemetry/logs."""
    data = request.get_json(silent=True) or {}
    logging.info(f"Device log: {data}")
    return jsonify({"status": 0})


@app.route("/image/<filename>")
def serve_image(filename):
    """Serve the rendered departure board image."""
    png_bytes, _ = _get_or_render()
    resp = make_response(send_file(
        io.BytesIO(png_bytes),
        mimetype="image/png",
        download_name=filename,
    ))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# --- Human-friendly preview ---

@app.route("/")
def index():
    """Browser-friendly preview page."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>MTA Departure Board</title>
    <meta http-equiv="refresh" content="{REFRESH_SECONDS}">
    <style>
        body {{ background: #222; display: flex; flex-direction: column;
               align-items: center; justify-content: center; min-height: 100vh;
               margin: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; color: #fff; }}
        img {{ border: 10px solid #111; border-radius: 8px; box-shadow: 0 8px 32px rgba(0,0,0,0.5); max-height: 85vh; width: auto; }}
        .info {{ margin-top: 16px; font-size: 14px; color: #aaa; }}
        a {{ color: #6cf; }}
    </style>
</head>
<body>
    <h2>MTA Departure Board — Kindle/TRMNL Preview</h2>
    <img src="/image/preview.png" width="{WIDTH}" height="{HEIGHT}" alt="Departure Board">
    <div class="info">
        Auto-refreshes every {REFRESH_SECONDS}s &bull;
        <a href="/api/display">BYOS API</a> &bull;
        <a href="/image/preview.png">Raw image</a>
    </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  MTA Departure Board — TRMNL BYOS Server")
    print(f"  Serving on http://0.0.0.0:{PORT}")
    print(f"  Preview:  http://localhost:{PORT}")
    print(f"  BYOS API: http://localhost:{PORT}/api/display")
    print("=" * 60)
    app.run(host="0.0.0.0", port=PORT, debug=False)
