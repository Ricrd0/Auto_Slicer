import json
from pathlib import Path

for path in Path("/opt/orca-extract/resources/profiles").rglob("*.json"):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    if data.get("type") != "machine":
        continue
    nozzles = data.get("nozzle_diameter")
    if not (isinstance(nozzles, list) and len(nozzles) == 2):
        continue
    if str(data.get("instantiation")).lower() not in {"true", "1"}:
        continue
    print(path)
    for key in [
        "name",
        "gcode_flavor",
        "printable_area",
        "printable_height",
        "extruder_offset",
        "retraction_length",
        "retraction_speed",
        "printer_structure",
    ]:
        if key in data:
            print(" ", key, "=", data[key])
    start = str(data.get("machine_start_gcode") or "")
    print("  start=", start[:200].replace("\n", " | "))
    print("---")
    if "idex" in str(path).lower() or "j1" in str(path).lower():
        break
