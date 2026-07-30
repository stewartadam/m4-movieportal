set positional-arguments := true

# List the available project commands.
default:
    @just --list

# Run the host-side test suite.
test:
    uv run python -m unittest discover -s tests

# Compile the host/device sources and run their tests.
check:
    uv run python -m compileall -q boot.py code.py compare.py movieportal.py mpv_ipc.py protocol.py settings.py stream.py tests
    uv run python -m unittest discover -s tests

# Install the CircuitPython runtime on a mounted board.
install-device device="/Volumes/CIRCUITPY":
    cp boot.py code.py movieportal.py protocol.py settings.py {{device}}/
    sync

# Stream a test pattern or movie. Pass normal stream.py arguments after `--`.
stream *args:
    uv run --script stream.py {{args}}

# Stream a movie while preserving spaces in its filename.
play source *args:
    uv run --script stream.py play {{quote(source)}} {{args}}

# Follow the open movie and playback controls in IINA.
iina *args:
    uv run --script stream.py iina --socket /tmp/m4-movieportal-mpv.sock {{args}}

# Render source, scaled-source, and post-processed columns at each timestamp.
compare source output *args:
    uv run compare.py {{quote(source)}} {{quote(output)}} {{args}}

# Attach to the CircuitPython serial console.
serial:
    discotool repl

# Attach to the dedicated binary data port.
data:
    discotool data
