"""Settings loading with ${ENV_VAR} and ${ENV_VAR:-default} interpolation."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")
_COMMENT = re.compile(r"\s+#.*$")


def load_dotenv(path: str | Path | None = None) -> list[str]:
    """Populate os.environ from .env. Returns the keys it set.

    Real environment variables win: anything already set is left alone, so a
    container's config or an explicit `export` still overrides the file. Parsing
    follows `set -a; source .env` closely enough for KEY=value files - optional
    `export` prefix, quotes stripped, trailing ` # comment` dropped.

    Deliberately dependency-free: the alternative is every entry point dying with
    KeyError because someone opened a new terminal tab.
    """
    p = Path(path or ROOT / ".env")
    if not p.exists():
        return []
    done = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        # strip the trailing comment BEFORE trimming whitespace: on `KEY=   # note` the
        # leading spaces are what mark the # as a comment, exactly as the shell reads it.
        # Trimming first would leave the comment text itself as the value - a non-empty
        # value that then sails past every "is it set?" guard downstream.
        v = _COMMENT.sub("", v).strip().strip("\"'")
        if k and v and k not in os.environ:
            os.environ[k] = v
            done.append(k)
    return done


def _interp(node):
    if isinstance(node, str):
        # ${VAR} keeps the literal when unset - so a forgotten value fails loudly at the
        # connector rather than silently matching nothing. ${VAR:-x} falls back to x.
        return _ENV.sub(lambda m: os.environ.get(m.group(1)) or
                        (m.group(2) if m.group(2) is not None else m.group(0)), node)
    if isinstance(node, dict):
        return {k: _interp(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interp(v) for v in node]
    return node


_SECRET_KEY = re.compile(r"(password|secret|token|api_?key|credential|user)$", re.I)


def assert_no_inline_secrets(node, where: str = "") -> None:
    """Refuse to load a profile that carries a credential as a literal.

    Config files get copied, pasted into tickets and committed; .env does not, and is
    gitignored. Anything credential-shaped must therefore be a ${VAR} reference, so the
    only copy of the value lives in one place. Empty is fine - that is 'not configured'.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if _SECRET_KEY.search(str(k)) and isinstance(v, str) and v.strip() \
                    and not v.strip().startswith("${"):
                raise ValueError(
                    f"{where}{k}: a credential is written literally in config/settings.yaml. "
                    f"Move the value to .env and reference it here as \"${{VAR_NAME}}\" - "
                    f"and rotate it, since it has been sitting in a plaintext config file.")
            assert_no_inline_secrets(v, f"{where}{k}.")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            assert_no_inline_secrets(v, f"{where}{i}.")


def load_settings(profile: str | None = None, path: str | Path | None = None) -> dict:
    load_dotenv()                       # before ${VAR} interpolation, and before any connector
    raw = yaml.safe_load(Path(path or SETTINGS_PATH).read_text(encoding="utf-8"))
    prof = profile or os.environ.get("AML_PROFILE") or raw.get("active_profile", "dev")
    if prof not in raw["profiles"]:
        raise KeyError(f"Unknown profile '{prof}'. Available: {list(raw['profiles'])}")
    # checked on the RAW profile, before interpolation - afterwards a ${VAR} that came
    # from .env is indistinguishable from a value typed into the file
    assert_no_inline_secrets(raw["profiles"][prof], f"profiles.{prof}.")
    out = _interp(raw["profiles"][prof])
    out["_profile"] = prof
    out["_scheduler"] = raw.get("scheduler", {})
    return out


def profiles(path: str | Path | None = None) -> list[str]:
    raw = yaml.safe_load(Path(path or SETTINGS_PATH).read_text(encoding="utf-8"))
    return list(raw["profiles"])
