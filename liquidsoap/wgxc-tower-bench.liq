# wgxc-tower.liq - wgxc-tower-fallback (host, Liquidsoap 2.3.x)

settings.log.file.path.set("/tmp/liquidsoap.log")
settings.server.telnet.set(true)
settings.server.telnet.port.set(1234)
settings.server.telnet.bind_addr.set("0.0.0.0")
settings.harbor.bind_addrs.set(["0.0.0.0"])

# ALSA output buffer: default 0.02s frame gave an 882-frame (20ms) hw buffer ->
# real underruns every 1-2h (/tmp/liquidsoap.log 2026-07-01 13:00/13:24/15:16).
# The hw buffer follows the frame size (settings.alsa.alsa_buffer is the
# internal ring only). 0.1s frame -> 4410-frame (100ms) hw buffer.
settings.frame.duration := 0.1
settings.alsa.alsa_buffer := 8820
settings.alsa.periods := 0  # DAC rejects forced periods (Invalid argument); use ALSA default

# --- METADATA TRACKING ---
meta_title  = ref("")
meta_artist = ref("")

# --- SOURCES ---
# poll_delay=30. on stream: stream is rarely active, avoids 404 log spam every 2s
automation_src = input.http(id="automation", timeout=10., "https://audio.wavefarm.org/automation.mp3")
# stream_src is the dynamic Stream Selector source.
# Default to a dummy URL to avoid 404 log spam; the dashboard will update this on selection.
stream_src = input.http(id="live", timeout=10., poll_delay=30., "http://localhost:8001/offline.mp3")

# Register metadata callbacks via .on_metadata method (Liquidsoap 2.x API)
automation_src.on_metadata(fun(m) -> begin
  meta_title.set(list.assoc(default="", "title", m));
  meta_artist.set(list.assoc(default="", "artist", m))
end)
stream_src.on_metadata(fun(m) -> begin
  meta_title.set(list.assoc(default="", "title", m));
  meta_artist.set(list.assoc(default="", "artist", m))
end)

emergency_queue    = request.queue(id="emergency_queue")
# No fallback playlist — if the queue is empty, it stays silent.
# BENCH: crossfade consecutive queue tracks (kills between-track gaps)
emergency_xf       = crossfade(fade_out=1.5, fade_in=1.5, duration=2., emergency_queue)
emergency_src      = mksafe(emergency_xf)

# Emergency fallback on automation connection loss (not triggered by quiet audio).
# Plays from the operator-managed emergency_playlist via the dashboard GUI.
# See HANDOFF-2026-07-02-emergency-fallback.md for design rationale and test matrix.
emergency_auto_pl  = playlist(id="emergency_auto",
  "/home/tower-one/wgxc-dashboard/playlists/emergency_playlist.m3u")

# --- CONTROL REFS ---
selected_source = ref("automation")
stream_label    = ref("NONE")
volume          = ref(1.0)

# --- TWO-LAYER GAIN: TRIM (fader) + BUS (routing) ---
# TRIM: level control (0.0–1.5). Fader-controlled. Cannot route a silent source.
# BUS:  routing gate (0.0=off, 1.0=on, floats allowed for crossfade).
# Combined output = trim * bus * source — moving the trim fader alone cannot unmute.
automation_trim = ref(1.0)
live_trim       = ref(1.0)
emergency_trim  = ref(1.0)

automation_bus = ref(1.0)
live_bus       = ref(0.0)
emergency_bus  = ref(0.0)

# Wrap automation with connection-loss fallback: engages only when the upstream
# mount becomes unreachable (not on quiet content — see HANDOFF §3/design rationale).
# buffer=2./max=10.: deeper elastic buffer rides through short stalls so a brief
# blip doesn't flap the fallback (HANDOFF §4); bench test 2026-07-02 showed the
# default shallow buffer wedged the source on recovery.
automation_avail   = fallback(id="automation_avail", track_sensitive=false,
  [buffer(buffer=2., max=10., automation_src), mksafe(emergency_auto_pl)])
# BENCH: server-side crossfade. Dashboard contract unchanged — JS writes bus
# targets (0.0/1.0) via audio.bus; the server ramps real gain toward the
# target over xfade_time seconds. A browser dying mid-crossfade can no longer
# strand a partial gain: both buses always converge to their targets.
xfade_time = ref(3.0)

def smooth_bus(target) =
  cur  = ref(target())
  last = ref(time())
  fun () -> begin
    now = time()
    dt = now - last()
    last.set(now)
    t = target()
    c = cur()
    step = if xfade_time() <= 0.05 then 1.0 else dt / xfade_time() end
    c =
      if abs(t - c) <= step then t
      elsif t > c then c + step
      else c - step end
    cur.set(c)
    c
  end
end

