import pytest

from supervisor import testing


@pytest.fixture
def layout(tmp_path):
    return testing.project(tmp_path, plugins=["local_llm"], routing={"plugins": {"local_llm": {}}})
