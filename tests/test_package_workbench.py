"""Exercise the build entrypoint before invoking any real packaging tool."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def invoke(tmp_path, version, existing=False):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if not shell:
        pytest.skip('PowerShell is required for the Windows package entrypoint')
    fake = tmp_path / 'fake-python.ps1'
    fake.write_text("if ($args[0] -eq '-B') { Write-Output $env:XIJIAN_FAKE_VERSION; exit 0 }; Write-Output 'FAKE_BUILD_REACHED'; exit 23", encoding='utf-8')
    output = tmp_path / 'output'
    if existing:
        (output / 'Xijian').mkdir(parents=True)
        (output / 'Xijian' / 'keep.txt').write_text('preserved', encoding='utf-8')
    env = dict(os.environ, XIJIAN_FAKE_VERSION=version)
    result = subprocess.run([shell, '-NoProfile', '-File', str(ROOT / 'tools/package_workbench.ps1'),
        '-Python', str(fake), '-Output', str(output)], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', timeout=20)
    return result, output


@pytest.mark.parametrize('version', ['0.3.0-alpha.5', '0.4.0-dev.1'])
def test_known_release_channels_reach_builder(tmp_path, version):
    result, output = invoke(tmp_path, version)
    assert result.returncode != 0  # Deliberate fake build failure.
    assert 'FAKE_BUILD_REACHED' in result.stdout
    assert not output.exists()


@pytest.mark.parametrize('version', ['../0.4.0-dev.1', '0.4.0-dev.1/escape', '0.4.0', '0.4.0-beta.1'])
def test_invalid_versions_never_reach_builder(tmp_path, version):
    result, output = invoke(tmp_path, version)
    assert result.returncode != 0
    assert 'Invalid application version' in result.stdout
    assert 'FAKE_BUILD_REACHED' not in result.stdout
    assert not output.exists()


def test_existing_output_is_preserved(tmp_path):
    result, output = invoke(tmp_path, '0.4.0-dev.1', existing=True)
    assert result.returncode != 0
    assert 'Output already contains a build' in result.stdout
    assert 'FAKE_BUILD_REACHED' not in result.stdout
    assert (output / 'Xijian' / 'keep.txt').read_text(encoding='utf-8') == 'preserved'