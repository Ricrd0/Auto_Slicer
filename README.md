# Auto Slicer

Batch-slice every STL and 3MF in an input folder, once per printer, and write gcode under a shared folder name on each machine.

Settings has a slicing engine choice. Cura uses CuraEngine 5.13 and the Cura machine profiles. Orca uses OrcaSlicer 2.4.2 and the machine profiles under `%APPDATA%\OrcaSlicer`. The same layer height, ironing, seam, infill, combing, adhesion, and support settings are applied to whichever engine is selected.

The web app calls the slicer directly. No language model is involved.

## Run

On Windows the app reads `%APPDATA%\cura\5.13` when `data/cura-config` is empty, and Docker mounts that folder read-only. To use a copy instead, put the version folder (the one that contains `machine_instances`) in `data/cura-config`. Orca profiles are read from `%APPDATA%\OrcaSlicer`, which Docker mounts at `/host-orca`.

The Models tab has Browse buttons for the input folder and the output directory. Docker can see the drives mounted in `docker-compose.yml`. The output folder name in Settings is the shared subfolder written under each printer.

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