automation_bus_s = smooth_bus({ automation_bus() })
live_bus_s       = smooth_bus({ live_bus() })
emergency_bus_s  = smooth_bus({ emergency_bus() })

automation = amplify(automation_bus_s, amplify({ automation_trim() }, automation_avail))
stream     = amplify(live_bus_s,       amplify({ live_trim() },       buffer(stream_src)))
emergency  = amplify(emergency_bus_s,  amplify({ emergency_trim() },  emergency_src))

# --- ROUTING ---
# add() mixes all sources simultaneously. Routing = setting bus to 0.0 or 1.0.
# mksafe() per-source: a disconnected stream produces silence rather than blocking.
radio = add(normalize=false, [mksafe(automation), mksafe(stream), mksafe(emergency)])

# ══════════════════════════════════════════════════════════════════
# DSP — broadcast processing chain (both stages enabled at startup)
# 1) LUFS normalizer: slow AGC, live-tunable via telnet `dsp.*`
# 2) fastlookaheadlimiter: brick-wall -2 dBFS, always active
#    (LADSPA internal lookahead — does NOT stall self_sync ALSA clock)
# ══════════════════════════════════════════════════════════════════
dsp_norm_on     = ref(true)     # LUFS normalizer: enabled at startup
dsp_norm_target = ref(-23.0)    # target loudness (LUFS)
# Gentle-profile trial 2026-07-02: 15s window + 20s ramps (was 8s/10s/10s) —
# previous values rode gain within musical phrases and read as harsh.
# Under empirical evaluation via dsp-gain-monitor.sh; revert if it undershoots.
dsp_norm_window = ref(15.0)     # integration window (s)
dsp_norm_up     = ref(20.0)     # gain-rise time constant (s)
dsp_norm_down   = ref(20.0)     # gain-drop time constant (s) — LS default 0.1s stepped
                                # audibly at the 10Hz frame rate (frame.duration 0.1)

# 1) LUFS normalizer (slow automatic gain control).
#    track_sensitive=false: default true hard-resets gain on every track mark
#    from the automation stream — same audible jump as a fast down ramp.
radio = normalize(
  id="dsp_norm",
  enabled={ dsp_norm_on() },
  lufs=true,
  target={ dsp_norm_target() },
  window={ dsp_norm_window() },
  up={ dsp_norm_up() },
  down={ dsp_norm_down() },
  track_sensitive=false,
  gain_min=-15., gain_max=30.,
  radio)

# Keep a handle on the normalizer stage: .gain() reads the current AGC
# coefficient (linear), exposed as telnet `dsp.gain` for empirical logging.
dsp_norm_src = radio

# 2) Brick-wall peak limiter — LADSPA fastlookaheadlimiter (UID 1913).
#    Always active. -2 dBFS ceiling, 0.3s release.
#    Internal lookahead does NOT create a Liquidsoap pipeline delay.
radio = ladspa.fastlookaheadlimiter(
  input_gain=0.0,
  limit=-2.0,
  release_time=0.3,
  radio)

# Post-DSP master gain — sits after normalizer+limiter so it actually affects output level.
radio = amplify({volume()}, radio)

radio_safe = mksafe(radio)

# RMS metering tap — rms() wraps the source; .rms() method returns current level (linear 0.0–1.0)
rms_tap = rms(id="meter", radio_safe)
radio_safe = lufs(id="lufsmeter", rms_tap)

# --- TELNET API ---

# Hard-cut switch: sets bus values immediately + updates selected_source label.
# Used by dashboard on page load to sync state after a mid-crossfade refresh.
server.register(namespace="radio", "select",
  fun(s) -> begin
    selected_source.set(s);
    if s == "automation" then
      automation_bus.set(1.0); live_bus.set(0.0); emergency_bus.set(0.0)
    elsif s == "live" then
      automation_bus.set(0.0); live_bus.set(1.0); emergency_bus.set(0.0)
    elsif s == "emergency" then
      automation_bus.set(0.0); live_bus.set(0.0); emergency_bus.set(1.0)
    end;
    "OK"
  end)

# Label-only: updates selected_source without touching bus or trim.
# Used by dashboard JS when it manages crossfade/bus ramping itself.
server.register(namespace="radio", "selected",
  fun(_) -> selected_source())

server.register(namespace="radio", "label",
  fun(s) -> begin selected_source.set(s); "OK" end)

# Source readiness (for BUFFERING indicator)
server.register(namespace="radio", "ready",
  fun(src) ->
    if src == "automation" then "#{automation_src.is_ready()}"
    elsif src == "live"     then "#{stream_src.is_ready()}"
    # Inner queue, not the mksafe() wrapper — mksafe is *always* ready, which
    # made this signal useless and stalled the dashboard's auto-advance on
    # tracks with unknown duration.
    elsif src == "emergency" then "#{emergency_queue.is_ready()}"
    else "unknown"
    end)

