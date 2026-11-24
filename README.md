# MTA Departure Board — TRMNL BYOS Server for Kindle

Real-time NYC subway departure times for stations near **37-25 32nd St, Long Island City 11101**, served as a portrait e-ink image via the TRMNL BYOS protocol.

## Nearby Stations

| Station | Typical Lines | Walk | GTFS ID |
|---------|---------------|------|---------|
| 39 Av–Dutch Kills | N, W | ~3 min | R08 |
| 36 St | M, R (E, F late nights) | ~3 min | G20 |
| Queens Plaza | E, F, M, R | ~9 min | G21 |

- Each station header dynamically displays the **active routes** currently running in the live GTFS feed.
- Shows the next **2 catchable trains** per direction (`minutes_away >= walk_min`) along with a **"Leave in Xm" / "Leave NOW"** indicator.

## Quick Start

The server is hosted at **https://mta.kennoh.xyz** (running on GCP `kennoh-vm` via `mta-kindle.service` + Caddy). Open https://mta.kennoh.xyz in a browser to preview.

To run locally:

```bash
cd ~/data/mta-kindle
source venv/bin/activate
python server.py
```

Override the listening port with `PORT=8080 python server.py`.

## TRMNL / KOReader Setup

1. Install [trmnl-koreader](https://github.com/usetrmnl/trmnl-koreader) on your jailbroken Kindle.
2. In KOReader, go to **Tools → TRMNL Display → Configure TRMNL**:
   - **Base URL**: `https://mta.kennoh.xyz`
   - **API Key**: `mta-kindle-local` (or any string)
   - **Use server refresh interval**: Enabled (`30s`)
3. Start the dashboard: **Tools → TRMNL Display → Start dashboard**.

> Note: A 30-second refresh over Wi-Fi will still drain the Kindle battery quickly unless it is plugged in. Increase `REFRESH_SECONDS` in `server.py` if running on battery.

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/setup` | GET | Device registration |
| `/api/display` | GET | Returns next screen payload (JSON) |
| `/api/log` | POST | Receives device telemetry |
| `/image/<filename>` | GET | Serves the rendered portrait PNG (`600×800`, 16-level grayscale) |
| `/` | GET | Auto-refreshing browser preview |
