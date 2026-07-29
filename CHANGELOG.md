# Changelog

## Unreleased

- Replace the M4 Marquee baseline with an M4 MoviePortal project scaffold.
- Add the first USB RGB565 streaming prototype with bounded device buffering.
- Add synthetic and FFmpeg host frame producers with automatic hardware decode.
- Play source audio on the host with configurable video synchronization delay.
- Add direct start-time seeking and optional IINA timeline, pause, and seek
  integration through mpv JSON IPC.
- Present a one-frame MatrixPortal preview when seeking while IINA is paused.
- Increase HUB75 output depth to 5 bits per channel (32,768 displayed colors).
- Add a `DISPLAY_ROTATION` setting for host-rendered movie and test frames.
