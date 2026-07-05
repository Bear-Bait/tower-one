# Liquidsoap: Docker → Host Migration
**Priority:** HIGH — eliminates 2-5 second audio latency, fixes telnet timeouts  
**Host:** tower-two (wgxc-tower-fallback), root@10.50.0.25  
**Risk:** Low. Rollback = `docker compose up -d liquidsoap`. Dashboard is unaffected.

---

## Why

Liquidsoap runs in Docker. Docker has no direct ALSA access, so:
- Audio output goes: Liquidsoap → MP3 encode → HTTP buffer → ffmpeg → ALSA
- That chain adds **2–5 seconds of latency** between what you switch and what the transmitter hears
- Telnet from the dashboard container routes through Docker's host-gateway bridge, causing intermittent timeouts

Running Liquidsoap directly on the host:
- `output.alsa` works natively (only crashes *inside* Docker)
- Audio latency drops to ~20ms
- Telnet connects to localhost:1234 directly — no Docker network layer
- The `tower-audio-out.service` (ffmpeg) can be removed — Liquidsoap plays to ALSA itself
- `output.harbor` still works — browser monitor player unchanged

---

## Current State

```
Docker: liquidsoap container (savonet/liquidsoap:v2.1.4)
  - liq script: /home/tower-two/wgxc-dashboard/liquidsoap/wgxc-tower.liq
  - music dir:  /home/tower-two/wgxc-dashboard/music/emergency/
  - telnet:     port 1234 (exposed to host)
  - harbor:     port 8001 (exposed to host)

Host systemd: tower-audio-out.service
  - ffmpeg pulling http://localhost:8001/monitor.mp3 → ALSA hw:0,0
  - This becomes UNNECESSARY once Liquidsoap outputs directly to ALSA
```

---

## Migration Steps

### Step 1: Install Liquidsoap on the host

```bash
apt update && apt install -y liquidsoap
liquidsoap --version   # expect 2.3.2
```

### Step 2: Update the liq script for 2.3.x syntax

The `set()` function is removed in 2.3.x. Replace with settings calls.  
Also add `output.alsa` (works on host) and remove the need for harbor-only output.

Write the updated script to `/home/tower-two/wgxc-dashboard/liquidsoap/wgxc-tower.liq`:

```liquidsoap
# wgxc-tower.liq — wgxc-tower-fallback (host, Liquidsoap 2.3.x)

settings.log.file.path.set("/tmp/liquidsoap.log")
settings.server.telnet.set(true)
settings.server.telnet.port.set(1234)
settings.server.telnet.bind_addr.set("0.0.0.0")
settings.harbor.bind_addrs.set(["0.0.0.0"])

# --- SOURCES ---
automation = input.http(id="automation", timeout=10., "http://audio.wavefarm.org:8000/automation.mp3")
live       = input.http(id="live",       timeout=10., "http://audio.wavefarm.org:8000/live.mp3")

emergency_queue    = request.queue(id="emergency_queue")
emergency_fallback = playlist(id="emergency_fallback", mode="normal", reload=60., "/music/emergency")
emergency          = fallback(track_sensitive=false, [emergency_queue, emergency_fallback])

# --- CONTROL REFS ---
selected_source = ref("automation")
volume          = ref(1.0)

# --- PER-SOURCE GAIN ---
automation_gain = ref(1.0)
live_gain       = ref(1.0)
emergency_gain  = ref(1.0)

automation = amplify({ !automation_gain }, automation)
live       = amplify({ !live_gain },       live)
emergency  = amplify({ !emergency_gain },  emergency)

# --- ROUTING ---
radio = switch(track_sensitive=false, [
  ({ !selected_source == "automation" }, automation),
  ({ !selected_source == "live" },       live),
  ({ !selected_source == "emergency" },  emergency)
])
radio = amplify({!volume}, radio)

# --- TELNET API ---
server.register(namespace="radio", "select",
  fun(s) -> begin selected_source := s; "OK" end)

server.register(namespace="radio", "selected",
  fun(_) -> !selected_source)

server.register(namespace="audio", "volume",
  fun(v) -> begin volume := float_of_string(v); "OK" end)

server.register(namespace="audio", "get_volume",
  fun(_) -> string_of_float(!volume))

server.register(namespace="audio", "gain",
  fun(s) -> begin
    let parts = string.split(separator=" ", s) in
    let src   = list.nth(parts, 0) in
    let v     = float_of_string(list.nth(parts, 1)) in
    if src == "automation" then automation_gain := v
    elsif src == "live"     then live_gain := v
    elsif src == "emergency" then emergency_gain := v
    end;
    "OK"
  end)

server.register(namespace="audio", "get_gain",
  fun(src) ->
    if src == "automation" then string_of_float(!automation_gain)
    elsif src == "live"     then string_of_float(!live_gain)
    elsif src == "emergency" then string_of_float(!emergency_gain)
    else "unknown"
    end)

server.register(namespace="emergency_queue", "skip",
  fun(_) -> begin emergency_queue.skip(); "OK" end)

# --- OUTPUT ---
# Harbor: browser monitor player at http://10.50.0.25:8001/monitor.mp3
output.harbor(%mp3, port=8001, mount="monitor.mp3",
  headers=[("Access-Control-Allow-Origin","*"),
            ("Access-Control-Allow-Headers","*")],
  mksafe(radio))

# ALSA: direct output to headphone jack → transmitter (works on host, not in Docker)
output.alsa(id="alsa_out", device="hw:0,0", mksafe(radio))
```

