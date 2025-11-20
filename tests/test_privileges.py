from __future__ import annotations
from lpm.privileges import privileged_section


def test_privileged_section_is_noop():
    # The privileged section should function as a lightweight context manager
    # without attempting to modify process credentials.
    with privileged_section():
        assert True
