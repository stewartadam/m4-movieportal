set positional-arguments := true

# List the available project commands.
default:
    @just --list

# Run the host-side test suite.
test:
    uv run python -m unittest discover -s tests

# Compile the scaffold and run its tests.
check:
    uv run python -m compileall -q code.py movieportal.py tests
    uv run python -m unittest discover -s tests

# Attach to the CircuitPython serial console.
serial:
    discotool repl
