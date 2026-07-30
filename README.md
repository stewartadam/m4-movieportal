# M4 MoviePortal

A CircuitPython project for streaming a movie from a host computer to an
Adafruit MatrixPortal M4 and attached 64x32 RGB LED matrix.

## Summary

The host decodes MP4/MKV input with FFmpeg, plays its audio locally, scales the
video to 64x32, converts it to RGB565, and streams frames over a dedicated USB
CDC data port. The MatrixPortal only buffers, schedules, and displays prepared
frames. This keeps the working set bounded and supports movies of arbitrary
length despite the board's 192 KB SRAM and 2 MB flash.

The USB framebuffer is RGB565, while the HUB75 scan driver currently uses
5 bits per color channel for 32,768 simultaneously displayed colors. Host frames
are rotated according to `DISPLAY_ROTATION` in `settings.py` before
transmission; supported values are `0` and `180`.

This is an initial USB prototype. Direct-mode pause/seek controls, HDR tone
mapping, and Wi-Fi transport are not implemented yet; IINA mode provides
interactive pause and seek controls.

## Hardware setup

The hardware target is:

1. [Adafruit MatrixPortal M4](https://www.adafruit.com/product/4745) with
   CircuitPython 10.2.1;
2. A 64x32 RGB matrix such as
   [the ones available from Adafruit](https://www.adafruit.com/product/2278);
3. An optional acrylic diffuser such as
   [the ones available from Adafruit](https://www.adafruit.com/product/4749);
4. A host computer connected to the MatrixPortal with a data-capable USB cable.

## Software setup

MoviePortal uses only modules built into CircuitPython, so it does not require
any libraries from the CircuitPython bundle.

1. Install the CircuitPython firmware (UF2) as detailed in the
   [Adafruit MatrixPortal guide](https://learn.adafruit.com/adafruit-matrixportal-m4/prep-the-matrixportal).

2. Install [uv](https://docs.astral.sh/uv/), which runs the host streamer and
   installs its Python dependencies automatically.

3. Install [`discotool`](https://github.com/Neradoc/discotool) to assist with
   device discovery and debugging:

   ```shell
   uv tool install discotool
   ```

4. Install [`just`](https://just.systems/) to run the project commands.

5. Install [FFmpeg](https://ffmpeg.org/download.html), including both the
   `ffmpeg` and `ffplay` commands. FFmpeg decodes the video, while ffplay plays
   its audio on the host. IINA users still need FFmpeg for video frames but do
   not need ffplay.

6. Connect the MatrixPortal and edit `DISPLAY_ROTATION` in `settings.py` to
   match the physical panel orientation. Supported values are `0` and `180`.

7. Install the device code:

   ```shell
   just install-device
   ```

   The default device path is `/Volumes/CIRCUITPY`. Pass a different mount path
   when needed:

   ```shell
   just install-device /path/to/CIRCUITPY
   ```

8. Press reset on the MatrixPortal. `boot.py` changes the USB descriptors, so
   the board should reconnect with two serial devices: the normal CircuitPython
   console and a dedicated binary data port.

9. Stream a ten-second generated test pattern:

   ```shell
   just stream -- test --seconds 10
   ```

## Project layout

- `code.py` is the CircuitPython entrypoint.
- `boot.py` enables the dedicated binary USB CDC port.
- `movieportal.py` contains the device receiver and player.
- `protocol.py` defines the protocol shared by host and device.
- `settings.py` contains user-adjustable panel configuration.
- `stream.py` is the host-side synthetic/FFmpeg streamer.
- `tests/` contains host-side unit tests.
- `Justfile` contains the development commands.

## Development

Install [uv](https://docs.astral.sh/uv/) and
[just](https://just.systems/), then run:

```sh
just check
```

## Playing movies

Stream a movie with computer audio and automatic hardware decoding when FFmpeg
and the platform support its codec:

```sh
just play "/path/to/movie.mkv"
```

Useful options include `--fit crop`, `--duration 5`, `--hwaccel none`,
`--start 300`, `--no-audio`, `--volume 75`, `--audio-delay-ms 200`, and
`--led-gamma 2.2`.
The audio delay defaults to the same 250 ms preroll used by the device; adjust
it if the computer's audio device adds noticeable latency.

MoviePortal applies a 2.2 gamma exponent by default before RGB565 quantization
so gamma-encoded SDR video drives the panel's linear LED output correctly.
Adjust `--led-gamma` for a particular panel, or use `--led-gamma 1` to disable
the correction. `--no-led-gamma` is the equivalent explicit on/off control.
Area scaling and gamma conversion use 16-bit working precision
so small, dark details are not discarded before quantization. MoviePortal then
uses stable five-bit output levels. In shadows, it quantizes luminance once
and adds reduced chroma residuals, producing muted colors without independent
red/green/blue sparkle. The host packs RGB565 itself so FFmpeg cannot introduce
colored ordered dithering while reducing the final channel depths.
`--led-dark-floor LEVEL` adjusts a fine-grained
luminance cutoff within the first five-bit output step. The default 0 is the
weakest cutoff; levels 1, 2, and above progressively suppress near-black noise
without discarding every first-step shadow pixel at once.
`--no-led-dark-floor` selects level 0.

Each non-scaling stage can be compared independently: use `--no-led-gamma` to
bypass gamma correction and `--no-rgb5-quantizer` to bypass the custom
five-bit/shadow quantizer. `--scaled-source` disables both in one step, leaving
only frame-rate conversion, scaling/cropping or padding, display rotation, and
the required RGB565 packing.

The global `--port /dev/cu.usbmodem...` and `--fps 15` options must precede
`play` when invoking `stream.py` directly. For example:

```sh
uv run --script stream.py --fps 15 play "/path/to/movie.mkv"
```

The `--hwaccel auto` option is the default and falls back to software decoding
when hardware support is unavailable.

## IINA playback controls

On macOS, IINA can provide the movie window, timeline, audio, pause, and seek
controls while MoviePortal follows its current playback position.

In IINA, open **Settings > Advanced**, enable advanced settings, and add this
entry under **Additional mpv options**:

| Name | Value |
| --- | --- |
| `input-ipc-server` | `/tmp/m4-movieportal-mpv.sock` |

Restart IINA so the option takes effect, open a movie, and run:

```sh
just iina
```

MoviePortal briefly pauses IINA while it seeks FFmpeg and prebuffers the panel,
then resumes both against the same preroll. Pausing IINA stops the USB session
and presents a one-frame preview at the current playhead position; seeking
while paused refreshes that preview at the new position.

Use `just stream -- --port /dev/cu.usbmodem... iina` to select the data port
explicitly. The `iina` command also accepts `--fit crop`, `--hwaccel none`,
`--led-gamma VALUE`, `--no-led-gamma`, `--rgb5-quantizer`,
`--no-rgb5-quantizer`, `--led-dark-floor LEVEL`, `--no-led-dark-floor`,
`--scaled-source`, and `--socket PATH`. For example, compare cutoff levels or
the scaled source without having to repeat the default IINA socket:

```sh
just iina --led-dark-floor 0
just iina --led-dark-floor 2
just iina --scaled-source
```

To open the CircuitPython console or data port with
[discotool](https://github.com/Neradoc/discotool):

```sh
just serial
just data
```
