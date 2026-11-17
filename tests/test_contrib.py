import shutil
import subprocess
from pathlib import Path

CONTRIB = Path(__file__).resolve().parent.parent / "contrib"


def test_openrc_script_syntax() -> None:
    sh = shutil.which("sh")
    assert sh is not None, "sh is required on Linux"
    script = CONTRIB / "openrc" / "vigil"
    result = subprocess.run([sh, "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    text = script.read_text()
    assert text.startswith("#!/usr/bin/openrc-run")
    assert "serve" in text


def test_desktop_file_keys() -> None:
    lines = (CONTRIB / "xdg" / "vigil.desktop").read_text().splitlines()
    assert lines[0] == "[Desktop Entry]"
    assert "Type=Application" in lines
    assert "Exec=vigil serve" in lines
    assert "X-GNOME-Autostart-enabled=true" in lines


def test_systemd_unit_has_execstart() -> None:
    lines = (CONTRIB / "systemd" / "vigil.service").read_text().splitlines()
    exec_starts = [line for line in lines if line.startswith("ExecStart=")]
    assert len(exec_starts) == 1
    assert exec_starts[0].endswith("vigil serve")
    assert "[Service]" in lines
    assert "WantedBy=default.target" in lines
