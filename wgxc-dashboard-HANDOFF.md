# WGXC Tower Fallback Dashboard — Agent Handoff
**Last updated:** 2026-04-10 (Session 3 - Host Migration)  
**Host:** `tower-two` (wgxc-tower-fallback) — Late 2012 Mac Mini, Debian 13, 4GB RAM  
**WireGuard IP:** `10.50.0.25`  
**LAN IP:** `192.168.1.113`  
**Dashboard URL:** `http://10.50.0.25:8080`

---

## Purpose

Emergency backup broadcast control panel for WGXC 90.7 FM staff. Used when the main scheduled stream goes off the air. Staff can switch audio sources, browse and play local audio files, and monitor loudness — all while the Mac Mini sends audio directly to the FM broadcast transmitter via its headphone/USB output.

---

## Architecture

```
Browser (staff)
  http://10.50.0.25:8080
        │
        ▼
Docker: tower-dashboard  (Flask/gunicorn, port 8080→5000)
  app/app.py             Flask API, proxies to Liquidsoap telnet
  app/templates/index.html  single-page UI
        │ telnet :1234 (via host.docker.internal)
        ▼
Host Systemd: liquidsoap-tower.service  (Liquidsoap v2.3.2)
  Sources: automation.mp3, live.mp3, request.queue + playlist fallback
  Outputs: 
    1. ALSA hw:0,0 (Direct to headphone jack → Transmitter)
    2. Harbor :8001/monitor.mp3 (Browser monitor player)
        │
Prometheus: 10.50.0.7:9090  (stream loudness LUFS, queried by /api/streams)
```

**Critical:** Liquidsoap runs on the **HOST** (not Docker) to eliminate 2-5s audio latency and fix telnet timeouts. Audio is sent directly to the transmitter via `output.alsa`.

---

## File Tree

```
~/cloud/wavefarm/               ← LOCAL SOURCE OF TRUTH (Mac, 10.50.0.5)
├── app/
│   ├── app.py                  ← Flask backend
│   └── templates/index.html    ← single-page UI (CSS + JS inline)
├── docker-compose.yml          ← Dashboard only
├── Dockerfile.dashboard
└── liquidsoap/
    └── wgxc-tower.liq          ← Liquidsoap 2.3.x script

tower-two:/home/tower-two/wgxc-dashboard/   ← DEPLOYED COPY
├── docker-compose.yml
├── Dockerfile.dashboard
├── .env                        ← WF_PASSWORD=<dashboard password> (see .env.example)
├── app/                        ← baked into Docker image at build time
│   ├── app.py
│   └── templates/index.html
├── liquidsoap/
│   └── wgxc-tower.liq          ← Symlinked/Used by systemd service
└── music/
    └── emergency/              ← drop MP3s here; playlist loops these
```

---

## Deploy Workflow

### Dashboard (app.py or index.html changed)
```bash
scp app/app.py root@10.50.0.25:/home/tower-two/wgxc-dashboard/app/app.py
scp app/templates/index.html root@10.50.0.25:/home/tower-two/wgxc-dashboard/app/templates/index.html
ssh root@10.50.0.25 "cd /home/tower-two/wgxc-dashboard && docker compose up --build -d"
```

### Liquidsoap script changed (wgxc-tower.liq)
```bash
scp liquidsoap/wgxc-tower.liq root@10.50.0.25:/home/tower-two/wgxc-dashboard/liquidsoap/wgxc-tower.liq
ssh root@10.50.0.25 "systemctl restart liquidsoap-tower"
```

### Full redeploy (everything)
```bash
# Sync files
scp app/app.py app/templates/index.html root@10.50.0.25:/home/tower-two/wgxc-dashboard/app/
scp app/templates/index.html root@10.50.0.25:/home/tower-two/wgxc-dashboard/app/templates/index.html
scp liquidsoap/wgxc-tower.liq root@10.50.0.25:/home/tower-two/wgxc-dashboard/liquidsoap/wgxc-tower.liq
# Rebuild dashboard & restart engine
ssh root@10.50.0.25 "cd /home/tower-two/wgxc-dashboard && docker compose up --build -d && systemctl restart liquidsoap-tower"
```

---

