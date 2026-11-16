# rf-spectrum

A **receive-only** spectrum-awareness demo for emissions control (EMCON) training. It runs on
a Raspberry Pi 5 with a cheap SDR, and its job is to show a unit that **their own radios are
visible to anyone listening**. That includes the Meshtastic node, the ExpressLRS control link,
the FPV video transmitter, phones and PMR446 handhelds.

- Live **waterfall and spectrum** in the browser (port **8140**, streamed over WebSocket)
- **Band presets** labelled with UK/EU allocations: PMR446, 433 MHz SRD, 868 MHz SRD/LoRa,
  902–928 MHz (US ISM, labelled "not UK"), LTE 800 uplink, 2.4 GHz ISM and 5.8 GHz FPV
- **Signal-of-interest rules**: energy at least N dB over the noise floor, in a band, with a
  given bandwidth, held for a given time, raises an **event**. Hopping rules catch ELRS-style links.
- **Event log** with timestamps, persisted to `/var/lib/rf-spectrum/events.jsonl`
- **EMCON score** panel showing how many emitters were heard, the duty cycle of each, a 0–100
  score, an **RF baseline** button and a **Debrief** (offline by default, Gemini optional)
- Optional **mesh alert** (`meshbus/tx`) and **CoT marker** on new emitters in watched bands.
  Both are **off by default** (see [Alerts](#alerts-off-by-default)).
- `--simulate` mode that needs no hardware. It synthesises LoRa chirp bursts, an ELRS-like
  hopper, an analog VTX carrier, phone uplink bursts, PMR446 voice, civilian background
  emitters and noise.

> **What this tool does not do.** It never transmits, jams or replays anything. It does not
> demodulate or decode any transmission. It only measures energy per frequency bin, and a test
> (`tests/test_receive_only.py`) fails if transmit, replay or decode code ever appears.

---

## Quick start on a Mac (no hardware)

```bash
uv venv -p 3.11 .venv && uv pip install -p .venv/bin/python -r requirements-dev.txt
```

```bash
.venv/bin/python -m rfspectrum --simulate
```

Open <http://localhost:8140>. The **Auto demo** scenario plays the full lesson by itself, or
press **Manual** and switch each simulated radio on yourself. Run the tests with:

```bash
.venv/bin/python -m pytest -q
```

Useful flags: `--scenario manual`, `--no-background` (no civilian emitters), `--seed 1`,
`--preset 5g8`, `--port 8140`.

## Install on the Pi

```bash
sudo bash deploy/install.sh
```

```bash
sudo INSTALL_HACKRF=1 bash deploy/install.sh   # also install hackrf_sweep for 2.4 / 5.8 GHz
```

The installer is idempotent and asks no questions. It:

1. installs Python and the build tools
2. builds the **RTL-SDR Blog** `librtlsdr` (tag `v1.3.6`, override with `RTLSDR_BLOG_REF`)
   into `/usr/local`. This driver supports both the **V4** and the V3. `RTLSDR_DRIVER=debian`
   uses Debian's packages instead (V3 only); `RTLSDR_DRIVER=none` skips the driver.
3. writes udev rules (`/etc/udev/rules.d/20-rtlsdr.rules`, `plugdev` access) and **blacklists
   the kernel DVB-T driver** (`/etc/modprobe.d/blacklist-rtl-sdr.conf`), which otherwise claims
   the dongle as a TV tuner
4. optionally installs `hackrf` (`INSTALL_HACKRF=1`). We only run `hackrf_sweep`, which is
   receive-only.
5. creates a `rf-spectrum` system user in `plugdev`, copies the code to `/opt/rf-spectrum`,
   builds the venv in `/opt/rf-spectrum/.venv`, and creates `/var/lib/rf-spectrum`
6. creates `/etc/rf-spectrum.env` from `deploy/rf-spectrum.env.example` if it doesn't exist,
   installs the systemd unit, and copies `profiles.d/rf-spectrum.conf` into pi-profiles

It does **not** enable the service at boot, because pi-profiles starts it:

```bash
sudo pi-profiles rf-spectrum
```

```bash
journalctl -u rf-spectrum -f
```

Unplug and replug the SDR once after the first install so the blacklist and udev rules apply.

**Backend selection** (`SDR_BACKEND=auto`): an RTL-SDR is used for every preset it can tune,
up to about 1.7 GHz, through `pyrtlsdr` with a numpy FFT. If `pyrtlsdr` can't load,
`rtl_power` is used instead (slower, about one frame per second). A HackRF covers anything
above 1.7 GHz through `hackrf_sweep`. With both plugged in, both are used. With neither,
the service starts in **SIMULATED** mode, and the header says so in amber.

## Hardware buying guide

| Item | Why | Approx. |
|---|---|---|
| **RTL-SDR Blog V4** dongle (genuine, from rtl-sdr.com resellers) | Primary receiver: 24 MHz–1.766 GHz, TCXO, aluminium case. Covers PMR446, 433, 868, 915 and LTE 800. | £35–45 |
| RTL-SDR Blog **dipole antenna kit** | Telescopic dipoles you can cut or extend to each band. Comes with the dongle bundle. | bundle |
| Short **USB extension** (0.5–1 m) | Moves the dongle away from the Pi 5 and the USB 3 SSD, which radiate noise, especially near 2.4 GHz. | £5 |
| *Optional:* **HackRF One** (Great Scott Gadgets, genuine) | Receive-only use here, from 1 MHz to 6 GHz. Needed to *see* ELRS 2.4, Wi-Fi and the **5.8 GHz VTX**. | £300+ |
| *Optional:* 5.8 GHz FPV antenna (SMA) for the HackRF | Pick up the VTX at a realistic range | £10 |
| *Optional:* small directional antenna (Yagi or log-periodic) | Shows how an adversary would take a **bearing** on your emitters | £20–60 |

Notes:

- The **V3** works too (Debian driver or Blog driver). The **V4** needs the Blog driver, which
  the installer builds by default.
- Avoid unbranded "RTL2832U + R820T" TV sticks. They drift badly and have no TCXO.
- Beware HackRF clones: quality varies a lot.
- **The HackRF hardware can transmit. This project never asks it to.** Don't install
  PortaPack firmware or use `hackrf_transfer` on the demo kit.

### Antenna notes

- Set the dipole to a quarter wave per element: **868 MHz ≈ 8.6 cm**, 446 MHz ≈ 16.8 cm,
  433 MHz ≈ 17.3 cm. Mount it vertically, because most handheld and drone antennas are
  vertical.
- A spare **Meshtastic 868 MHz antenna** works well on the RTL-SDR (SMA). Check the connector,
  because some LoRa antennas are **RP-SMA** and won't mate properly.
- For 5.8 GHz, match the VTX antenna polarisation (RHCP/LHCP), or use a linear antenna, which
  hears both at −3 dB.
- **Don't overload the receiver.** Keep the SDR at least 2–3 m from your own transmitters
  during the demo. A 1 W ELRS module or a 600 mW VTX right next to the dongle saturates it
  and paints false signals everywhere. Never connect a transmitter to an SDR by cable. The
  HackRF's documented maximum RX input is only −5 dBm.
- Losing a few dB at range is part of the lesson. Walk the drone away and watch the signal
  stay well above the floor.

## Demo script (about 10 minutes)

Before you start, check that every radio is **off**: Meshtastic node, ELRS handset and
receiver, VTX/quad battery, and phones in flight mode. Select **868 MHz SRD / LoRa**.

1. **Quiet waterfall (1 min).** Show the noise floor (dashed blue) and the detection threshold
   (dashed red). Point out the allocation strip: g1 868.0–868.6 (1% duty), g3 869.4–869.65
   (10%, Meshtastic default). If anything civilian shows up, such as an alarm sensor or a smart
   meter, discuss it, then press **Set RF baseline**. That's a real EMCON practice: survey
   the environment before you switch on, so you know what's yours.
   *EMCON score: 100 SILENT.*
2. **Meshtastic node on (2 min).** Power the Heltec V3 and send a message. A 250 kHz block
   appears at 869.525 MHz with each packet, and again when another node relays it. The event
   log shows **NEW** emitter, then start/end with duration. Lesson: even "low duty cycle"
   traffic, such as position beacons and NodeInfo, gives away presence and pattern of life,
   and every relay re-transmits it.
3. **ELRS control link on (2 min).** Power the handset and the receiver (props off). The
   waterfall fills with dashes hopping across 863–870 MHz, and the rule *Frequency hopper
   863–870* fires with a **~90–100% duty cycle**. The score drops to LOUD. Lesson: an RC link
   transmits the whole time the handset is on, whether you're flying or not.
4. **FPV video on (2 min).** *Needs a HackRF, or use the simulator (FPV drone power-on / flight
   toggles for a DJI O3/O4 quad; see [Drone link signature](#drone-link-signature-counter-uas-segment)).* Switch to **5.8 GHz FPV
   video** and power the quad. A DJI O3/O4 air unit shows as a solid 10–40 MHz digital block
   (an analog VTX would be a narrower 3–7 MHz carrier). Lesson: the video link transmits the
   whole time the air unit is powered, even on the ground. Power it only when needed, at the
   lowest power setting.
5. **Phones (1 min, optional).** Switch to **Mobile uplink (LTE 800)** and take one phone out
   of flight mode. Uplink bursts appear even when nobody is using the phone.
6. **Debrief (2 min).** Press **Debrief**. Talk through the emitter list: what an adversary
   with the same £35 receiver and a directional antenna would know. Then switch everything off
   again and watch the score recover.

**Auto-scan** cycles through every band the attached receivers can tune, so one SDR can watch
the lot. Emitters in a band you aren't currently watching keep their last-known state until
that band is checked again.

With only an RTL-SDR, step 4 can't be shown live. Run that segment with the simulator, or buy
the HackRF.

In simulate mode, **Auto demo** runs the same story unattended (quiet → Meshtastic → ELRS →
433 MHz background → phone on LTE 800 → FPV drone power-on on 2.4 then 5.8 → drone flight → all off), and **Manual** gives you a toggle for each radio.
The demo drone is DJI O3/O4 **digital**, so Auto demo shows the 10/20 MHz digital block, never
an analog carrier. The analog VTX is a separate, optional **Manual** toggle.

## Drone link signature (counter-UAS segment)

The reference drone is a 5" FPV with **ExpressLRS 2.4 GHz** control and **DJI O3/O4** video at
5.8 GHz. rf-spectrum looks for its RF signature using energy and occupancy only, with nothing
decoded:

| Component | Rule | What it looks like |
|---|---|---|
| Control link (C2) | `hopper_2g4` | many short narrowband bursts hopping across roughly 2400–2480 MHz at a high packet rate |
| Digital video | `dji_video_5g8` | a persistent 10/20/40 MHz wideband block in 5725–5850 MHz |
| Analog video | `vtx_5g8` | a 3–7 MHz FM carrier anywhere in 5640–5950 MHz |
| 2.4 GHz video (weak evidence) | `wifi_2g4` at ≥90% duty | a near-continuous wideband block at 2.4 GHz, which could just be busy Wi-Fi |

- **One component** firing raises **"Possible drone C2/video link"** (amber banner).
- **C2 and video together** raise **"Drone C2 + video link (higher confidence)"** (red banner).
- Each escalation writes a `drone` event to the log. If `ALERT_MESH` / `ALERT_COT` are on (and
  `ALERT_DRONE=1`, the default), it also sends a `meshbus/tx` text and a CoT **unknown-air**
  (`a-u-A`) marker at the sensor position, with remarks like *"RF: drone DJI/digital video link 5.8 GHz
  + drone control link 2.4 GHz, bearing unknown"*. Both go through the same rate limits as the
  other alerts. No marker is sent unless `SENSOR_LAT`/`SENSOR_LON` are set.
- Anything produced from simulated data is labelled **SIMULATED**: in the UI, as a `SIMULATED:`
  prefix in the CoT remarks, and as `[SIM]` in the mesh text.
- Bluetooth also hops at 2.4 GHz and Wi-Fi also fills 2.4 GHz, which is why one component alone
  is only "possible". Press **Set RF baseline** before the drone powers up.

**Hardware reality.** An RTL-SDR tops out near 1.7 GHz, so it **cannot see 2.4 or 5.8 GHz**.
Detecting this drone live needs a **HackRF One**, or another receiver that reaches 6 GHz,
through the existing `hackrf_sweep` backend. One HackRF covers both bands with **Auto-scan**,
because each band keeps its last-known state while the other is being swept. Without one, run
this segment in simulate mode: the **FPV drone power-on** and **FPV drone flight** toggles
(also part of **Auto demo**) generate ELRS 2.4 hopping and an O4-style 10/20 MHz video block.

**Bearings.** This sensor reports *that* a link exists, not *where*. With a directional panel
or Yagi antenna on the HackRF, you can take a manual bearing: sweep the antenna slowly and note
where the video block peaks on the spectrum. Two bearings from different positions give a rough
fix.

**Teaching point.** Real counter-drone systems never rely on RF alone. They layer **RF**
(this tool), **acoustic** (acoustic-detector), **visual/EO-IR** (fpv-vision, op-sentry) and
**radar**. An autonomous drone with no video link, or one flying on a pre-planned route with
its radios off, is invisible to RF detection.

## How detection works

1. **Frames.** Each preset is a 1024-bin grid. The RTL-SDR backend hops 2.4 MS/s captures
   across the preset (keeping the flat middle 80% of each), runs a Welch-averaged FFT, and
   max-pools the result onto the grid. hackrf_sweep output is max-held over about 100 ms, so
   short bursts survive.
2. **Noise floor.** Per bin, it drops quickly and rises slowly. It's also clamped to the
   frame median plus 4 dB, so a VTX left on for ten minutes never becomes "noise".
3. **Hits.** Contiguous bins at least `THRESHOLD_DB` (default 10 dB) over the floor. Bandwidth
   is measured as the bins within 6 dB of the peak.
4. **Rules** (`rfspectrum/rules.py`, or your own JSON via `RULES_FILE`):
   - *fixed* rules match band, bandwidth and SNR. A run held for `min_duration_s` opens an
     event, and a gap longer than `HOLD_S` closes it.
   - *hopping* rules need at least `min_hops` distinct narrow channels within `hop_window_s`.
   - Strong, sustained hits that no rule claims become *Unclassified* emitters.
5. **EMCON score**: `100 − 15 × emitters − 50 × worst duty cycle`, over the last
   `EMCON_WINDOW_S` (60 s), excluding baselined emitters. Grades: ≥90 SILENT, ≥70 LOW,
   ≥40 ELEVATED, otherwise LOUD.

The power values are **uncalibrated** dB, and nothing here estimates range or bearing.
The point is "it's visible", not "it's at grid X".

## Alerts (off by default)

Set these in `/etc/rf-spectrum.env`:

| Setting | Effect |
|---|---|
| `ALERT_MESH=1` | Publishes `{"text","to","channel","source":"rf-spectrum"}` to `meshbus/tx` on the local MQTT broker (`MQTT_*`). The blufor-tracker bridge sends it over LoRa. We never open the radio's serial port. At most one text per `ALERT_MIN_INTERVAL_S` (default 60, never below 30). |
| `ALERT_COT=1` | Sends a CoT `a-u-G` marker per new emitter to `TAK_HOST:TAK_PORT` (default 127.0.0.1:8087) over one long-lived connection (see *TAK registration* below). The marker sits at `SENSOR_LAT/SENSOR_LON`, the sensor's own position, because energy detection gives no bearing. At most one message per 5 s per marker (the newest is kept). **If `SENSOR_LAT`/`SENSOR_LON` are unset (or 0,0), no CoT is sent** and the log says so once; use the same OP position as the other tools. |
| `WATCH_BANDS=863-870` | Only NEW emitters in these MHz ranges alert. Baselined emitters never alert. |

**Why off by default:** a mesh alert is itself an 868 MHz LoRa transmission. If you're
watching 868 MHz, the alert shows up on the waterfall as a new burst at 869.525 MHz. The
blufor-tracker node's own traffic (position beacons, telemetry) triggers the Meshtastic rule
too. So with mesh alerts on, the tool spends its time reporting your own mesh. That's a nice
EMCON point to make once, and a reason not to leave it on. If you do enable it, baseline your
own node first, or watch a different band.

### TAK registration (OpenTAKServer)

OpenTAKServer drops CoT from any sender that isn't a registered device, so rf-spectrum keeps
**one** TCP connection to `TAK_HOST:TAK_PORT` (reconnecting with backoff, 1 s to 60 s) and
registers on it before sending anything:

- 3 s after each (re)connect it sends a registration SA, uid `isrkit-rf-spectrum-<ISR_HOST>`,
  callsign `RF <ISR_HOST>`, type `a-f-G-E-S`, group Cyan. The uid and callsign are stable, so a
  reconnect is accepted (OTS refuses a callsign that another uid already holds).
- The SA is re-sent every 60 s with a 120 s stale, at `SENSOR_LAT/SENSOR_LON`. With no position
  set, the SA carries a 0,0 point with a 9999999 error and stale = start, so it is never drawn.
- Markers (new emitters `a-u-G`, drone link `a-u-A`) go on the same connection after
  registration, with their own uids. They are built so OTS also **stores** them (and shows them
  on its web map): no `<takv>`, a `<contact>` with a callsign but no endpoint, a real position
  (never 0,0), and a `<link relation="p-p">` only to our own registration uid. Messages queued while TAK is down are sent after the next registration.

### Launcher heartbeat

Every `HEARTBEAT_S` seconds (default 10), rf-spectrum publishes a **retained** JSON message to
`isrkit/heartbeat/rf-spectrum` on the MQTT broker:
`{"tool":"rf-spectrum","host":"isr-pi","url":"http://isr-pi.local:8140","mode":"simulate|cpu","status":"ok|degraded|error","ts":…}`.
`status` is `degraded` when the selected band has no receiver or the SDR errored. `host` is `ISR_HOST` if set, else the lowercased short hostname. On a clean
shutdown it publishes `"status":"stopped"`. The heartbeat and mesh alerts share one MQTT
connection that publishes only while connected; if the broker is down it logs one line, retries
with backoff (1 s to 60 s) and logs again when it reconnects. The MQTT and TAK addresses come from `MQTT_HOST/PORT/USER/PASS` and
`TAK_HOST/TAK_PORT`, or from the `--mqtt-host`, `--mqtt-port`, `--mqtt-user`, `--mqtt-pass`,
`--tak-host` and `--tak-port` flags. They default to localhost, and the web UI binds to
`0.0.0.0`.

## Optional cloud debrief (Gemini)

The debrief works fully offline with rule-based text. If `GEMINI_API_KEY` is set, the
**Debrief** button sends the *emitter summary* (rule names, frequencies, bandwidths, duty
cycles, never raw samples) to Google's Gemini API (`GEMINI_MODEL`, default `gemini-3.8-flash`)
for a more conversational write-up. The header shows which mode is active
(`AI: offline` / `AI: Gemini cloud`). If the call fails, it falls back to the offline text.

> **Cloud mode sends data to Google.** Use it only for demo sessions, never with real
> operational data.

## Configuration

Everything is in `deploy/rf-spectrum.env.example` (copied to `/etc/rf-spectrum.env`). The
main settings:

| Variable | Default | Meaning |
|---|---|---|
| `SDR_BACKEND` | `auto` | `auto`, `sim`, `rtlsdr`, `rtl_power` or `hackrf` |
| `RFSPEC_PRESET` | `868` | starting band (`pmr446`, `433`, `868`, `915`, `lte800`, `2g4`, `5g8`) |
| `RTL_GAIN` / `RTL_PPM` | `auto` / `0` | RTL-SDR gain (dB or auto) and frequency correction |
| `HACKRF_LNA` / `HACKRF_VGA` / `HACKRF_AMP` | `32` / `20` / `0` | HackRF receive gains |
| `THRESHOLD_DB` | `10` | detection threshold over the noise floor |
| `HOLD_S` | `1.0` | gap that ends an event |
| `EMCON_WINDOW_S` | `60` | duty-cycle and score window |
| `SCAN_DWELL_S` | `10` | seconds per band in auto-scan |
| `SIM_SCENARIO` / `SIM_BACKGROUND` | `auto` / `1` | simulator story and civilian emitters |

## Legal note (UK)

*Not legal advice.*

- **Receiving is fine.** In the UK you may operate a receiver like this one without a licence.
- **Don't decode or disclose others' transmissions.** Under the **Wireless Telegraphy Act
  2006** (s.48), it's an offence to use wireless telegraphy apparatus with intent to obtain
  information about the contents of a message you aren't authorised to receive, or to disclose
  that information. This tool deliberately measures **energy only**. Don't bolt a decoder onto
  it, and don't record or share the contents of anyone else's traffic.
- **Never transmit with this kit.** Transmitting needs a licence or a licence exemption
  (Ofcom IR 2030 for SRDs, with power and duty limits). Jamming is illegal. US-region "915"
  ELRS or Meshtastic settings transmit outside UK SRD allocations, so set the **EU 868**
  region and domain.
- Organisations should also follow their own spectrum management and EMCON policies.

## Layout

```
rfspectrum/
  bands.py       presets + UK/EU allocation labels
  rules.py       signal-of-interest rules
  engine.py      noise floor, hits, events, emitters, EMCON score
  simulator.py   synthetic RF environment for --simulate
  sources/       rtlsdr (pyrtlsdr), rtl_power, hackrf_sweep, simulated, auto-detect
  alerts/        tak_client.py (registered TAK connection), cot.py (markers), mesh.py (meshbus/tx), mqttbus.py, heartbeat.py
  debrief.py     offline + optional Gemini debrief
  runtime.py     source thread -> engine -> alerts / WebSocket
  web/           FastAPI app + static index.html
deploy/          install.sh, start.sh, rf-spectrum.service, rf-spectrum.env.example
profiles.d/      rf-spectrum.conf for pi-profiles
tests/           pytest (runs on a Mac with no hardware)
```

## Licence

MIT. See [LICENSE](LICENSE).
