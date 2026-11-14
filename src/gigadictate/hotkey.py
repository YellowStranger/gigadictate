"""Hotkey parsing and the press/hold state machine.

Platform independent: works on Windows virtual-key codes but never calls the OS,
so all of the tricky timing logic is unit-tested on any machine.

Behaviour
---------
* Hotkey pressed and released quickly (< hold threshold)  -> toggle mode:
  recording continues until the hotkey is pressed again.
* Hotkey held longer than the threshold                    -> push-to-talk:
  recording stops on release.
* Another key pressed while the hotkey is held during the first second
  (e.g. Right Ctrl + C)                                    -> it was a shortcut,
  recording is cancelled silently.
* Esc while recording                                      -> cancel (Esc is swallowed).
* Key-up lost (lock screen, UAC prompt)                    -> `tick()` notices the key
  is physically up and stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

# ---------------------------------------------------------------- key tables
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LMENU, VK_RMENU = 0xA4, 0xA5
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_ESCAPE = 0x1B
VK_MASK = 0xE8  # unassigned VK, used to stop Alt/Win release from opening menus

MODIFIER_VKS = {VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN}

_MODS: dict[str, frozenset[int]] = {
    "ctrl": frozenset({VK_LCONTROL, VK_RCONTROL}),
    "left ctrl": frozenset({VK_LCONTROL}),
    "right ctrl": frozenset({VK_RCONTROL}),
    "shift": frozenset({VK_LSHIFT, VK_RSHIFT}),
    "left shift": frozenset({VK_LSHIFT}),
    "right shift": frozenset({VK_RSHIFT}),
    "alt": frozenset({VK_LMENU, VK_RMENU}),
    "left alt": frozenset({VK_LMENU}),
    "right alt": frozenset({VK_RMENU}),
    "win": frozenset({VK_LWIN, VK_RWIN}),
    "left win": frozenset({VK_LWIN}),
    "right win": frozenset({VK_RWIN}),
}
_ALIASES = {"control": "ctrl", "rctrl": "right ctrl", "lctrl": "left ctrl", "ralt": "right alt",
            "lalt": "left alt", "altgr": "right alt", "rshift": "right shift", "lshift": "left shift",
            "windows": "win", "super": "win", "escape": "esc", "return": "enter", "del": "delete",
            "ins": "insert", "pgup": "page up", "pgdn": "page down", "menu": "apps",
            "pause/break": "pause", "scroll": "scroll lock", "caps": "caps lock"}

KEYS: dict[str, int] = {
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "page up": 0x21, "page down": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "scroll lock": 0x91, "caps lock": 0x14, "num lock": 0x90,
    "print screen": 0x2C, "apps": 0x5D,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
KEYS.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
KEYS.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(d): 0x30 + d for d in range(10)})
KEYS.update({f"f{i}": 0x6F + i for i in range(1, 25)})
KEYS.update({f"num{d}": 0x60 + d for d in range(10)})

_VK_NAME: dict[int, str] = {}
for _n, _v in KEYS.items():
    if not (len(_n) == 1 and _n.isupper()):
        _VK_NAME.setdefault(_v, _n)

# Keys that are fine as a single-key hotkey (nobody types them in text).
_SOLO_OK = {KEYS[f"f{i}"] for i in range(1, 25)} | {
    KEYS["pause"], KEYS["scroll lock"], KEYS["caps lock"], KEYS["insert"], KEYS["apps"], KEYS["print screen"],
} | {VK_RCONTROL}
_PRINTABLE = {v for k, v in KEYS.items() if len(k) == 1} | {KEYS["space"]} | {KEYS[f"num{d}"] for d in range(10)}


class HotkeyError(ValueError):
    pass


Combo = tuple[frozenset[int], ...]  # every group must have at least one key down


def parse_hotkey(text: str) -> Combo:
    if not isinstance(text, str) or not text.strip():
        raise HotkeyError("пустая горячая клавиша")
    groups: list[frozenset[int]] = []
    for part in text.lower().split("+"):
        name = " ".join(part.split())
        if not name:
            raise HotkeyError(f"лишний '+' в {text!r}")
        name = _ALIASES.get(name, name)
        if name in _MODS:
            g = _MODS[name]
        elif name in KEYS:
            g = frozenset({KEYS[name]})
        else:
            raise HotkeyError(f"неизвестная клавиша {part.strip()!r}")
        if g in groups:
            raise HotkeyError(f"клавиша {name!r} повторяется")
        groups.append(g)
    return tuple(groups)


def _group_name(g: frozenset[int]) -> str:
    for n, s in _MODS.items():
        if s == g:
            return n
    (vk,) = g
    return _VK_NAME.get(vk, f"vk{vk:02x}")


def format_combo(combo: Combo) -> str:
    order = {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}

    def key(g: frozenset[int]) -> tuple[int, str]:
        n = _group_name(g)
        return (order.get(n.split()[-1], 9), n)

    return "+".join(_group_name(g) for g in sorted(combo, key=key))


def display_name(text: str) -> str:
    """Human-friendly: 'right ctrl' -> 'Правый Ctrl', 'ctrl+alt+d' -> 'Ctrl+Alt+D'."""
    out = []
    for part in text.split("+"):
        side = ""
        if part.startswith("right "):
            side, part = "Правый ", part[6:]
        elif part.startswith("left "):
            side, part = "Левый ", part[5:]
        special = {"ctrl": "Ctrl", "alt": "Alt", "win": "Win", "shift": "Shift"}
        out.append(side + special.get(part, part.upper() if len(part) <= 3 else part.title()))
    return "+".join(out)


def combo_from_vks(vks: Iterable[int]) -> Combo:
    """Turn a set of captured keys into a combo. A lone modifier keeps its side;
    modifiers inside a combination become side-less (Ctrl+Alt+D works with either Ctrl)."""
    vks = set(vks)
    if len(vks) == 1:
        (vk,) = vks
        for g in _MODS.values():
            if g == frozenset({vk}):
                return (g,)
        return (frozenset({vk}),)
    groups: list[frozenset[int]] = []
    for vk in vks:
        if vk in MODIFIER_VKS:
            g = next(s for n, s in _MODS.items() if vk in s and " " not in n)
        else:
            g = frozenset({vk})
        if g not in groups:
            groups.append(g)
    return tuple(groups)


def validate_combo(combo: Combo) -> str | None:
    """Return an error message if the hotkey would break normal typing."""
    if not combo:
        return "пустая горячая клавиша"
    if any(VK_ESCAPE in g for g in combo):
        return "Esc занят: он отменяет диктовку"
    mods = [g for g in combo if g <= MODIFIER_VKS]
    keys = [g for g in combo if not g <= MODIFIER_VKS]
    if len(keys) > 1:
        return "можно использовать только одну обычную клавишу вместе с модификаторами"
    if len(combo) == 1:
        (vk,) = combo[0] if len(combo[0]) == 1 else (None,)
        if vk in _SOLO_OK:
            return None
        if combo[0] <= MODIFIER_VKS:
            return ("из одиночных модификаторов подходит только Правый Ctrl "
                    "(Shift нужен для заглавных, Alt/Win открывают меню)")
        return "эта клавиша нужна для набора текста; добавьте Ctrl/Alt/Win или выберите F1–F24"
    if not keys:
        names = {_group_name(g).split()[-1] for g in mods}
        if names in ({"ctrl", "shift"}, {"alt", "shift"}):
            return "это сочетание переключает раскладку клавиатуры"
        return None
    mod_names = {_group_name(g).split()[-1] for g in mods}
    if mod_names <= {"shift"} and keys[0] & _PRINTABLE:
        return "Shift+символ нужен для набора текста"
    return None


def combo_uses_menu_keys(combo: Combo) -> bool:
    return any(g & {VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN} for g in combo)


# ---------------------------------------------------------------- state machine
@dataclass
class Callbacks:
    start: Callable[[], None]
    stop: Callable[[], None]
    cancel: Callable[[], None]


IDLE = "idle"
UNDECIDED = "undecided"  # recording, hotkey still held, mode not known yet
TOGGLE = "toggle"  # recording, hotkey released, waiting for second press
WAIT_RELEASE = "wait_release"  # done, ignoring until hotkey fully released


class HotkeyMachine:
    SHORTCUT_WINDOW_S = 1.0

    def __init__(self, combo: Combo, callbacks: Callbacks, hold_threshold_s: float = 0.35,
                 clock: Callable[[], float] | None = None):
        import time

        self.combo = combo
        self.cb = callbacks
        self.hold_threshold_s = hold_threshold_s
        self.clock = clock or time.monotonic
        self.state = IDLE
        self.down: set[int] = set()
        self._swallowed: set[int] = set()
        self._t0 = 0.0
        self.recording = False
        self._trigger_vks = frozenset().union(*[g for g in combo if not g <= MODIFIER_VKS]) if combo else frozenset()
        self._combo_vks = frozenset().union(*combo) if combo else frozenset()

    # -- public API -------------------------------------------------------
    def set_combo(self, combo: Combo) -> None:
        self.reset()
        self.__init__(combo, self.cb, self.hold_threshold_s, self.clock)  # type: ignore[misc]

    def reset(self) -> None:
        if self.recording:
            self._cancel()
        self.state = IDLE
        self.down.clear()
        self._swallowed.clear()

    def satisfied(self, down: set[int] | None = None) -> bool:
        down = self.down if down is None else down
        return bool(self.combo) and all(g & down for g in self.combo)

    def key_down(self, vk: int) -> bool:
        """Returns True if the event must be swallowed."""
        if vk in self.down:  # auto-repeat
            return vk in self._swallowed
        was = self.satisfied()
        self.down.add(vk)
        now = self.clock()

        if self.recording and vk == VK_ESCAPE:
            self._cancel()
            self.state = WAIT_RELEASE if was else IDLE
            self._swallowed.add(vk)
            return True

        if vk not in self._combo_vks:
            if self.state == UNDECIDED and now - self._t0 < self.SHORTCUT_WINDOW_S:
                self._cancel()  # hotkey was part of a shortcut like RCtrl+C
                self.state = WAIT_RELEASE
            return False

        if self.satisfied() and not was:
            if self.state == IDLE:
                self._t0 = now
                self.state = UNDECIDED
                self.recording = True
                self.cb.start()
            elif self.state == TOGGLE:
                self._stop()
                self.state = WAIT_RELEASE
            if vk in self._trigger_vks:
                self._swallowed.add(vk)
                return True
        return False

    def key_up(self, vk: int) -> bool:
        swallow = vk in self._swallowed
        self._swallowed.discard(vk)
        if vk not in self.down:
            return swallow
        was = self.satisfied()
        self.down.discard(vk)
        if vk == VK_ESCAPE:
            return swallow
        if was and not self.satisfied():
            if self.state == UNDECIDED:
                if self.clock() - self._t0 >= self.hold_threshold_s:
                    self._stop()
                    self.state = IDLE
                else:
                    self.state = TOGGLE
            elif self.state == WAIT_RELEASE:
                self.state = IDLE
        if not self.down and self.state == WAIT_RELEASE:
            self.state = IDLE
        return swallow

    def tick(self, is_physically_down: Callable[[int], bool]) -> None:
        """Repair state when key-up events were lost (secure desktop, lock screen)."""
        for vk in list(self.down):
            if not is_physically_down(vk):
                self.key_up(vk)

    def force_stop(self) -> None:
        """Stop from outside (max duration reached, device lost)."""
        if self.recording:
            self.recording = False
            self.state = WAIT_RELEASE if self.satisfied() else IDLE

    # -- internals -----------------------------------------------------------
    def _stop(self) -> None:
        self.recording = False
        self.cb.stop()

    def _cancel(self) -> None:
        self.recording = False
        self.cb.cancel()


class HotkeyCapture:
    """Records the next key combination the user presses (for the settings menu)."""

    def __init__(self, on_done: Callable[[Combo | None], None]):
        self.on_done = on_done
        self.down: set[int] = set()
        self.peak: set[int] = set()
        self.done = False

    def key_down(self, vk: int) -> bool:
        if self.done:
            return False
        if vk == VK_ESCAPE and not self.down:
            self.done = True
            self.on_done(None)
            return True
        self.down.add(vk)
        self.peak |= self.down
        return True  # swallow everything while capturing

    def key_up(self, vk: int) -> bool:
        if self.done:
            return False
        self.down.discard(vk)
        if not self.down and self.peak:
            self.done = True
            self.on_done(combo_from_vks(self.peak))
        return True
