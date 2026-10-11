import pytest

from .flapping import Rig


@pytest.fixture
def rig_factory(tmp_path):
    made = []

    def make(script, **kwargs):
        rig = Rig(tmp_path, script, **kwargs)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.close()
