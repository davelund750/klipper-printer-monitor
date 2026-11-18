# Casting to Chromecasts and Nest Hubs

**Built** (2026-10-20): `server/casting.py`, the dashboard's cast button, and
go2rtc in `install.sh`; see the README's "Casting to TVs and displays". These
are the notes from the prototype that led to it (2026-10-19/20), kept for the
reasons behind the choices.

What changed on the way from prototype to build:

- go2rtc listens only on the Pi (`127.0.0.1:1984`): its API can add `exec:`
  sources that run commands, so it must not be on the network. The
  dashboard's server relays the one stream to cast screens at
  `/api/camera-stream` (`casting.relay_stream()`); the prototype's
  `origin: "*"` on the network is gone.
- The server registers the camera with go2rtc itself (`PUT /api/streams`),
  so it follows the printer's address, and re-registers if go2rtc restarts.
- Cast screens are opened at `/?cast`: split view, view-only, the MSE player.
- The player's re-timed frames are back to back (each starts where the last
  ends). Before, a frame could start a few milliseconds after the last one
  ended, and the player stalls at such gaps.
- The hitch every 20–30 s turned out not to be the dashboard (switching off
  all its redrawing changed nothing) or a second viewer: the original
  prototype player stalled the same way on the same day, and on another run
  the TV played 100 s without one. Frames lost between the camera and go2rtc
  stall the TV's decoder until the next full frame. The player now jumps
  after 1 s stuck (not 6 s behind) and then waits 5 s for the decoder.
- pychromecast keeps a discovery running for the devices it found: they
  rely on it to be reached. Stopping it right after a search made casting
  time out.

## Result

It works, including the camera: on Office TV (Chromecast with Google TV) the
K2's camera played smoothly at 12–15 fps with about 1.5 seconds of delay,
1 dropped frame in 2 minutes.

Then the whole dashboard, with this camera in place of its WebRTC one,
was cast to Office TV (a throwaway copy, 2026-10-19): the layout and live
data looked right, and judged good enough to build. One thing to fix in the
build: inside the dashboard the camera hitches about every 20–25 seconds
(playback stalls for a few seconds, the buffer grows to ~5–6 s, then it
jumps back), which the bare player never did. Something else on the page,
probably its 2-second redraws on the Chromecast's weak processor, gets in
the way. The camera code from that mock-up became `startMse()` /
`stopMse()` in `web/app.js`, used in `loadCamera` for a camera of type
`"mse"`.

## How

```
K2 camera (WebRTC, H.264) ──> go2rtc on the Pi ──> MSE stream (same H.264, re-wrapped)
                                                     └──> player page on the Cast device
Pi server ──(pychromecast)──> Cast device: "open this page" (DashCast receiver)
```

1. **Casting the page.** The Pi's server finds Cast devices on the network
   and tells one to open a page, with
   [pychromecast](https://github.com/home-assistant-libs/pychromecast) and
   its DashCast controller, loading the page directly (`force=True`), not in
   DashCast's frame. Plain `http://` on the LAN works; no HTTPS needed.
   - A page loaded directly replaces the DashCast receiver, which then can't
     take another load: quit the running app first, then load
     (as `server/casting.py` does).
   - Be patient with a device that's been idle (e.g. overnight): it's slow to
     start the receiver, and an address sent too early is silently dropped,
     leaving DashCast on "Waiting for address". So start the receiver, wait
     until the device reports it running (plus a couple of seconds), then
     send the address, and confirm the device actually requests the page,
     retrying if not. `cast.py` does this and took on the first try.
   - The dashboard itself loads and updates fine on the Chromecast.
2. **Why the camera needs go2rtc.** The Chromecast's browser (Chrome 92,
   CrKey 1.56) only takes VP8/VP9 over WebRTC, and the K2 only sends H.264,
   so the dashboard's own WebRTC camera fails there ("Failed to parse
   SessionDescription"). The Chromecast does play H.264 through MSE, so
   [go2rtc](https://github.com/AlexxIT/go2rtc) (v1.9.14, single binary)
   receives the K2's WebRTC stream and re-wraps it as fMP4 for MSE, with no
   re-encoding.
   - Source: `webrtc:http://<printer>:8000/call/webrtc_local#format=creality`
   - The player connects to `ws://<pi>:1984/api/ws?src=k2`, sends
     `{"type": "mse", "value": "<codecs it supports>"}`, gets back
     `{"type": "mse", "value": "video/mp4; codecs=..."}`, then binary fMP4
     (one frame per fragment). go2rtc needs `api: origin: "*"` for a page
     served from elsewhere to connect.
3. **The player (the important part).** go2rtc's own player was choppy, and
   so was a plain one: the K2 timestamps frames coarsely (bursts of frames
   1 ms apart, then a 0.5–1 s jump) even though they arrive evenly (about
   every 66 ms). A normal video player trusts the timestamps and drops most
   frames. The prototype's player (now `startMse()` in `web/app.js`) fixes it:
   - **Re-times every frame** by arrival: steady spacing at the average
     arrival interval, eased 2% toward actual arrival so it never drifts.
     It rewrites each fragment's `tfdt` (start, 90 kHz) and `tfhd` default
     sample duration.
   - **Buffers 1.2 s before playing**, then plays at normal speed, catching
     up by playing 3% faster while more than 0.2 s over target.
   - **Never jumps** except in an emergency (6+ s behind): after a jump the
     Chromecast freezes until the camera's next keyframe, which is what
     made earlier versions hitch. Same for changing speed by 5% or more:
     avoid.
   - Trims the buffer rarely (past 60 s, down to 30 s).

## Costs on a Pi 3B

- go2rtc on a Mac while relaying: 2.3% of one core, 28 MB. Expect very
  roughly 15–25% of one Pi 3B core: it only re-wraps, never decodes.
- About 1.9 Mbit/s per Cast screen watching.
- Builds: `go2rtc_linux_arm` (32-bit Raspberry Pi OS) or `go2rtc_linux_arm64`.
- New dependencies: go2rtc (a binary, as a systemd service) and
  pychromecast (Python, with zeroconf), so the server is no longer
  standard-library only.

## Side benefits of go2rtc

- The kiosk's own camera could watch go2rtc too, so only the Pi connects
  to the printer's camera (the K2 likely limits viewers), and the Google
  STUN server is no longer needed.
- Reconnecting to the printer's camera happens in one place, on the Pi.

## Still to decide or check

- The camera hitch inside the dashboard (above).
- A cast page can stop on the TV without the Cast session ending: once,
  Office TV still reported DashCast "Application ready" but the page had
  stopped polling (no requests at all), so it never noticed the printer
  coming back online. A refresh (re-cast) fixed it. The build should
  watch each cast screen's status polls and re-cast any that go quiet for
  a minute, alongside the periodic re-cast for Nest Hubs.
- The Cast button and device picker on the kiosk (multi-select; a Stop per
  device), and re-casting every few minutes so Nest Hubs don't drop back to
  the photo frame.
- What a cast screen shows: the dashboard with this player as its camera,
  view-only (no touch on a Hub). While a job prints, the left side splits:
  camera on top, the layer preview below (no Camera/Preview switch, since
  nobody can tap it). Tried in the mock-up (2026-10-20) by opening the page
  with `?cast`; the real build should open its cast screens that way. Cast screens must be treated as viewers
  (`canChange: false`): no gear, no light button, and no "Tap here to
  change the address" on the offline banner. They are naturally when they
  ask the Pi from their own address; the mock-up had to force it, since it
  relayed through the Mac.
- Not yet tried on a Nest Hub.
- Measure on the Pi 3B itself, next to Chromium.
