"""Check launcher routing without starting servers or installing dependencies."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if os.name == "nt":
    git = shutil.which("git")
    BASH = str(Path(git).resolve().parents[1] / "bin" / "bash.exe") if git else None
else:
    BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).is_file(), reason="native Bash required")


def run_launcher(tmp_path, setup, *args, **overrides):
    source_dir = tmp_path / "source tree"
    source_dir.mkdir()
    launcher = source_dir / "dev.sh"
    shutil.copyfile(ROOT / "dev.sh", launcher)
    env = os.environ.copy()
    for name in ("WSL_DISTRO_NAME", "WSL_INTEROP", "BASH_ENV"):
        env.pop(name, None)
    env.update(overrides)
    # Never let an old/broken launcher reach dependency installation or port cleanup.
    guard = '''
command() { if [[ "$*" == '-v uv' ]]; then return 1; fi; builtin command "$@"; }
'''
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", guard + setup + '\nsource "$1" "${@:2}"',
         "test-dev", launcher.as_posix(), *args],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )


@pytest.mark.parametrize("wsl_env", [{"WSL_DISTRO_NAME": "Ubuntu"}, {"WSL_INTEROP": "/run/WSL/1_interop"}, {}])
def test_rejects_wsl_before_dependencies(tmp_path, wsl_env):
    kernel = "Linux" if wsl_env else "Linux microsoft-standard-WSL2"
    result = run_launcher(tmp_path, f'uname() {{ printf "%s\\n" "{kernel}"; }}', **wsl_env)
    assert result.returncode == 1
    assert "WSL" in result.stderr
    assert "dev.ps1" in result.stderr
    assert "uv" not in result.stderr


@pytest.mark.parametrize("platform", ["MINGW64_NT", "MSYS_NT", "CYGWIN_NT"])
def test_windows_delegates_to_native_launcher(tmp_path, platform):
    result = run_launcher(tmp_path, '''
uname() { printf '%s\\n' "$TEST_PLATFORM"; }
powershell.exe() { :; }
cygpath() { printf 'C:/source tree/dev.ps1\\n'; }
exec() { printf '%s\\n' "$@"; printf 'ports=%s,%s\\n' "$BACKEND_PORT" "$FRONTEND_PORT"; exit 37; }
''', "-BackendPort", "8000", TEST_PLATFORM=platform, BACKEND_PORT="8001", FRONTEND_PORT="5173")
    assert result.returncode == 37
    assert result.stdout.splitlines() == [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        "C:/source tree/dev.ps1", "-BackendPort", "8000", "ports=8001,5173",
    ]


def test_windows_missing_powershell_fails_without_unix_fallback(tmp_path):
    result = run_launcher(tmp_path, '''
uname() { printf '%s\\n' MSYS_NT; }
command() { if [[ "$*" == '-v powershell.exe' || "$*" == '-v uv' ]]; then return 1; fi; builtin command "$@"; }
''')
    assert result.returncode == 1
    assert "powershell.exe" in result.stderr
    assert "uv" not in result.stderr


@pytest.mark.parametrize("platform", ["Linux", "Darwin"])
def test_unix_keeps_source_launcher(tmp_path, platform):
    result = run_launcher(tmp_path, f'''
uname() {{ printf '%s\\n' {platform}; }}
command() {{ if [[ "$*" == '-v uv' ]]; then return 1; fi; builtin command "$@"; }}
''')
    assert result.returncode == 1
    assert "uv" in result.stderr
    assert "WSL" not in result.stderr
