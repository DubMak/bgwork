"""Key names -> Windows virtual-key codes. Accepts xdotool-style names too (Return, Page_Down...)."""
from . import win32 as w

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN = 0x10, 0x11, 0x12, 0x5B

# Generic modifier -> the VKs whose state we flip so GetKeyState sees it either way.
MODIFIER_VKS = {VK_SHIFT: (0x10, 0xA0), VK_CONTROL: (0x11, 0xA2), VK_MENU: (0x12, 0xA4), VK_LWIN: (0x5B,)}

MODIFIERS = {
    "ctrl": VK_CONTROL, "control": VK_CONTROL, "ctl": VK_CONTROL,
    "shift": VK_SHIFT,
    "alt": VK_MENU, "option": VK_MENU, "menu": VK_MENU,
    "win": VK_LWIN, "super": VK_LWIN, "meta": VK_LWIN, "cmd": VK_LWIN, "windows": VK_LWIN,
}

NAMED = {
    "enter": 0x0D, "return": 0x0D, "kp_enter": 0x0D,
    "tab": 0x09, "space": 0x20, "backspace": 0x08, "back": 0x08,
    "escape": 0x1B, "esc": 0x1B, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "page_up": 0x21, "prior": 0x21,
    "pagedown": 0x22, "page_down": 0x22, "next": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "caps_lock": 0x14, "printscreen": 0x2C, "print": 0x2C, "pause": 0x13,
    "apps": 0x5D, "contextmenu": 0x5D,
    "minus": 0xBD, "-": 0xBD, "plus": 0xBB, "equal": 0xBB, "=": 0xBB,
    "comma": 0xBC, ",": 0xBC, "period": 0xBE, ".": 0xBE, "slash": 0xBF, "/": 0xBF,
    "semicolon": 0xBA, ";": 0xBA, "apostrophe": 0xDE, "'": 0xDE, "grave": 0xC0, "`": 0xC0,
    "bracketleft": 0xDB, "[": 0xDB, "bracketright": 0xDD, "]": 0xDD, "backslash": 0xDC, "\\": 0xDC,
}
NAMED.update({f"f{i}": 0x6F + i for i in range(1, 25)})

EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}


def name_to_vk(name):
    n = name.strip()
    low = n.lower()
    if low in MODIFIERS:
        return MODIFIERS[low]
    if low in NAMED:
        return NAMED[low]
    if len(n) == 1:
        if n.isalnum():
            return ord(n.upper())
        r = w.VkKeyScanW(n)
        if r != -1:
            return r & 0xFF
    raise ValueError(f"unknown key name: {name!r}")


def parse_combo(combo):
    """'ctrl+shift+p' -> ([VK_CONTROL, VK_SHIFT], [ord('P')]). Space-separated sequences are not supported."""
    parts = [p for p in combo.replace(" ", "").split("+")] if combo.strip() != "+" else ["+"]
    if combo.endswith("++"):
        parts = parts[:-2] + ["+"]
    mods, keys = [], []
    for p in parts:
        if not p:
            continue
        vk = name_to_vk(p)
        (mods if p.lower() in MODIFIERS else keys).append(vk)
    if not keys and mods:          # bare modifier press, e.g. "alt"
        keys.append(mods.pop())
    return mods, keys


def vk_info(vk):
    """(scan code, extended flag) for building WM_KEY* lParams."""
    return w.MapVirtualKeyW(vk, 0), (1 if vk in EXTENDED else 0)
