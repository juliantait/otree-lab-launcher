"""Tests for the participant-PC kiosk shortcut bundle (review feature D).

Covers the stdlib ``.lnk`` writer in ``otree_core`` and the per-lab bundle:

* a single generated ``.lnk`` is PARSED BACK from its raw bytes to assert the
  magic header (HeaderSize 0x0000004C + the Shell Link CLSID), the stored target
  browser exe path, and the command-line Arguments string carrying the expected
  kiosk URL for a seat;
* a full bundle for a lab writes one correctly-named ``.lnk`` per seat inside the
  ``"<lab> participant PC shortcuts"`` folder.

Pure stdlib + ``otree_core`` (no tkinter, no display), so it runs in the same
one-process ``pytest tests/`` run as the other committed suites.

NB: a hand-built ``.lnk`` cannot be *behaviour*-tested off Windows -- whether the
shortcut actually opens Edge in kiosk full-screen must be confirmed by Julian on a
real Windows lab PC. These tests prove the bytes are a well-formed Shell Link
carrying the right target + arguments, which is what we can verify here.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import otree_core as core  # noqa: E402


# ---------------------------------------------------------------------------
# A minimal [MS-SHLLINK] parser -- just enough to read back what the writer put
# in: the header, the LinkInfo LocalBasePath, and the Unicode StringData blocks.
# ---------------------------------------------------------------------------

# LinkFlags bits (mirrors the writer's private constants).
_HAS_LINK_TARGET_IDLIST = 0x00000001
_HAS_LINK_INFO = 0x00000002
_HAS_NAME = 0x00000004
_HAS_RELATIVE_PATH = 0x00000008
_HAS_WORKING_DIR = 0x00000010
_HAS_ARGUMENTS = 0x00000020
_HAS_ICON_LOCATION = 0x00000040
_IS_UNICODE = 0x00000080


def parse_lnk(data):
    """Parse a subset of a Shell Link into a dict for assertions."""
    out = {}
    out["header_size"] = struct.unpack_from("<I", data, 0)[0]
    out["clsid"] = data[4:20]
    flags = struct.unpack_from("<I", data, 20)[0]
    out["flags"] = flags
    out["show_command"] = struct.unpack_from("<I", data, 60)[0]

    pos = 76  # end of the fixed header

    # We never emit a LinkTargetIDList; if a future writer does, skip it so the
    # rest of the parse still lines up.
    if flags & _HAS_LINK_TARGET_IDLIST:
        idlist_size = struct.unpack_from("<H", data, pos)[0]
        pos += 2 + idlist_size

    # LinkInfo: read the LocalBasePath (ANSI, null-terminated).
    if flags & _HAS_LINK_INFO:
        link_info_start = pos
        link_info_size = struct.unpack_from("<I", data, pos)[0]
        local_base_path_offset = struct.unpack_from("<I", data, pos + 16)[0]
        abs_off = link_info_start + local_base_path_offset
        end = data.index(b"\x00", abs_off)
        out["target"] = data[abs_off:end].decode("latin-1")
        pos = link_info_start + link_info_size

    def read_string():
        nonlocal pos
        count = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        raw = data[pos:pos + count * 2]
        pos += count * 2
        return raw.decode("utf-16-le")

    # StringData, in the spec's fixed order.
    if flags & _HAS_NAME:
        out["name"] = read_string()
    if flags & _HAS_RELATIVE_PATH:
        out["relative_path"] = read_string()
    if flags & _HAS_WORKING_DIR:
        out["working_dir"] = read_string()
    if flags & _HAS_ARGUMENTS:
        out["arguments"] = read_string()
    if flags & _HAS_ICON_LOCATION:
        out["icon"] = read_string()
    return out


# ---------------------------------------------------------------------------
# (i) a single .lnk: parse it back and check header + target + arguments
# ---------------------------------------------------------------------------

def test_lnk_magic_header():
    data = core.build_windows_lnk(r"C:\x\y.exe", arguments='--kiosk "u"')
    parsed = parse_lnk(data)
    # The "magic header": a 0x0000004C HeaderSize immediately followed by the
    # 16-byte Shell Link CLSID {00021401-0000-0000-C000-000000000046}.
    assert parsed["header_size"] == 0x0000004C
    assert parsed["clsid"] == core.SHELL_LINK_CLSID
    assert parsed["clsid"] == (b"\x01\x14\x02\x00\x00\x00\x00\x00"
                               b"\xc0\x00\x00\x00\x00\x00\x00\x46")
    assert parsed["flags"] & _IS_UNICODE
    assert parsed["flags"] & _HAS_LINK_INFO
    # We deliberately do NOT ship a LinkTargetIDList (it would encode the
    # generating machine's shell namespace, wrong for another PC).
    assert not (parsed["flags"] & _HAS_LINK_TARGET_IDLIST)


def test_lnk_stores_target_exe_and_arguments():
    exe = core.PARTICIPANT_BROWSER_EXE
    url = core.participant_seat_url("192.168.0.9", "8000", "study", "s7")
    args = core.participant_kiosk_arguments(url)
    data = core.build_windows_lnk(exe, arguments=args, working_dir=r"C:\dir",
                                  description="Experiment - s7", icon_path=exe)
    parsed = parse_lnk(data)
    assert parsed["target"] == exe
    assert parsed["arguments"] == args
    assert parsed["working_dir"] == r"C:\dir"
    assert parsed["name"] == "Experiment - s7"
    assert parsed["icon"] == exe
    # SW_SHOWNORMAL.
    assert parsed["show_command"] == 1


def test_seat_lnk_arguments_carry_kiosk_url():
    data = core.build_participant_seat_lnk("10.0.0.2", "8000", "study", "seat_3")
    parsed = parse_lnk(data)
    assert parsed["target"] == core.PARTICIPANT_BROWSER_EXE
    args = parsed["arguments"]
    # The exact per-seat link, quoted, with the kiosk flags around it.
    expected_url = ("http://10.0.0.2:8000/room/study"
                    "?participant_label=seat_3&welcome_page_ok=1")
    assert expected_url in args
    assert args.startswith('--kiosk "')
    assert "--edge-kiosk-type=fullscreen" in args
    assert 'welcome_page_ok=1' in args


def test_kiosk_flags_are_easy_to_change_constants():
    # Chrome-style override: no edge flag, chrome exe.
    url = core.participant_seat_url("h", "8000", "study", "s1")
    args = core.participant_kiosk_arguments(url, extra_flags="")
    assert args == '--kiosk "%s"' % url
    data = core.build_participant_seat_lnk(
        "h", "8000", "study", "s1",
        browser_exe=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    parsed = parse_lnk(data)
    assert parsed["target"].endswith("chrome.exe")


# ---------------------------------------------------------------------------
# (ii) the full bundle: one correctly-named .lnk per seat, in the right folder
# ---------------------------------------------------------------------------

def test_bundle_writes_one_lnk_per_seat(tmp_path):
    seats = ["s1", "s2", "s10", "A_1"]
    result = core.export_participant_shortcuts(
        str(tmp_path), "Small Lab", "192.168.1.5", seats,
        room="study", shortcut_label="Experiment")
    assert result["ok"] is True
    assert result["count"] == 4

    folder = os.path.join(str(tmp_path), "Small Lab participant PC shortcuts")
    assert result["folder"] == folder
    assert os.path.isdir(folder)

    files = sorted(os.listdir(folder))
    assert files == sorted([
        "Experiment - s1.lnk", "Experiment - s2.lnk",
        "Experiment - s10.lnk", "Experiment - A_1.lnk"])

    # Each is a real Shell Link whose arguments carry that seat's link.
    for seat in seats:
        path = os.path.join(folder, "Experiment - %s.lnk" % seat)
        parsed = parse_lnk(open(path, "rb").read())
        assert parsed["header_size"] == 0x0000004C
        assert parsed["clsid"] == core.SHELL_LINK_CLSID
        assert ("participant_label=%s&welcome_page_ok=1" % seat) in parsed["arguments"]


def test_bundle_falls_back_to_lab_name_when_no_shortcut_label(tmp_path):
    result = core.export_participant_shortcuts(
        str(tmp_path), "Rotterdam Annex", "host", ["x1"], shortcut_label="")
    assert result["ok"] is True
    files = os.listdir(result["folder"])
    assert files == ["Rotterdam Annex - x1.lnk"]


def test_bundle_folder_name_uses_the_lab_name(tmp_path):
    core.export_participant_shortcuts(str(tmp_path), "Large Lab", "h", ["s1"])
    assert os.path.isdir(os.path.join(str(tmp_path), "Large Lab participant PC shortcuts"))


def test_bundle_default_port_is_8000(tmp_path):
    result = core.export_participant_shortcuts(str(tmp_path), "Lab", "h", ["s1"])
    parsed = parse_lnk(open(result["files"][0], "rb").read())
    assert ":8000/room/" in parsed["arguments"]


def test_bundle_custom_port(tmp_path):
    result = core.export_participant_shortcuts(
        str(tmp_path), "Lab", "h", ["s1"], port="9001")
    parsed = parse_lnk(open(result["files"][0], "rb").read())
    assert ":9001/room/" in parsed["arguments"]


def test_bundle_sanitizes_illegal_filename_characters(tmp_path):
    # A shortcut label with characters Windows forbids in a filename.
    result = core.export_participant_shortcuts(
        str(tmp_path), "My/Lab:2", "h", ["s1"], shortcut_label='Exp*t?')
    assert result["ok"] is True
    folder_name = os.path.basename(result["folder"])
    fname = os.path.basename(result["files"][0])
    for bad in '<>:"/\\|?*':
        assert bad not in folder_name
        assert bad not in fname


# ---------------------------------------------------------------------------
# Guard rails: empty inputs / bad destination fail cleanly (no exception)
# ---------------------------------------------------------------------------

def test_bundle_rejects_no_seats(tmp_path):
    result = core.export_participant_shortcuts(str(tmp_path), "Lab", "h", [])
    assert result["ok"] is False
    assert "no seats" in result["message"].lower()


def test_bundle_rejects_no_host(tmp_path):
    result = core.export_participant_shortcuts(str(tmp_path), "Lab", "", ["s1"])
    assert result["ok"] is False
    assert "host" in result["message"].lower()


def test_bundle_rejects_missing_destination():
    result = core.export_participant_shortcuts(
        "/no/such/folder/really", "Lab", "h", ["s1"])
    assert result["ok"] is False
    assert "folder" in result["message"].lower()
