import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize("shoulder", ["200", "201", "299", "2s0", "2az", "29f"])
def test_dark_2_shoulder_accepts_version_and_two_lowercase_alphanumeric_code(shoulder: str):
    assert Settings(_env_file=None, minter_shoulder=shoulder).minter_shoulder == shoulder


@pytest.mark.parametrize("shoulder", ["", "001", "00x", "300", "20", "2000", "2S0", "2-é", "2-0"])
def test_dark_2_shoulder_rejects_legacy_or_invalid_formats(shoulder: str):
    with pytest.raises(ValidationError, match="MINTER_SHOULDER"):
        Settings(_env_file=None, minter_shoulder=shoulder)
