# Auto Slicer

Batch-slice every STL and 3MF in an input folder with CuraEngine, once per printer, and write gcode under a shared folder name on each machine.

The web app calls CuraEngine directly. No language model is involved.

## Run

Put models in `data/input`. On Windows the app reads `%APPDATA%\cura\5.13` when `data/cura-config` is empty, and Docker mounts that folder read-only. To use a copy instead, put the version folder (the one that contains `machine_instances`) in `data/cura-config`.

```bash
docker compose build
docker compose up
```

Open `http://localhost:8080`.

`CURA_VERSION` is a build arg. Use the same Cura 5 release that wrote the configuration folder. A mismatch is shown on the printer list.

Printer rows can be downloaded as a zip and uploaded on another machine to replace that printer or add it when it is missing.

## Local tests

```bash
python -m pip install -e ".[dev]"
python -m pytest
```
