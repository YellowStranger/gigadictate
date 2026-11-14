import pytest

from gigadictate.hotkey import (
    VK_ESCAPE, VK_LCONTROL, VK_LMENU, VK_LSHIFT, VK_LWIN, VK_RCONTROL, VK_RMENU, Callbacks, HotkeyCapture,
    HotkeyError, HotkeyMachine, combo_from_vks, display_name, format_combo, parse_hotkey, validate_combo,
)

C, F9, D, SPACE = 0x43, 0x78, 0x44, 0x20


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def m():
    return make("right ctrl")


def make(hk):
    log = []
    clock = Clock()
    mach = HotkeyMachine(parse_hotkey(hk), Callbacks(lambda: log.append("start"), lambda: log.append("stop"),
                                                     lambda: log.append("cancel")), 0.35, clock)
    mach.log, mach.clk = log, clock
    return mach


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("text,expected", [
    ("right ctrl", "right ctrl"), ("RCtrl", "right ctrl"), ("Ctrl + Alt + D", "ctrl+alt+d"),
    ("alt+ctrl+d", "ctrl+alt+d"), ("F9", "f9"), ("ctrl+win", "ctrl+win"), ("ctrl+shift+space", "ctrl+shift+space"),
    ("caps lock", "caps lock"), ("AltGr", "right alt"),
])
def test_parse_roundtrip(text, expected):
    assert format_combo(parse_hotkey(text)) == expected


@pytest.mark.parametrize("bad", ["", "  ", "ctrl+", "+d", "foo", "ctrl+ctrl", None, "ctrl++d"])
def test_parse_errors(bad):
    with pytest.raises(HotkeyError):
        parse_hotkey(bad)


@pytest.mark.parametrize("hk,ok", [
    ("right ctrl", True), ("f9", True), ("caps lock", True), ("ctrl+win", True), ("ctrl+alt+d", True),
    ("pause", True), ("ctrl+shift+space", True),
    ("a", False), ("space", False), ("enter", False), ("left ctrl", False), ("shift", False),
    ("alt", False), ("win", False), ("right alt", False), ("ctrl+shift", False), ("alt+shift", False),
    ("shift+a", False), ("ctrl+esc", False), ("esc", False), ("ctrl+a+b", False),
])
def test_validate(hk, ok):
    assert (validate_combo(parse_hotkey(hk)) is None) == ok


def test_display_name():
    assert display_name("right ctrl") == "Правый Ctrl"
    assert display_name("ctrl+alt+d") == "Ctrl+Alt+D"
    assert display_name("caps lock") == "Caps Lock"


def test_combo_from_vks():
    assert format_combo(combo_from_vks({VK_RCONTROL})) == "right ctrl"
    assert format_combo(combo_from_vks({VK_RCONTROL, VK_LMENU, D})) == "ctrl+alt+d"
    assert format_combo(combo_from_vks({F9})) == "f9"


# ------------------------------------------------------------------ machine
def test_hold_mode(m):
    m.key_down(VK_RCONTROL)
    m.clk.t += 1.0
    m.key_down(VK_RCONTROL)  # autorepeat
    m.key_up(VK_RCONTROL)
    assert m.log == ["start", "stop"]
    assert m.state == "idle" and not m.recording


def test_toggle_mode(m):
    m.key_down(VK_RCONTROL)
    m.clk.t += 0.1
    m.key_up(VK_RCONTROL)
    assert m.log == ["start"] and m.recording
    m.clk.t += 3
    m.key_down(VK_LSHIFT)  # typing other keys during toggle recording is fine
    m.key_up(VK_LSHIFT)
    m.key_down(VK_RCONTROL)
    assert m.log == ["start", "stop"]
    m.clk.t += 2  # long second press must not start a new recording on release
    m.key_up(VK_RCONTROL)
    assert m.log == ["start", "stop"] and m.state == "idle"


