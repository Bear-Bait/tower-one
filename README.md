# tower-one — WGXC 90.7-FM Transmitter-Site Broadcast Stack

The audio chain that feeds the FM transmitter for [WGXC 90.7-FM](https://wavefarm.org/wgxc),
Wave Farm's creative community radio station in New York's Hudson Valley. A
Liquidsoap radio engine, a Flask operator dashboard, a Prometheus exporter, and
the operations documentation that keeps a volunteer-run station on the air —
all running on one small Debian box at the tower site.

This is a sanitized public snapshot of a production system, published the night
before the box was promoted from bench spare to primary transmitter feed
(2026-07-06). Real credentials, the restream host, and the music library are
excluded; everything else is exactly what runs on air.

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

## What's interesting here

- **Emergency fallback on connection loss, not silence.**
  If the automation upstream drops, the engine fails over to a locally stored,
  operator-curated playlist in ~12 seconds and recovers automatically when the
  upstream returns. Failover triggers on *connection* state, so intentional
  quiet programming (this is an experimental-arts station) never trips it.
  Design rationale and the live test matrix are in
  [`wgxc-dashboard-HANDOFF.md`](wgxc-dashboard-HANDOFF.md).

- **Keepalive tone for the hardware failsafe.**
  The transmitter site has a hardware silence sensor that switches to a backup
  audio source after sustained dead air. A near-ultrasonic tone
  ([`liquidsoap/keepalive-cycle.flac`](liquidsoap/keepalive-cycle.flac)) rides
  under the program audio so the sensor sees signal whenever the chain is
  healthy — silence then *means* failure, and the failsafe only fires when it
  should.

- **ALSA buffer forensics, documented in the config.**
  The engine originally underran every 1–2 hours. The comment block at the top
  of [`liquidsoap/wgxc-tower.liq`](liquidsoap/wgxc-tower.liq) records the
  diagnosis: the hardware buffer follows Liquidsoap's frame duration (20 ms →
  882 frames), not the internal ring setting everyone tunes first. One
  `settings.frame.duration` change bought a 100 ms hardware buffer and zero
  underruns since.

- **Operator dashboard as an installable PWA.**
  [`app/`](app/) is a Flask app with PIN-based staff auth (salted hashes, no
  plaintext), source switching, per-bus gain, an emergency-queue UI, and live
  loudness display — installable to a phone home screen so a volunteer can fix
  dead air from bed at 3 a.m.

- **Ops writing.** The docs are the other half of the system:
  [`MONITORING.md`](MONITORING.md) (goal: "never make a 3 a.m. drive to the
  tower for something that could have been caught"),
  [`LIQUIDSOAP-HOST-MIGRATION.md`](LIQUIDSOAP-HOST-MIGRATION.md) (Docker → host
  migration that removed 2–5 s of transmitter latency, with rollback plan), and
  [`AUTOMATION-TAGGING.md`](AUTOMATION-TAGGING.md) (hands-off library ingest).

## Layout

| Path | What it is |
|------|------------|
| `liquidsoap/wgxc-tower.liq` | Production radio engine: source buses, crossfaded emergency queue, telnet control surface, restream output |
| `liquidsoap/wgxc-tower-bench.liq` | Bench-test variant used to qualify changes before they touch air |
| `app/` | Flask dashboard (PWA): auth, source control, file browser, loudness UI |
| `tower_exporter.py` | Prometheus exporter: Liquidsoap state, audio levels, upstream health, crash counts |
| `tools/` | Staff account provisioning (`add_user.py`, `seed_staff.py`) |
| `playlists/emergency_playlist.m3u` | The curated automation-loss fallback playlist |
| `docker-compose.yml` / `Dockerfile.dashboard` | Dashboard container (the engine itself runs on the host — see the migration doc) |

## Running it

```sh
cp .env.example .env      # set the dashboard password
docker compose up -d      # dashboard on :8080
# the Liquidsoap engine runs on the host under systemd — see
# LIQUIDSOAP-HOST-MIGRATION.md for the unit and ALSA notes
```

Note that hostnames and addresses in the docs reflect the private WireGuard
mesh (`10.50.0.0/24`) this fleet runs on; the Icecast restream host in the
`.liq` files is redacted to a TEST-NET address (`203.0.113.10`).

---

Built and operated by [Forrest Muelrath](https://forrestmuelrath.com) as part
of Wave Farm's broadcast-fleet infrastructure work, with AI-agent pairing
under human review — every change to this chain ships with a hypothesis, a
read-only evidence phase, and a rollback plan, because the failure mode is
dead air on a licensed FM frequency.
