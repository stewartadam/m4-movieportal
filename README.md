# M4 MoviePortal

A CircuitPython project for playing a movie on an Adafruit MatrixPortal M4 and
an attached RGB LED matrix.

## Status

This repository is a scaffold. Movie loading, decoding, frame timing, and
matrix rendering are intentionally not implemented yet.

The first implementation milestone will answer these questions:

- What on-device movie format fits the MatrixPortal M4's memory and storage
  constraints?
- Should frames be streamed from storage or loaded individually?
- What panel size, color depth, and frame rate will be supported?
- What host-side conversion tool is needed to prepare source video?

## Project layout

- `code.py` is the CircuitPython entrypoint.
- `movieportal.py` will contain the application runtime.
- `tests/` contains host-side scaffold checks.
- `Justfile` contains the development commands.

## Development

Install [uv](https://docs.astral.sh/uv/) and
[just](https://just.systems/), then run:

```sh
just check
```

To open the CircuitPython serial console with
[discotool](https://github.com/Neradoc/discotool):

```sh
just serial
```

Device installation instructions will be added with the playback
implementation.
