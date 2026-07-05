# tower-two Monitoring & Observability
**Host:** 10.50.0.25 (wgxc-tower-fallback)  
**Goal:** Never make a 3am drive to the broadcast tower for something that could have been caught.

---

## What's Running

| Service | Port | Covers |
|---------|------|--------|
| `prometheus-node-exporter` | 9100 | CPU, memory, disk, temp, network, load |
| `tower-exporter.py` | 9200 | Liquidsoap state, audio output, upstream, crashes |
| Both scraped by Prometheus on monitor | 10.50.0.7:9090 | — |

---

## Alert Rules (9 active)

All rules live in `/etc/prometheus/alert_rules.yml` on the monitor.

### Critical — Page immediately

| Alert | Condition | What it means |
|-------|-----------|---------------|
| `TowerLiquidsoapDown` | `tower_liquidsoap_up == 0` for 1m | Engine unreachable, transmitter likely silent |
| `TowerALSAOutputInactive` | `tower_alsa_output_active == 0` for 2m | Liquidsoap lost the audio device, nothing to transmitter |
| `TowerUpstreamUnreachable` | `tower_upstream_reachable == 0` for 3m | Can't reach audio.wavefarm.org, automation/live feeds dead |
| `TowerNodeDown` | `up{alias="tower-two"} == 0` for 2m | Machine offline or WireGuard tunnel dropped |

### Warning — Investigate soon

| Alert | Condition | What it means |
|-------|-----------|---------------|
| `TowerLiquidsoapCrashLoop` | >2 restarts in 1h | Clock bug, memory issue, or bad liq script change |
| `TowerCPUHot` | CPU temp >80°C for 5m | Dust, fan failure, thermal throttling → audio dropouts |
| `TowerDiskFull` | Root partition >85% | Logs or music files growing uncontrolled |
| `TowerMemoryPressure` | Available <300MB for 10m | OOM risk — Liquidsoap uses ~450MB normally |
| `TowerUnexpectedReboot` | Uptime <5 minutes | Power outage, kernel panic, or manual reboot |

---

## ⚠️ Hardware Watch — The Disk

```
/dev/sda SMART Power_On_Hours: 47,913+  (as of 2026-04-10)
```

**This drive has over 5.5 years of continuous runtime.** HDDs typically last 3–5 years.
SMART currently reports PASSED with zero bad sectors — but age alone is a risk.

**What to watch in Grafana:**
- `node_disk_io_time_seconds_total{device="sda"}` — sustained I/O → early failure sign
- SMART textfile metrics (from `prometheus-node-exporter-smartmon` timer):
  - `smartmon_reallocated_sector_ct_value` — ANY increase above 0 = dying drive
  - `smartmon_current_pending_sector_value` — unreadable sectors in flight
  - `smartmon_offline_uncorrectable_value` — unrecoverable read errors

**Action plan if any SMART attribute goes non-zero:**
1. Alert fires within 15 minutes
2. Back up `/home/tower-two/wgxc-dashboard/music/emergency/` immediately
3. Order replacement drive (SSD recommended — no mechanical failure modes)
4. Plan a maintenance window. Do not wait.

---

## Key Metrics to Build Grafana Panels For

### Audio State Panel
```promql
# What's on air right now
tower_liquidsoap_source_active{alias="tower-two"}

# Master volume
tower_liquidsoap_master_volume{alias="tower-two"}

# Service restarts (should be flat 0)
tower_liquidsoap_restarts_total{alias="tower-two"}
```

### System Health Panel
```promql
# CPU temperature (alert at >80°C)
node_thermal_zone_temp{alias="tower-two", type="x86_pkg_temp"}

# Available memory
node_memory_MemAvailable_bytes{alias="tower-two"} / 1024 / 1024

# Disk free (%)
node_filesystem_avail_bytes{alias="tower-two", mountpoint="/"} 
  / node_filesystem_size_bytes{alias="tower-two", mountpoint="/"}

# Load average
node_load1{alias="tower-two"}
```

### Uptime / Reliability Panel
```promql
# Uptime in hours
(node_time_seconds{alias="tower-two"} - node_boot_time_seconds{alias="tower-two"}) / 3600

# Is everything up?
tower_liquidsoap_up AND tower_alsa_output_active AND tower_upstream_reachable AND tower_harbor_stream_up
```

---

## Diagnostics Runbook

### "TowerLiquidsoapDown" fires
```bash
ssh root@10.50.0.25
systemctl status liquidsoap-tower
journalctl -u liquidsoap-tower -n 50 --no-pager
tail -30 /tmp/liquidsoap.log
# If stopped: systemctl restart liquidsoap-tower
# If crash-looping: check /tmp/liquidsoap.log for the error before restarting
```

### "TowerALSAOutputInactive" fires (Liquidsoap up but ALSA not held)
```bash
lsof /dev/snd/*
# If something else has it: kill that process, restart liquidsoap-tower
# Common culprit: ffplay or ffmpeg test commands left running from debugging
```

### "TowerCPUHot" fires
```bash
# Check current temp
cat /sys/class/thermal/thermal_zone0/temp   # divide by 1000 for °C
# Check if Liquidsoap is the culprit
top -bn1 | head -15
# 2012 Mac Mini: clean dust from intake vents on bottom
# Fan failure: listen for unusual noise, replace if needed
```

### "TowerNodeDown" fires
```bash
ping 10.50.0.25
# If no response: check WireGuard on MacBook (wg show)
# If WireGuard is up: machine is powered off or crashed
# Physical intervention: power cycle the Mac Mini at the tower
```

### "TowerUpstreamUnreachable" fires
```bash
ssh root@10.50.0.25 "curl -sv http://audio.wavefarm.org:8000/automation.mp3 --max-time 5 -o /dev/null"
# Check WireGuard on tower-two: wg show
# Check if audio.wavefarm.org is actually down (check from another machine)
# If only tower-two is affected: restart wg0 interface
```

---

## Services on tower-two — Full List

```
systemctl status liquidsoap-tower        # Audio engine (Liquidsoap 2.3.2)
systemctl status tower-exporter          # Prometheus audio metrics (:9200)
systemctl status prometheus-node-exporter # Prometheus system metrics (:9100)
systemctl status tower-dashboard         # (Docker) Flask web UI (:8080)
```

All four must be running for the system to function as a broadcast machine.

---

## Path to Primary Broadcast Machine

The Mac Mini needs the following before it can be trusted as primary:

1. **Replace the HDD with an SSD** — 47k+ hour drive is too old for primary duty.
   Recommended: any 2.5" SATA SSD (Samsung 860 EVO, Crucial MX500). ~$40.
   
2. **Configure automatic liquidsoap-tower restart with backoff** — already has
   `Restart=always, RestartSec=5` in systemd. Consider increasing to 30s to prevent
   hammering a broken resource.

3. **Add an `ExecStartPre` health check** — before starting Liquidsoap, verify
   ALSA device is free: `fuser /dev/snd/pcmC0D0p` should return nothing.

4. **Add UPS monitoring** — if the tower has a UPS, expose its battery level to
   Prometheus. A power event with no warning is the #1 cause of unclean reboots
   and filesystem corruption on the HDD.

5. **Test the failover scenario** — deliberately take down `audio.wavefarm.org`
   (or block it on tower-two with iptables) and verify the emergency playlist
   kicks in correctly and the alert fires.

6. **Add Grafana alerting** — currently rules are defined but alertmanager may
   need configuration to send notifications (email, Slack, PagerDuty).
   Check: `systemctl status alertmanager` on monitor.
