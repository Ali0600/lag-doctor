"""The declared-absence gate (tests/capabilities.py): CI names what its runner lacks; the laptop names nothing."""
from __future__ import annotations

import pytest

import conftest
from tests import capabilities as cap


class FakeItem:
    """The two methods the gate uses on a collected test."""

    def __init__(self, *needs):
        self.marks = [pytest.mark.needs(*names).mark for names in needs]
        self.added = []

    def iter_markers(self, name):
        return [m for m in self.marks if m.name == name]

    def add_marker(self, marker):
        self.added.append(marker)


def reasons(item):
    return [m.mark.kwargs["reason"] for m in item.added if m.mark.name == "skip"]


def test_nothing_declared_means_nothing_is_absent():
    assert cap.absent_capabilities({}) == set()
    assert cap.absent_capabilities({cap.ABSENT_ENV: " , "}) == set()


def test_the_declared_names_are_absent():
    assert cap.absent_capabilities({cap.ABSENT_ENV: "gpu, internet_icmp"}) == {"gpu", "internet_icmp"}


def test_an_unknown_declared_name_is_a_usage_error():
    with pytest.raises(pytest.UsageError, match="gpus"):
        cap.absent_capabilities({cap.ABSENT_ENV: "gpu,gpus"})


def test_a_test_needing_an_absent_capability_skips_with_what_is_missing():
    reason = cap.skip_reason(["display", "gpu"], {"gpu"})
    assert reason == f"this machine has no {cap.CAPABILITIES['gpu']} ({cap.ABSENT_ENV})"


def test_a_test_needing_only_what_is_there_runs():
    assert cap.skip_reason(["gpu"], {"display"}) is None
    assert cap.skip_reason([], set(cap.CAPABILITIES)) is None


def test_an_unknown_marker_name_fails_even_when_nothing_is_absent():
    with pytest.raises(pytest.UsageError, match="gpus"):
        cap.skip_reason(["gpus"], set())


def test_the_conftest_hook_skips_exactly_the_tests_that_need_what_ci_lacks(monkeypatch):
    gpu, both, plain, display = FakeItem(("gpu",)), FakeItem(("display",), ("gpu",)), FakeItem(), FakeItem(("display",))
    monkeypatch.setenv(cap.ABSENT_ENV, "gpu")
    conftest.pytest_collection_modifyitems(None, [gpu, both, plain, display])
    assert reasons(gpu) == reasons(both) == [f"this machine has no {cap.CAPABILITIES['gpu']} ({cap.ABSENT_ENV})"]
    assert reasons(plain) == reasons(display) == []


def test_the_conftest_hook_skips_nothing_on_the_laptop(monkeypatch):
    items = [FakeItem((name,)) for name in cap.CAPABILITIES]
    monkeypatch.delenv(cap.ABSENT_ENV, raising=False)
    conftest.pytest_collection_modifyitems(None, items)
    assert all(not item.added for item in items)