## Flask API Reference (`app/app.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves `index.html` |
| GET | `/api/status` | Returns `{selected, volume, gains, host}` (batched telnet) |
| POST | `/api/switch` | `{source: "automation"|"live"|"emergency"}` — switches source |
| POST | `/api/volume` | `{level: 0.0–1.0}` — sets Liquidsoap master volume |
| POST | `/api/gain` | `{source: "automation"|"live"|"emergency", level: float}` — per-source gain |
| GET | `/api/music/files` | JSON list of MP3s in `/music/emergency` |
| POST | `/api/music/play` | `{filename: "..."}` — push to queue then skip current (instant play) |
| POST | `/api/music/skip` | Skip current emergency track, advance to next |
| POST | `/api/music/stop` | Switch to automation + skip queue |
| GET | `/api/wavefarm/streams` | Prometheus mounts query + current LUFS |

---

## Liquidsoap Script (`liquidsoap/wgxc-tower.liq`)

**Version:** 2.3.2 (Host)  
**Outputs:**
- `alsa_out`: Hardware device `hw:0,0`.
- `harbor`: Port `8001`, mount `monitor.mp3`.

**Routing:** Uses `switch` with `track_sensitive=false` for immediate transitions.  
**Live stream:** `poll_delay=30.` — avoids 404 log spam every 2s when no live show is broadcasting.

**Telnet Commands:**
- `radio.selected` / `radio.select <name>`
- `audio.get_volume` / `audio.volume <float>`
- `audio.get_gain <src>` / `audio.gain <src> <float>`
- `emergency_queue.skip`

---

## ⚠️ KNOWN HAZARDS — READ BEFORE EDITING

### 1. `output.alsa` REQUIRES `self_sync=false` — DO NOT REMOVE THIS FLAG
```liquidsoap
output.alsa(id="alsa_out", device="hw:0,0", self_sync=false, radio_safe)
```
Without `self_sync=false`, ALSA claims the hardware clock. `output.harbor` claims wall-clock.
Two self-sync outputs on one source → `Clock_base.Sync_error` → process exits after ~2 hours.
`self_sync=false` makes ALSA follow harbor's clock. Do not revert this.

`output.alsa` must remain active for the transmitter feed. Do NOT use it inside Docker.

### 2. File Permissions
The `liquidsoap` systemd service runs as the `liquidsoap` user. Parent directories (`/home/tower-two`, etc.) must have `o+x` permissions for the user to traverse them and read the script/music.

### 3. `track_sensitive=false` is load-bearing
Without it, transitions between HTTP streams will hang indefinitely waiting for a track end.

### 4. Deprecated syntax in 2.3.x
- Use `ref.set(v)` or `ref() == v` instead of `:=` or `!`.
- Use `settings.x.y.set(v)` instead of `set("x.y", v)`.

---

## Change Log — 2026-04-10 Session 3 (Host Migration)

### Migrated: Liquidsoap from Docker to Host
Moved the radio engine to a native systemd service (`liquidsoap-tower`).
- **Latency Fixed:** Audio delay dropped from 2-5s to ~20ms.
- **Telnet Reliability:** Intermittent timeouts eliminated by direct localhost connection.
- **FFmpeg Removed:** The `tower-audio-out.service` is no longer needed as Liquidsoap speaks to ALSA directly.

### Fixed: Sync Errors
Resolved `Clock_base.Sync_error` by unifying the audio stream under a single `mksafe()` wrapper before splitting to ALSA and Harbor outputs.

### Optimized: Dashboard Backend
Refined `app.py` to batch all telnet reads into a single connection. Added support for the Liquidsoap 2.3.x banner format.

---

## TODO — Next Session

### 1. Persistent Custom Stream Switching
Implement a way for Liquidsoap to actually switch to an arbitrary URL pasted in the "Custom Stream" field.
- **Liquidsoap 2.x approach:** Use `input.http({ !custom_url_ref })`.

### 2. Multi-User Lockout / Safety
Add a "Take Control" indicator to prevent multiple staff members from fighting over faders.

### 3. Audio History Graph
Add a sparkline showing the last 5 minutes of LUFS history for the FM Monitor.

---

## Diagnostics

```bash
# Liquidsoap health (telnet):
echo -e 'radio.selected\nexit' | nc -w 2 127.0.0.1 1234

# Dashboard API:
curl -s http://127.0.0.1:8080/api/status

# Harbor stream:
curl -s --max-time 3 -o /dev/null -w '%{http_code} %{size_download}b' http://localhost:8001/monitor.mp3

# Service logs:
systemctl status liquidsoap-tower
journalctl -u liquidsoap-tower -f

# Check who is using ALSA:
lsof /dev/snd/*
```
