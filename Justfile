default:
    @just --list

venv := ".venv"

# Build a self-contained ./rig executable you can run anywhere
build:
    rm -rf .stage
    mkdir -p .stage
    cp -r src/rig .stage/rig
    cp -r $({{venv}}/bin/python -c "import argcomplete, pathlib; print(pathlib.Path(argcomplete.__file__).parent)") .stage/argcomplete
    printf 'from rig.cli import main\nraise SystemExit(main())\n' > .stage/__main__.py
    {{venv}}/bin/python -m zipapp .stage -o ./rig -p '/usr/bin/env python3'
    rm -rf .stage
    chmod +x ./rig

# Build a wheel + sdist into dist/ (for publishing)
dist:
    {{venv}}/bin/python -m build

# Run the CLI, e.g. `just run --version` or `just run -- plan -R ~/dots`
run *args="--help": build
    ./rig {{args}}

# Run the test suite
test:
    {{venv}}/bin/pytest -q

# Lint and check formatting
lint:
    {{venv}}/bin/ruff check .
    {{venv}}/bin/ruff format --check .

# (Re)install the package + dev deps into the venv
getdeps:
    {{venv}}/bin/pip install -e ".[dev]"

# installs rig onto the users system into /usr/bin/rig
install:
    just build
    sudo cp ./rig /usr/bin/rig
