import pytest

from supervisor import testing


@pytest.fixture
def layout(tmp_path):
    return testing.project(tmp_path, plugins=["llm_endpoint"], routing={"plugins": {"llm_endpoint": {}}})
