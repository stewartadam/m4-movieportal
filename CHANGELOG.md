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
- Scale and linearize gamma-encoded SDR video at 16-bit working precision for
  the HUB75 panel.
- Use hue-preserving five-bit quantization and a fine-grained black cutoff to
  avoid spurious primary-color dots without erasing muted colors.
- Pack RGB565 deterministically on the host to prevent FFmpeg's final
  pixel-format conversion from reintroducing colored ordered dithering.
- Keep the decoder handoff at RGB48 and use frame-aware RGB565 shadow colors,
  retaining real warm tones without adding color sparkle to neutral scenes.
- Render comparison cells through the live frame pipeline and preview the exact
  transmitted RGB565 values with inverse LED gamma.
- Model Protomatter's five-bit scan output: emit even RGB565 green codes and
  discard the unused green bit in comparison previews.
- Use the tagged BT.709 SDR transfer by default instead of a pure 2.2 power
  approximation, preserving channel balance in deep shadows.
- Replace the shared-luminance shadow lift with local 3x3 chroma cleanup so
  neutral noise is suppressed without painting colored scenes gray or orange.
- Strengthen local chroma cleanup adaptively for lit neutral scenes while
  preserving sparse color in nearly black frames.
- Bias only red-dominant shadow quantization away from marginal red codes,
  retaining BT.709 contrast and exposing the correction as a tuning control.
- Make gamma correction and the custom five-bit quantizer independently
  switchable, with a scaled-source comparison mode that bypasses both.
- Add a comparison PNG command that lays out source, scaled-source, and
  post-processed frames as columns across requested timestamp rows.
- Display IINA's current playhead frame when starting or becoming paused.
