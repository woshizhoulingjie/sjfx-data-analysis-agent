import os
from pathlib import Path
import pytest
from services.scanner import local_host_path


def test_local_absolute_path_is_the_host_filesystem(tmp_path):
    assert local_host_path(str(tmp_path)) == tmp_path


@pytest.mark.skipif(os.name != 'nt', reason='Windows host case')
def test_windows_rejects_drive_relative_and_device_namespace():
    for name in ('C:documents', 'relative/data', r'\\.\PhysicalDrive0'):
        with pytest.raises(ValueError):
            local_host_path(name)


@pytest.mark.skipif(os.name == 'nt', reason='Linux host case')
def test_remote_linux_cannot_treat_client_windows_path_as_local():
    for name in (r'C:\documents\files', r'\\desktop\files'):
        with pytest.raises(ValueError, match='另一台电脑'):
            local_host_path(name)
