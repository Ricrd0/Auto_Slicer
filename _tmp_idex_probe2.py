import json
from pathlib import Path

paths = list(Path("/opt/orca-extract/resources/profiles/Snapmaker/machine").glob("*.json"))
paths += list(Path("/opt/orca-extract/resources/profiles/Ratrig/machine").glob("*IDEX*0.4*.json"))
for path in paths:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    if data.get("type") != "machine":
        continue
    nozzles = data.get("nozzle_diameter")
    if not isinstance(nozzles, list):
        continue
    print(path.name, "nozzles", nozzles, "inst", data.get("instantiation"), "inherits", data.get("inherits"))
    if len(nozzles) >= 2 or "idex" in path.name.lower() or "j1" in path.name.lower():
        for key in [
            "extruder_offset",
            "retraction_length",
            "retraction_speed",
            "deretraction_speed",
            "z_hop",
            "printable_area",
            "printable_height",
            "gcode_flavor",
            "single_extruder_multi_material",
        ]:
            if key in data:
                print(" ", key, "=", data[key])
        print("---")
