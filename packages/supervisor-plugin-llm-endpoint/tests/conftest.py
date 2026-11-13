import sys
from pathlib import Path

import pytest

# When this package's own repository (not an installed wheel) sits beside its tests, prefer it over whatever
# "supervisor" an ambient site-packages happens to have (an editable install pointed at a different checkout,
# say): the tests must check the code they ship beside, not a stray one. Harmless once ci_package.py has
# copied only tests/ next to an installed wheel (parents[3] is then outside any framework checkout and the
# insert is a no-op path that resolves nothing).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if (_REPO_ROOT / "supervisor" / "__init__.py").is_file() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from supervisor import testing


@pytest.fixture
def layout(tmp_path):
    return testing.project(tmp_path, plugins=["llm_endpoint"], routing={"plugins": {"llm_endpoint": {}}})