def test_shortcut_cancels(m):
    m.key_down(VK_RCONTROL)
    m.clk.t += 0.2
    assert m.key_down(C) is False  # Ctrl+C still reaches the app
    m.key_up(C)
    m.key_up(VK_RCONTROL)
    assert m.log == ["start", "cancel"] and m.state == "idle"


def test_other_key_after_long_hold_keeps_recording(m):
    m.key_down(VK_RCONTROL)
    m.clk.t += 5
    m.key_down(C)
    m.key_up(C)
    m.key_up(VK_RCONTROL)
    assert m.log == ["start", "stop"]


def test_escape_cancels_and_is_swallowed(m):
    m.key_down(VK_RCONTROL)
    m.key_up(VK_RCONTROL)
    assert m.key_down(VK_ESCAPE) is True
    assert m.key_up(VK_ESCAPE) is True
    assert m.log == ["start", "cancel"] and m.state == "idle"
    assert m.key_down(VK_ESCAPE) is False  # not swallowed when not recording
    m.key_up(VK_ESCAPE)


def test_escape_while_holding(m):
    m.key_down(VK_RCONTROL)
    m.clk.t += 2
    m.key_down(VK_ESCAPE)
    m.key_up(VK_ESCAPE)
    m.key_up(VK_RCONTROL)
    assert m.log == ["start", "cancel"]
    m.key_down(VK_RCONTROL)
    assert m.log[-1] == "start"


def test_combo_trigger_key_swallowed():
    m = make("ctrl+alt+d")
    assert m.key_down(VK_LCONTROL) is False
    assert m.key_down(VK_LMENU) is False
    assert m.key_down(D) is True
    assert m.key_down(D) is True  # autorepeat also swallowed
    m.clk.t += 1
    assert m.key_up(D) is True
    assert m.key_up(VK_LMENU) is False
    m.key_up(VK_LCONTROL)
    assert m.log == ["start", "stop"]
    # D alone types normally
    assert m.key_down(D) is False and m.key_up(D) is False


def test_combo_any_side_modifier():
    m = make("ctrl+alt+d")
    m.key_down(VK_RCONTROL)
    m.key_down(VK_RMENU)
    m.key_down(D)
    assert m.log == ["start"]


def test_order_of_combo_press_does_not_matter():
    m = make("ctrl+win")
    m.key_down(VK_LWIN)
    m.key_down(VK_LCONTROL)
    assert m.log == ["start"]
    m.clk.t += 1
    m.key_up(VK_LWIN)
    assert m.log == ["start", "stop"]
    m.key_up(VK_LCONTROL)
    m.key_down(VK_LCONTROL)
    m.key_down(VK_LWIN)
    assert m.log == ["start", "stop", "start"]


def test_lost_key_up_recovered_by_tick(m):
    m.key_down(VK_RCONTROL)
    m.clk.t += 2  # user locks the screen with the key held; key-up never arrives
    m.tick(lambda vk: False)
    assert m.log == ["start", "stop"] and m.state == "idle"
    m.key_down(VK_RCONTROL)
    assert m.log[-1] == "start"


def test_force_stop_then_release(m):
    m.key_down(VK_RCONTROL)
    m.force_stop()  # e.g. max duration reached while held
    m.clk.t += 1
    m.key_up(VK_RCONTROL)
    assert m.log == ["start"]  # no extra stop
    m.key_down(VK_RCONTROL)
    assert m.log == ["start", "start"]


def test_unmatched_key_up_ignored(m):
    assert m.key_up(VK_RCONTROL) is False
    assert m.log == []


def test_capture():
    got = []
    cap = HotkeyCapture(got.append)
    assert cap.key_down(VK_LCONTROL) and cap.key_down(VK_LMENU) and cap.key_down(D)
    cap.key_up(D)
    cap.key_up(VK_LMENU)
    assert not got
    cap.key_up(VK_LCONTROL)
    assert format_combo(got[0]) == "ctrl+alt+d"


def test_capture_escape_cancels():
    got = []
    cap = HotkeyCapture(got.append)
    cap.key_down(VK_ESCAPE)
    assert got == [None]
