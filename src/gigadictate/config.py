"""Settings: JSON file in the user's app-data folder, merged over defaults and validated."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "GigaDictate"

MODELS = {
    # name -> human label
    "gigaam-v3-e2e-rnnt": "GigaAM v3 e2e RNNT (точнее, с пунктуацией)",
    "gigaam-v3-e2e-ctc": "GigaAM v3 e2e CTC (быстрее, с пунктуацией)",
}
INSERT_METHODS = ("auto", "type", "paste")


@dataclass
class Config:
    hotkey: str = "right ctrl"
    # Press shorter than this = toggle mode (press again to stop); longer = push-to-talk.
    hold_threshold_ms: int = 350
    max_recording_s: int = 300
    # Keep recording a little after the key is released so the last word is not cut.
    tail_ms: int = 200
    model: str = "gigaam-v3-e2e-rnnt"
    threads: int = 0  # 0 = auto
    insert_method: str = "auto"
    paste_threshold_chars: int = 200
    trailing_space: bool = True
    voice_commands: bool = True
    sounds: bool = True
    unload_after_idle_min: int = 10  # 0 = keep model in memory forever
    input_device: str = ""  # "" = system default
    _extra: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ helpers
    def validate(self) -> list[str]:
        """Fix invalid values in place, return a list of problems found."""
        problems: list[str] = []
        d = Config()

        def bad(name: str) -> None:
            problems.append(f"{name}={getattr(self, name)!r} недопустимо, использую {getattr(d, name)!r}")
            setattr(self, name, getattr(d, name))

        for f in fields(self):
            if f.name.startswith("_"):
                continue
            want = type(getattr(d, f.name))
            val = getattr(self, f.name)
            if want is bool and not isinstance(val, bool):
                bad(f.name)
            elif want is int and (isinstance(val, bool) or not isinstance(val, int)):
                bad(f.name)
            elif want is str and not isinstance(val, str):
                bad(f.name)

        ranges = {
            "hold_threshold_ms": (100, 2000),
            "max_recording_s": (5, 3600),
            "tail_ms": (0, 1000),
            "threads": (0, 64),
            "paste_threshold_chars": (0, 100000),
            "unload_after_idle_min": (0, 24 * 60),
        }
        for name, (lo, hi) in ranges.items():
            if not lo <= getattr(self, name) <= hi:
                bad(name)
        if self.model not in MODELS:
            bad("model")
        if self.insert_method not in INSERT_METHODS:
            bad("insert_method")

        from .hotkey import HotkeyError, parse_hotkey, validate_combo

        try:
            err = validate_combo(parse_hotkey(self.hotkey))
        except HotkeyError as e:
            err = str(e)
        if err:
            problems.append(f"hotkey={self.hotkey!r}: {err}; использую {d.hotkey!r}")
            self.hotkey = d.hotkey
        return problems

    def to_json(self) -> dict:
        data = asdict(self)
        extra = data.pop("_extra")
        return {**extra, **data}


def app_dir() -> Path:
    base = os.environ.get("GIGADICTATE_HOME")
    if base:
        p = Path(base)
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    else:
        p = Path.home() / ".config" / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return app_dir() / "config.json"


def load(path: Path | None = None) -> tuple[Config, list[str]]:
    path = path or config_path()
    problems: list[str] = []
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("корень файла не объект")
        except (ValueError, OSError) as e:
            backup = path.with_suffix(".json.broken")
            try:
                os.replace(path, backup)
            except OSError:
                pass
            problems.append(f"Файл настроек повреждён ({e}), сохранён как {backup.name}; использую значения по умолчанию")
            raw = {}
    known = {f.name for f in fields(Config) if not f.name.startswith("_")}
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    cfg._extra = {k: v for k, v in raw.items() if k not in known}
    problems += cfg.validate()
    if not path.exists() or problems:
        save(cfg, path)
    return cfg, problems


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or config_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written config
