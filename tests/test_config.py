"""Config loading: .env parsing and ${VAR} interpolation.

Both have bitten setup already - an empty-but-commented line that yielded the comment
as its value, and a placeholder that silently matched nothing - so they are pinned here.
"""
import os

import pytest

from src.config import load_dotenv, load_settings

ENV = """\
# a comment line
AML_PROFILE=zoho                    # dev | zoho | microsoft
export ZOHO_CLIENT_ID=1000.ABC
ZOHO_SECRET_QUOTED='sec#ret'
ZOHO_TOKEN="1000.rrr.ttt"
ZOHO_EMPTY_WITH_COMMENT=            # the value is empty; this is only a note
ZOHO_EMPTY=
ALREADY_SET=from_file
no_equals_line
"""


@pytest.fixture
def envfile(tmp_path):
    p = tmp_path / ".env"
    p.write_text(ENV, encoding="utf-8")
    return p


def _clean(monkeypatch):
    for k in ("AML_PROFILE", "ZOHO_CLIENT_ID", "ZOHO_SECRET_QUOTED", "ZOHO_TOKEN",
              "ZOHO_EMPTY_WITH_COMMENT", "ZOHO_EMPTY", "ALREADY_SET"):
        monkeypatch.delenv(k, raising=False)


def test_dotenv_parses_values(envfile, monkeypatch):
    _clean(monkeypatch)
    load_dotenv(envfile)
    assert os.environ["AML_PROFILE"] == "zoho"          # trailing comment dropped
    assert os.environ["ZOHO_CLIENT_ID"] == "1000.ABC"   # `export ` prefix tolerated
    assert os.environ["ZOHO_SECRET_QUOTED"] == "sec#ret"  # a # inside quotes survives
    assert os.environ["ZOHO_TOKEN"] == "1000.rrr.ttt"


def test_commented_empty_value_stays_unset(envfile, monkeypatch):
    """`KEY=   # note` must be empty, not the note - a non-empty junk value would pass
    every downstream 'is it configured?' guard and then match nothing at runtime."""
    _clean(monkeypatch)
    load_dotenv(envfile)
    assert "ZOHO_EMPTY_WITH_COMMENT" not in os.environ
    assert "ZOHO_EMPTY" not in os.environ


def test_real_environment_wins(envfile, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("ALREADY_SET", "from_environment")
    load_dotenv(envfile)
    assert os.environ["ALREADY_SET"] == "from_environment"


def test_missing_file_is_a_noop(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == []


def test_interpolation_default_and_placeholder(monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.load_dotenv", lambda *a, **k: [])
    monkeypatch.delenv("ZOHO_SENDER_FILTER", raising=False)
    monkeypatch.delenv("ZOHO_DC", raising=False)
    settings = tmp_path / "s.yaml"
    settings.write_text(
        "profiles:\n  t:\n    a: \"${ZOHO_DC:-in}\"\n    b: \"${ZOHO_SENDER_FILTER}\"\n",
        encoding="utf-8")
    out = load_settings("t", settings)
    assert out["a"] == "in"                          # default applies when unset
    assert out["b"] == "${ZOHO_SENDER_FILTER}"       # placeholder kept, so it fails loudly

    monkeypatch.setenv("ZOHO_DC", "com")
    monkeypatch.setenv("ZOHO_SENDER_FILTER", "ops@real.in")
    out = load_settings("t", settings)
    assert (out["a"], out["b"]) == ("com", "ops@real.in")


def test_inline_secret_is_refused(tmp_path, monkeypatch):
    """A credential typed into settings.yaml must stop the load, not be used."""
    monkeypatch.setattr("src.config.load_dotenv", lambda *a, **k: [])
    s = tmp_path / "s.yaml"
    s.write_text("profiles:\n  t:\n    ticket_config:\n      password: \"hunter2\"\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="credential is written literally"):
        load_settings("t", s)


def test_env_reference_and_empty_are_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.load_dotenv", lambda *a, **k: [])
    monkeypatch.setenv("ODOO_PASSWORD", "from-env")
    s = tmp_path / "s.yaml"
    s.write_text("profiles:\n  t:\n    ticket_config:\n"
                 "      password: \"${ODOO_PASSWORD}\"\n      user: \"\"\n", encoding="utf-8")
    out = load_settings("t", s)
    assert out["ticket_config"]["password"] == "from-env"


def test_shipped_settings_carry_no_secrets():
    """The real config file, every profile - the check that would have caught the
    Odoo password that was pasted into it."""
    import yaml
    from src.config import SETTINGS_PATH, assert_no_inline_secrets
    raw = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    for name, prof in raw["profiles"].items():
        assert_no_inline_secrets(prof, f"profiles.{name}.")
