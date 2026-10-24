from __future__ import annotations

import pytest

from auto_slicer.settings_schema import SliceSettings, cura_setting_overrides, pack_gap


def test_defaults_match_the_shop_profile() -> None:
    settings = SliceSettings()
    assert settings.layer_height == 0.2
    assert settings.ironing_enabled is True
    assert settings.ironing_only_highest_layer is True
    assert settings.z_seam_type == "user_specified"
    assert settings.z_seam_position == "backright"
    assert settings.orca_seam == "back"
    assert settings.orca_scarf_joint == "external"
    assert settings.orca_scarf_conditional is True
    assert settings.infill_pattern == "lightning"
    assert settings.infill_sparse_density == 5
    assert settings.retraction_combing == "noskin"
    assert settings.travel_avoid_other_parts is True
    assert settings.wall_thickness == 1.2
    assert settings.make_overhang_printable is False
    assert settings.purge_line is True
    assert settings.meshfix_maximum_resolution == 0.05
    assert settings.meshfix_maximum_deviation == 0.025
    assert settings.meshfix_maximum_travel_resolution == 0.2
    assert settings.meshfix_union_all is True
    assert settings.orca_resolution == 0.012
    assert settings.precise_outer_wall is True
    assert settings.precise_z_height is True
    assert settings.enable_arc_fitting is False
    assert settings.slice_closing_radius == 0.05
    assert settings.elephant_foot_compensation == 0.15
    assert settings.z_hop_enabled is True
    assert settings.z_hop == 0.2
    assert settings.z_hop_type == "Normal Lift"
    assert settings.z_hop_on_surfaces == "Top Only"
    assert settings.adhesion_type == "brim"
    assert settings.support_enable is False
    assert settings.orca_arrange_spacing == 5
    assert pack_gap(settings) == 5
    assert pack_gap(SliceSettings(orca_arrange_spacing=2.5)) == 2.5
    assert settings.orca_arrange_rotate is False
    assert settings.orca_arrange_multicolor is True
    assert settings.orca_arrange_align_y is False


def test_shared_overrides_put_combing_seam_and_infill_last_among_themselves() -> None:
    pairs = dict(cura_setting_overrides(SliceSettings()))
    assert pairs["layer_height"] == "0.2"
    assert pairs["ironing_enabled"] == "true"
    assert pairs["ironing_only_highest_layer"] == "true"
    assert pairs["z_seam_type"] == "user_specified"
    assert pairs["z_seam_position"] == "backright"
    assert pairs["infill_pattern"] == "lightning"
    assert pairs["infill_sparse_density"] == "5"
    assert pairs["retraction_combing"] == "noskin"
    assert pairs["travel_avoid_other_parts"] == "true"
    assert pairs["wall_thickness"] == "1.2"
    assert pairs["conical_overhang_enabled"] == "false"
    assert pairs["meshfix_maximum_resolution"] == "0.05"
    assert pairs["meshfix_maximum_deviation"] == "0.025"
    assert pairs["meshfix_maximum_travel_resolution"] == "0.2"
    assert pairs["meshfix_union_all"] == "true"
    assert pairs["xy_offset_layer_0"] == "-0.15"
    assert pairs["retraction_hop"] == "0.2"
    assert pairs["retraction_hop_enabled"] == "true"
    assert pairs["adhesion_type"] == "brim"
    off = dict(cura_setting_overrides(SliceSettings(z_hop_enabled=False, z_hop=0.4)))
    assert off["retraction_hop_enabled"] == "false"
    assert off["retraction_hop"] == "0"
    assert pairs["brim_width"] == "8"
    assert pairs["support_enable"] == "false"
    assert pairs["center_object"] == "false"
    assert "support_type" not in pairs


def test_skirt_and_supports_add_their_settings() -> None:
    settings = SliceSettings(adhesion_type="skirt", support_enable=True, support_structure="tree")
    pairs = dict(cura_setting_overrides(settings))
    assert pairs["skirt_line_count"] == "3"
    assert "brim_width" not in pairs
    assert pairs["support_structure"] == "tree"
    assert pairs["support_type"] == "buildplate"


def test_rejects_unknown_combing_mode() -> None:
    with pytest.raises(ValueError):
        SliceSettings(retraction_combing="sideways").validate()
