import sys
import os
import importlib
import pytest

SCRIPTS = [
    "patch_utils",
    "send_telegram"
]

@pytest.mark.parametrize("script", SCRIPTS)
def test_no_side_effects_on_import(script, mocker):
    m_open = mocker.patch("builtins.open", mocker.mock_open())
    m_os_remove = mocker.patch("os.remove", create=True)
    m_urllib = mocker.patch("urllib.request.urlopen", create=True)
    m_subprocess = mocker.patch("subprocess.check_output", create=True)

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    if script in sys.modules:
        importlib.reload(sys.modules[script])
    else:
        importlib.import_module(script)

    m_open.assert_not_called()
    m_os_remove.assert_not_called()
    m_urllib.assert_not_called()
    m_subprocess.assert_not_called()
