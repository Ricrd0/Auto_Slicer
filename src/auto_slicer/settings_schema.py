from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from auto_slicer.paths import safe_segment

AdhesionType = Literal["none", "skirt", "brim", "raft"]
SeamType = Literal["sharpest_corner", "shortest", "random", "user_specified"]
SeamPosition = Literal[
    "backleft",
    "back",
    "backright",
    "right",
    "frontright",
    "front",
    "frontleft",
    "left",
]
CombingMode = Literal["off", "all", "noskin", "infill"]
SupportType = Literal["buildplate", "everywhere"]
SupportStructure = Literal["normal", "tree"]

ADHESION_TYPES: tuple[AdhesionType, ...] = ("none", "skirt", "brim", "raft")
SEAM_TYPES: tuple[SeamType, ...] = (
    "sharpest_corner",
    "shortest",
    "random",
    "user_specified",
)
SEAM_POSITIONS: tuple[SeamPosition, ...] = (
    "backleft",
    "back",
    "backright",
    "right",
    "frontright",
    "front",
    "frontleft",
    "left",
)
COMBING_MODES: tuple[CombingMode, ...] = ("off", "all", "noskin", "infill")
SUPPORT_TYPES: tuple[SupportType, ...] = ("buildplate", "everywhere")
SUPPORT_STRUCTURES: tuple[SupportStructure, ...] = ("normal", "tree")
INFILL_PATTERNS: tuple[str, ...] = (
    "lightning",
    "gyroid",
    "cubic",
    "grid",
    "lines",
    "triangles",
    "trihexagon",
    "zigzag",
    "concentric",
    "cross",
    "cross_3d",
)


def _as_bool(value: str) -> str:
    return "true" if value else "false"


def _enum(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}")
    return value


@dataclass
class SliceSettings:
    output_folder_name: str = ""
    rotation_x: float = 0.0
    rotation_y: float = 0.0
    rotation_z: float = 0.0
    layer_height: float = 0.2
    ironing_enabled: bool = True
    ironing_only_highest_layer: bool = True
    z_seam_type: SeamType = "user_specified"
    z_seam_position: SeamPosition = "backright"
    infill_pattern: str = "lightning"
    infill_sparse_density: float = 5.0
    retraction_combing: CombingMode = "noskin"
    adhesion_type: AdhesionType = "brim"
    brim_width: float = 8.0
    skirt_line_count: int = 3
    raft_margin: float = 15.0
    support_enable: bool = False
    support_type: SupportType = "buildplate"
    support_structure: SupportStructure = "normal"
    support_angle: float = 50.0
    support_infill_rate: float = 15.0

    def validate(self) -> None:
        if self.output_folder_name:
            safe_segment(self.output_folder_name)
        if self.layer_height <= 0:
            raise ValueError("layer_height must be positive")
        if not 0 <= self.infill_sparse_density <= 100:
            raise ValueError("infill_sparse_density must be between 0 and 100")
        _enum(self.z_seam_type, SEAM_TYPES, "z_seam_type")
        _enum(self.z_seam_position, SEAM_POSITIONS, "z_seam_position")
        _enum(self.retraction_combing, COMBING_MODES, "retraction_combing")
        _enum(self.adhesion_type, ADHESION_TYPES, "adhesion_type")
        _enum(self.support_type, SUPPORT_TYPES, "support_type")
        _enum(self.support_structure, SUPPORT_STRUCTURES, "support_structure")
        if self.infill_pattern not in INFILL_PATTERNS:
            raise ValueError(f"infill_pattern must be one of {', '.join(INFILL_PATTERNS)}")
        if self.brim_width < 0 or self.skirt_line_count < 0 or self.raft_margin < 0:
            raise ValueError("adhesion sizes cannot be negative")
        if self.support_infill_rate < 0 or self.support_angle < 0:
            raise ValueError("support settings cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SliceSettings:
        known = {field: data[field] for field in SliceSettings.__dataclass_fields__ if field in data}
        settings = SliceSettings(**known)
        settings.validate()
        return settings


def pack_gap(settings: SliceSettings) -> float:
    """A few millimetres, and at least the brim width when a brim is used."""
    if settings.adhesion_type == "brim":
        return max(5.0, settings.brim_width)
    return 5.0


def cura_setting_overrides(settings: SliceSettings) -> list[tuple[str, str]]:
    """Shared settings applied after the printer stack so they win."""
    settings.validate()
    values: list[tuple[str, str]] = [
        ("layer_height", _number(settings.layer_height)),
        ("ironing_enabled", _as_bool(settings.ironing_enabled)),
        ("ironing_only_highest_layer", _as_bool(settings.ironing_only_highest_layer)),
        ("z_seam_type", settings.z_seam_type),
        ("infill_pattern", settings.infill_pattern),
        ("infill_sparse_density", _number(settings.infill_sparse_density)),
        ("retraction_combing", settings.retraction_combing),
        ("adhesion_type", settings.adhesion_type),
        ("support_enable", _as_bool(settings.support_enable)),
        ("center_object", "false"),
        ("mesh_position_x", "0"),
        ("mesh_position_y", "0"),
        ("mesh_position_z", "0"),
    ]
    if settings.z_seam_type == "user_specified":
        values.append(("z_seam_position", settings.z_seam_position))
    if settings.adhesion_type == "brim":
        values.append(("brim_width", _number(settings.brim_width)))
    elif settings.adhesion_type == "skirt":
        values.append(("skirt_line_count", str(int(settings.skirt_line_count))))
    elif settings.adhesion_type == "raft":
        values.append(("raft_margin", _number(settings.raft_margin)))
    elif settings.adhesion_type == "none":
        pass
    else:
        raise AssertionError(settings.adhesion_type)
    if settings.support_enable:
        values.extend(
            [
                ("support_type", settings.support_type),
                ("support_structure", settings.support_structure),
                ("support_angle", _number(settings.support_angle)),
                ("support_infill_rate", _number(settings.support_infill_rate)),
            ]
        )
    return values


def _number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return format(value, "g")
