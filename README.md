# tower-one — WGXC 90.7-FM Transmitter-Site Broadcast Stack

**Last Updated:** 2026-07-05
**Scope:** Public snapshot of the production audio chain feeding the WGXC 90.7-FM transmitter ([Wave Farm](https://wavefarm.org/wgxc), Hudson Valley, NY)

---

## Overview

`tower-one` is a Debian machine at the FM transmitter site. It runs the full chain between the station's program sources and the transmitter input: a Liquidsoap radio engine (host `systemd` service), a Flask operator dashboard (Docker, installable PWA), a Prometheus exporter, and the operations documentation for all three.

This snapshot was published 2026-07-05, the night before the machine was promoted from bench spare to primary transmitter feed. Credentials, the restream host address, and the music library are removed; code and documentation are otherwise identical to the production tree. See [Sanitization](#sanitization).

---

## Architecture

```
  automation stream ──┐
  (station schedule)  │
                      ├──► Liquidsoap engine ──► ALSA/DAC ──► FM transmitter
  live studio stream ─┤    (wgxc-tower.liq,          │
                      │     host systemd unit)       └──► Icecast restream
  emergency playlist ─┘         ▲    ▲                    (off-site monitor)
  (local files)                 │    │ telnet :1234
                                │    │
                    keepalive   │  Flask dashboard (Docker) ◄── staff PWA
                    tone loop ──┘  tower_exporter.py :9200 ──► Prometheus
```

---

## Design Notes

**Emergency fallback triggers on connection loss.** When the automation upstream disconnects, the engine switches to a locally stored, operator-curated playlist in ~12 seconds and switches back ~3 seconds after upstream recovery (both measured in live tests). The engine watches the stream connection itself — whether the automation server is still delivering data — and ignores audio level, since WGXC broadcasts experimental and transmission-arts programming where long quiet passages are intentional program content. Design rationale and test matrix: [`wgxc-dashboard-HANDOFF.md`](wgxc-dashboard-HANDOFF.md).

**Keepalive tone holds off the hardware failsafe.** The transmitter site has a hardware silence sensor that switches to a backup audio source after sustained dead air. The engine mixes a near-ultrasonic tone ([`liquidsoap/keepalive-cycle.flac`](liquidsoap/keepalive-cycle.flac)) into the program feed at low level, keeping the sensor input above threshold as long as the engine is producing output. The failsafe therefore fires only when the chain itself has stopped.

**ALSA underrun root cause is documented in the config.** The engine underran every 1–2 hours after initial deployment. Root cause: the ALSA hardware buffer size follows Liquidsoap's frame duration (default 0.02 s → 882-frame buffer), not `settings.alsa.alsa_buffer`, which sizes only the internal ring. Setting `settings.frame.duration := 0.1` produced a 4410-frame (100 ms) hardware buffer; zero underruns since. The comment block at the top of [`liquidsoap/wgxc-tower.liq`](liquidsoap/wgxc-tower.liq) records the diagnosis with log timestamps.

**Operator dashboard is an installable PWA.** [`app/`](app/) is a Flask application with PIN-based staff authentication (salted hashes, no plaintext storage), source switching, per-bus gain control, an emergency-queue UI, and live loudness display. Staff install it to a phone home screen and can restore program audio remotely.

**Documentation ships with the system.** [`MONITORING.md`](MONITORING.md) covers the two exporters and alert coverage. [`LIQUIDSOAP-HOST-MIGRATION.md`](LIQUIDSOAP-HOST-MIGRATION.md) documents the Docker → host migration that removed 2–5 seconds of output latency, including the rollback plan. [`AUTOMATION-TAGGING.md`](AUTOMATION-TAGGING.md) documents hands-off library ingest and tagging.

---

## Layout

| Path | Contents |
|------|----------|
| `liquidsoap/wgxc-tower.liq` | Production radio engine: source buses, crossfaded emergency queue, telnet control surface, restream output |
| `liquidsoap/wgxc-tower-bench.liq` | Bench-test variant used to qualify changes before deployment |
| `app/` | Flask dashboard (PWA): auth, source control, file browser, loudness UI |
| `tower_exporter.py` | Prometheus exporter: Liquidsoap state, audio levels, upstream health, crash counts |
| `tools/` | Staff account provisioning (`add_user.py`, `seed_staff.py`) |
| `playlists/emergency_playlist.m3u` | Curated automation-loss fallback playlist |
| `docker-compose.yml`, `Dockerfile.dashboard` | Dashboard container; the engine runs on the host (see migration doc) |

---

## Running It

```bash
cp .env.example .env      # set the dashboard password
docker compose up -d      # dashboard on :8080
```

The Liquidsoap engine runs on the host under `systemd`; see [`LIQUIDSOAP-HOST-MIGRATION.md`](LIQUIDSOAP-HOST-MIGRATION.md) for the unit definition and ALSA configuration.

---

## Sanitization

- `.env` removed; `.env.example` added. All secrets load from environment variables or untracked files.
- Icecast restream host replaced with TEST-NET address `203.0.113.10` in both `.liq` files.
- Music library and runtime playlist state excluded.
- Addresses in `10.50.0.0/24` are the fleet's private WireGuard mesh and are retained.

---

Built and operated by [Forrest Muelrath](https://forrestmuelrath.com) for Wave Farm's broadcast fleet, with AI-agent pairing under human review. Fleet change protocol: written hypothesis, read-only evidence phase, and rollback plan before any change is applied — the failure mode is dead air on a licensed FM frequency.
