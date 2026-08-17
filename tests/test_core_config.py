"""Config defaults, validation and the audit-line config hash."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from interlayer.core.config import (
    Config,
    config_from_env,
    config_from_mapping,
    default_config,
    load_config,
)
from interlayer.core.errors import ConfigError
from interlayer.core.models import CompliancePosture


def test_shipped_defaults_are_exactly_the_documented_ones() -> None:
    config = default_config()
    assert config.state_root == Path.home() / ".interlayer"
    assert config.retention_days == 90
    assert config.retain_emails is False
    assert config.enabled_adapters == ["first_party_export"]  # PRIV-04
    assert config.include_inferred is False
    assert config.min_inferred_confidence == 0.5
    assert config.allow_contact_fields is False
    assert config.base_seed == 42
    assert config.resolution == 1.0
    assert config.consensus_runs == 25


def test_priv_04_only_the_first_party_posture_is_enabled_by_default() -> None:
    config = default_config()
    assert config.adapter_enabled(CompliancePosture.FIRST_PARTY_EXPORT)
    for posture in (
        CompliancePosture.MANUAL_CAPTURE,
        CompliancePosture.THIRD_PARTY_API,
        CompliancePosture.AUTOMATED,
    ):
        assert not config.adapter_enabled(posture)
    assert config.enabled_postures == frozenset({CompliancePosture.FIRST_PARTY_EXPORT})


def test_state_root_is_expanded_and_absolute() -> None:
    assert Config(state_root="~/somewhere").state_root == Path.home() / "somewhere"
    assert Config(state_root="relative/root").state_root.is_absolute()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retention_days": -1},
        {"retention_days": 4000},
        {"enabled_adapters": ["first_party_export", "definitely_not_an_adapter"]},
        {"enabled_adapters": ["scrape_everything"]},
        {"resolution": 0.0},
        {"resolution": -1.5},
        {"consensus_runs": 0},
        {"min_inferred_confidence": 1.5},
        {"min_inferred_confidence": -0.1},
        {"base_seed": -1},
        {"retain_emails": "perhaps"},
        {"state_root": ""},
        {"state_root": 17},
    ],
)
def test_nonsense_settings_raise_config_error(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        Config(**kwargs)  # type: ignore[arg-type]


def test_retention_zero_is_allowed() -> None:
    assert Config(retention_days=0).retention_days == 0


def test_config_hash_is_hex_stable_and_sensitive() -> None:
    a = default_config()
    b = default_config()
    assert a.config_hash == b.config_hash
    assert len(a.config_hash) == 64
    assert int(a.config_hash, 16) >= 0

    for changed in (
        a.replace(retention_days=30),
        a.replace(retain_emails=True),
        a.replace(include_inferred=True),
        a.replace(base_seed=1),
        a.replace(resolution=1.5),
        a.replace(consensus_runs=3),
        a.replace(enabled_adapters=["first_party_export", "manual_capture"]),
        a.replace(state_root="/tmp/elsewhere"),
    ):
        assert changed.config_hash != a.config_hash


def test_config_hash_is_stable_across_processes_and_hash_seeds() -> None:
    """A Python ``hash()`` would vary with PYTHONHASHSEED. Canonical JSON does not."""
    program = (
        "from interlayer.core.config import Config;"
        "print(Config(state_root='/tmp/fixed').config_hash)"
    )
    digests = set()
    for seed in ("0", "1", "999"):
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        digests.add(completed.stdout.strip())
    assert len(digests) == 1
    assert digests.pop() == Config(state_root="/tmp/fixed").config_hash


def test_replace_revalidates_and_rejects_unknown_settings() -> None:
    config = default_config()
    with pytest.raises(ConfigError):
        config.replace(retention_days=-5)
    with pytest.raises(ConfigError):
        config.replace(retian_emails=True)


def test_load_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                f'state_root = "{tmp_path / "state"}"',
                "retention_days = 30",
                "retain_emails = true",
                'enabled_adapters = ["first_party_export", "manual_capture"]',
                "consensus_runs = 7",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(path, env={})
    assert config.state_root == tmp_path / "state"
    assert config.retention_days == 30
    assert config.retain_emails is True
    assert config.enabled_adapters == ["first_party_export", "manual_capture"]
    assert config.consensus_runs == 7


def test_load_from_toml_with_wrapping_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[interlayer]\nretention_days = 14\n", encoding="utf-8")
    assert load_config(path, env={}).retention_days == 14


def test_load_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "retention_days: 45\nretain_emails: false\nenabled_adapters:\n  - first_party_export\n",
        encoding="utf-8",
    )
    config = load_config(path, env={})
    assert config.retention_days == 45
    assert config.enabled_adapters == ["first_party_export"]


def test_unknown_key_in_file_is_an_error_not_a_shrug(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('retian_emails = true\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(path, env={})


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", env={})


def test_unsupported_format_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    path.write_text("retention_days = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported config format"):
        load_config(path, env={})


def test_env_overrides_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("retention_days = 30\n", encoding="utf-8")
    config = load_config(
        path, env={"INTERLAYER_RETENTION_DAYS": "7", "INTERLAYER_RETAIN_EMAILS": "true"}
    )
    assert config.retention_days == 7
    assert config.retain_emails is True


def test_explicit_overrides_beat_env(tmp_path: Path) -> None:
    config = load_config(
        None, env={"INTERLAYER_RETENTION_DAYS": "7"}, overrides={"retention_days": 3}
    )
    assert config.retention_days == 3


def test_env_only(tmp_path: Path) -> None:
    config = config_from_env(
        {
            "INTERLAYER_STATE_ROOT": str(tmp_path),
            "INTERLAYER_ENABLED_ADAPTERS": "first_party_export,manual_capture",
            "INTERLAYER_INCLUDE_INFERRED": "yes",
            "INTERLAYER_MIN_INFERRED_CONFIDENCE": "0.75",
        }
    )
    assert config.state_root == tmp_path
    assert config.enabled_adapters == ["first_party_export", "manual_capture"]
    assert config.include_inferred is True
    assert config.min_inferred_confidence == 0.75


def test_bad_env_value_raises() -> None:
    with pytest.raises(ConfigError):
        config_from_env({"INTERLAYER_RETENTION_DAYS": "ninety"})


def test_derived_paths_live_under_the_state_root(tmp_path: Path) -> None:
    config = Config(state_root=tmp_path)
    assert config.db_path.parent == tmp_path
    assert config.audit_path == tmp_path / "audit.log"


def test_settings_view_is_json_safe() -> None:
    settings = default_config().settings()
    assert isinstance(settings["state_root"], str)
    assert set(settings) == {
        "state_root",
        "retention_days",
        "retain_emails",
        "enabled_adapters",
        "include_inferred",
        "min_inferred_confidence",
        "allow_contact_fields",
        "base_seed",
        "resolution",
        "consensus_runs",
    }


def test_config_from_mapping_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigError):
        config_from_mapping({"nope": 1})