# Master volume
server.register(namespace="audio", "volume",
  fun(v) -> begin volume.set(float_of_string(default=1.0, v)); "OK" end)

server.register(namespace="audio", "get_volume",
  fun(_) -> string.float(volume()))

# Stream Label (display name for the active Stream Selector URL)
server.register(namespace="audio", "stream_label",
  fun(l) -> begin stream_label.set(l); "OK" end)

server.register(namespace="audio", "get_stream_label",
  fun(_) -> stream_label())

# TRIM — per-source level fader (does NOT affect routing)
server.register(namespace="audio", "gain",
  fun(s) -> begin
    let parts = string.split(separator=" ", s) in
    if list.length(parts) >= 2 then
      let src = list.nth(parts, 0) in
      let v = float_of_string(default=1.0, list.nth(parts, 1)) in
      if src == "automation" then automation_trim.set(v)
      elsif src == "live"     then live_trim.set(v)
      elsif src == "emergency" then emergency_trim.set(v)
      end
    end;
    "OK"
  end)

server.register(namespace="audio", "get_gain",
  fun(src) ->
    if src == "automation" then string.float(automation_trim())
    elsif src == "live"     then string.float(live_trim())
    elsif src == "emergency" then string.float(emergency_trim())
    else "unknown"
    end)

# BUS — routing gate per source (0.0=off, 1.0=on, floats ok for crossfade)
server.register(namespace="audio", "bus",
  fun(s) -> begin
    let parts = string.split(separator=" ", s) in
    if list.length(parts) >= 2 then
      let src = list.nth(parts, 0) in
      let v = float_of_string(default=0.0, list.nth(parts, 1)) in
      if src == "automation" then automation_bus.set(v)
      elsif src == "live"     then live_bus.set(v)
      elsif src == "emergency" then emergency_bus.set(v)
      end
    end;
    "OK"
  end)

server.register(namespace="audio", "get_bus",
  fun(src) ->
    if src == "automation" then string.float(automation_bus())
    elsif src == "live"     then string.float(live_bus())
    elsif src == "emergency" then string.float(emergency_bus())
    else "unknown"
    end)

# Metadata (last received from any source that sends it)
server.register(namespace="audio", "get_meta",
  fun(_) -> begin
    let t = meta_title() in
    let a = meta_artist() in
    if a != "" and t != "" then "#{a} — #{t}"
    elsif t != "" then t
    elsif a != "" then a
    else ""
    end
  end)

# RMS: read from the rms() wrapper via its .rms method
server.register(namespace="audio", "get_rms",
  fun(_) -> string.float(rms_tap.rms()))

# ── DSP controls ──
def is_on(v) = v == "on" or v == "true" or v == "1" end
server.register(namespace="dsp", "norm",
  fun(v) -> begin dsp_norm_on.set(is_on(v)); "norm=#{dsp_norm_on()}" end)
server.register(namespace="dsp", "target",
  fun(v) -> begin dsp_norm_target.set(float_of_string(default=-23., v)); "target=#{dsp_norm_target()} LUFS" end)
server.register(namespace="dsp", "window",
  fun(v) -> begin dsp_norm_window.set(float_of_string(default=8., v)); "window=#{dsp_norm_window()}s" end)
server.register(namespace="dsp", "up",
  fun(v) -> begin dsp_norm_up.set(float_of_string(default=10., v)); "up=#{dsp_norm_up()}s" end)
server.register(namespace="dsp", "down",
  fun(v) -> begin dsp_norm_down.set(float_of_string(default=10., v)); "down=#{dsp_norm_down()}s" end)
server.register(namespace="dsp", "gain",
  fun(_) -> begin
    g = dsp_norm_src.gain()
    "#{string.float(dB_of_lin(g), decimal_places=2)} dB (lin #{string.float(g, decimal_places=5)})"
  end)
server.register(namespace="dsp", "status",
  fun(_) -> "norm=#{dsp_norm_on()} target=#{dsp_norm_target()}LUFS window=#{dsp_norm_window()}s up=#{dsp_norm_up()}s down=#{dsp_norm_down()}s | lim=always-on limit=-2.0dBFS release=0.3s")

# BENCH: crossfade ramp length (seconds) for server-side bus smoothing
server.register(namespace="audio", "xfade_time",
  fun(v) -> begin xfade_time.set(float_of_string(default=3., v)); "xfade_time=#{xfade_time()}s" end)

# Clear cached metadata (call when switching sources so stale titles don't persist)
server.register(namespace="audio", "clear_meta",
  fun(_) -> begin
    meta_title.set("");
    meta_artist.set("");
    "OK"
  end)

# Automation connection-loss fallback status (observability probe).
# Returns "true" if the fallback playlist is active (automation source not ready),
# "false" if automation is connected. See HANDOFF-2026-07-02-emergency-fallback.md.
server.register(namespace="automation", "fallback_active",
  fun(_) -> if automation_src.is_ready() then "false" else "true" end)

