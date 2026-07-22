"""Encoder sign vs closed-loop spin validation."""

from base_spin_motion import (
    encoder_delta_wrong_dir,
    expected_encoder_delta,
)


def test_expected_delta_honors_encoder_sign():
    assert expected_encoder_delta(8.0, 1.0) == 8.0
    assert expected_encoder_delta(8.0, -1.0) == -8.0
    assert expected_encoder_delta(-8.0, -1.0) == 8.0


def test_negative_encoder_delta_is_ok_for_positive_cmd_when_sign_minus_one():
    assert encoder_delta_wrong_dir(8.0, -6.3, encoder_sign=-1.0) is False


def test_positive_encoder_delta_is_wrong_for_positive_cmd_when_sign_minus_one():
    assert encoder_delta_wrong_dir(8.0, 6.3, encoder_sign=-1.0) is True


def test_matching_signs_ok_with_encoder_sign_one():
    assert encoder_delta_wrong_dir(8.0, 6.3, encoder_sign=1.0) is False
    assert encoder_delta_wrong_dir(8.0, -6.3, encoder_sign=1.0) is True
