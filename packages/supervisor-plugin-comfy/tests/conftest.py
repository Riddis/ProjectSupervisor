import pytest

from supervisor import testing


@pytest.fixture
def layout(tmp_path):
    return testing.project(tmp_path, plugins=["comfy"], routing={"plugins": {"comfy": {}}})