# Emergency queue: skip current item.
server.register(namespace="emergency_queue", "skip",
  fun(_) -> begin
    log("emergency_queue.skip: skipping queue")
    emergency_queue.skip();
    "OK"
  end)

# play_now: push a file and play it immediately.
# Checks is_ready() BEFORE pushing to know whether the queue was already active.
server.register(namespace="emergency_queue", "play_now",
  fun(path) -> begin
    let was_ready = emergency_queue.is_ready() in
    log("emergency_queue.play_now: path=#{path} was_ready=#{was_ready}");
    emergency_queue.push(request.create(path));
    if was_ready then 
      emergency_queue.skip() 
    end;
    "OK"
  end)

# --- RESTREAM OUTPUT ---
# Relay the master bus to a remote Icecast server.
# blank.strip watches for silence on the restream path only (does not affect ALSA/harbor).
# fallback switches to a local emergency playlist when the signal has been silent for 15s,
# then switches back automatically when the live signal returns.
restream_main = blank.strip(id="restream_blank",
  max_blank=15., start_blank=false, threshold=-50., radio_safe)
restream_emergency_pl = playlist(id="restream_emergency",
  "/home/tower-one/wgxc-dashboard/playlists/emergency_playlist.m3u")
restream_chain = fallback(id="restream_chain",
  track_sensitive=false,
  [restream_main, mksafe(restream_emergency_pl)])

output.icecast(%mp3(bitrate=128),
  id="restream_out",
  host="203.0.113.10",
  port=8000,
  password=string.trim(file.contents("/home/tower-one/wgxc-dashboard/liquidsoap/restream.pass")),
  mount="test-restream",
  name="WGXC Tower-Two Relay",
  description="Live relay from tower-two with silence failover",
  on_start=(fun () -> log("restream: connected to 203.0.113.10:8000/test-restream")),
  on_stop=(fun () -> log("restream: disconnected from remote Icecast")),
  restream_chain)

# --- OUTPUT ---
# Harbor: browser monitor player at http://10.50.0.25:8001/monitor.mp3
output.harbor(%mp3, port=8001, mount="monitor.mp3",
  headers=[("Access-Control-Allow-Origin","*"),
            ("Access-Control-Allow-Headers","*")],
  radio_safe)

# Failsafe keepalive: 4s of 17 kHz @ -18 dBFS every 100s, TX path only.
# (2s @ -20 every 180s proved marginal 2026-07-01: single bursts registered only
#  intermittently — the sensor needs more charge time and overdrive; >=2 bursts
#  per 4-minute delay window tolerates a missed burst.)
# Keeps the Angry Audio Failsafe silence sensor fed through legitimately quiet
# program content (bench-validated 2026-07-01: sensor trips ~= -28 dBFS, flat to
# 19.5 kHz; 2s @ -20 dBFS reliably resets the 4-minute delay; transmitter 15 kHz
# lowpass keeps it off-air). Deliberately NOT mixed into radio_safe: the restream
# blank.strip failover must still see real silence.
keepalive = single(id="keepalive", "/home/tower-one/wgxc-dashboard/liquidsoap/keepalive-cycle.flac")
tx_out = add(id="tx_mix", normalize=false, [radio_safe, mksafe(keepalive)])

# ALSA: direct output to headphone jack → transmitter
# self_sync=true: ALSA is clock master; eliminates ~68min buffer underrun from clock drift.
output.alsa(id="alsa_out", device="hw:CARD=DAC,DEV=0", self_sync=true, tx_out)



# --- PROMETHEUS METRICS (bench; 2026-07-02) ---
# Build lacks the prometheus optional; hand-render text exposition via harbor.
prom_rms_val  = ref(0.0)
prom_lufs_val = ref(-70.0)

thread.run(every=1., fun () -> begin
  prom_rms_val.set(rms_tap.rms())
  prom_lufs_val.set(radio_safe.lufs())
end)

def prom_metrics_handler(_) =
  body = "# HELP liquidsoap_rms Radio safe output RMS level (linear 0.0-1.0)\n"
       ^ "# TYPE liquidsoap_rms gauge\n"
       ^ "liquidsoap_rms{source=\"radio_safe\"} #{prom_rms_val()}\n"
       ^ "# HELP liquidsoap_lufs Radio safe output integrated loudness (LUFS)\n"
       ^ "# TYPE liquidsoap_lufs gauge\n"
       ^ "liquidsoap_lufs{source=\"radio_safe\"} #{prom_lufs_val()}\n"
  http.response(content_type="text/plain; version=0.0.4", data=body)
end

harbor.http.register.simple(port=9099, method="GET", "/metrics", prom_metrics_handler)
