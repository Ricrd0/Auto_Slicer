from __future__ import annotations

import pytest

from auto_slicer.settings_schema import SliceSettings, cura_setting_overrides


def test_defaults_match_the_shop_profile() -> None:
    settings = SliceSettings()
    assert settings.layer_height == 0.2
    assert settings.ironing_enabled is True
    assert settings.ironing_only_highest_layer is True
    assert settings.z_seam_type == "user_specified"
    assert settings.z_seam_position == "backright"
    assert settings.infill_pattern == "lightning"
    assert settings.infill_sparse_density == 5
    assert settings.retraction_combing == "noskin"
    assert settings.adhesion_type == "brim"
    assert settings.support_enable is False


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
    assert pairs["adhesion_type"] == "brim"
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