**Important:** Test the script syntax before creating the service:
```bash
liquidsoap --check /home/tower-two/wgxc-dashboard/liquidsoap/wgxc-tower.liq
```
If it exits 0 with no errors, proceed. If there are syntax errors in 2.3.2, fix them before proceeding.

### Step 3: Create systemd service for Liquidsoap

```bash
cat > /etc/systemd/system/liquidsoap-tower.service << 'EOF'
[Unit]
Description=WGXC Tower Liquidsoap Radio Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/liquidsoap /home/tower-two/wgxc-dashboard/liquidsoap/wgxc-tower.liq
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
```

### Step 4: Stop the Docker liquidsoap container

```bash
cd /home/tower-two/wgxc-dashboard
docker compose stop liquidsoap
```

Do NOT remove it yet — leave it as rollback.

### Step 5: Start host Liquidsoap and verify

```bash
systemctl start liquidsoap-tower
sleep 3
systemctl status liquidsoap-tower --no-pager

# Verify telnet works
echo -e 'radio.selected\nexit' | nc -w 2 127.0.0.1 1234

# Verify harbor stream
curl -s --max-time 3 -o /dev/null -w '%{http_code} %{size_download}b' http://localhost:8001/monitor.mp3

# Check liq log
tail -30 /tmp/liquidsoap.log
```

Expected: telnet returns `automation`, harbor returns `200`.

### Step 6: Update docker-compose — dashboard talks to localhost

The dashboard container currently connects to `host.docker.internal:1234`. With Liquidsoap on the host, it still connects to the host — this is ALREADY CORRECT and needs no change.

Verify `.env` or `docker-compose.yml` has:
```
LIQUIDSOAP_HOST=host.docker.internal
LIQUIDSOAP_PORT=1234
```
This still works — `host.docker.internal` resolves to the Docker host gateway, which is the host running Liquidsoap.

### Step 7: Test dashboard API

```bash
curl -s http://127.0.0.1:8080/api/status
# Expect: {"selected":"automation","volume":"1.0","host":"..."}
```

### Step 8: Stop tower-audio-out.service (ffmpeg no longer needed)

Liquidsoap now plays directly to ALSA. The ffmpeg bridge is redundant.

```bash
systemctl stop tower-audio-out
systemctl disable tower-audio-out
```

Verify audio still plays through the headphone jack by listening or checking the liq log for `[alsa_out:3] Start`.

### Step 9: Enable Liquidsoap on boot and clean up Docker

```bash
systemctl enable liquidsoap-tower

# Remove liquidsoap from docker-compose (dashboard only)
cd /home/tower-two/wgxc-dashboard
docker compose rm -f liquidsoap
```

Update `docker-compose.yml` — remove the entire `liquidsoap:` service block and the `depends_on: liquidsoap` from dashboard. Keep only:

```yaml
services:
  dashboard:
    build:
      context: .
      dockerfile: Dockerfile.dashboard
    container_name: tower-dashboard
    env_file: .env
    environment:
      - LIQUIDSOAP_HOST=host.docker.internal
      - LIQUIDSOAP_PORT=1234
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./music:/music
    ports:
      - "8080:5000"
    restart: always
```

```bash
docker compose up -d   # restart dashboard with updated compose
```

---

## Rollback (if anything breaks)

```bash
# Stop host Liquidsoap
systemctl stop liquidsoap-tower

# Restart Docker liquidsoap
cd /home/tower-two/wgxc-dashboard
docker compose up -d liquidsoap

# Restart ffmpeg bridge
systemctl start tower-audio-out
```

The dashboard needs no changes — it connects to host.docker.internal:1234 either way.

---

## Known Syntax Differences: 2.1.4 → 2.3.2

| 2.1.4 | 2.3.2 |
|-------|-------|
| `set("log.file.path", v)` | `settings.log.file.path.set(v)` |
| `set("server.telnet", true)` | `settings.server.telnet.set(true)` |
| `set("server.telnet.port", 1234)` | `settings.server.telnet.port.set(1234)` |
| `set("harbor.bind_addr", "0.0.0.0")` | `settings.harbor.bind_addrs.set(["0.0.0.0"])` |
| `"#{!volume}"` string interpolation | `string_of_float(!volume)` preferred |
| `playlist.safe(path)` | removed — use `playlist(...)` |
| `request.queue(id=...)` | same |
| `fallback(track_sensitive=false, ...)` | same |
| `switch(track_sensitive=false, [...])` | same |

If `liquidsoap --check` reports unknown settings keys, check the 2.3.x docs:  
`liquidsoap --list-settings 2>/dev/null | grep harbor`

---

## After Migration: Update the Main Handoff

Update `wgxc-dashboard-HANDOFF.md`:
- Architecture diagram: replace Docker liquidsoap with systemd
- Remove tower-audio-out.service from the architecture
- Note that `output.alsa` is now ACTIVE (not disabled)
- Update known hazards: ALSA in Docker is still disabled, but host is fine
