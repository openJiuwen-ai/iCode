# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build-local skill discovery and the independent catalogs it initializes."""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import weakref
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.service.profiles.agents.schema import SkillConfig, SkillsConfig
from chrys.service.skills import discovery as discovery_module
from chrys.service.skills import provider as provider_module
from chrys.service.skills.adapter import create_skills_provider
from chrys.service.skills.discovery import SkillDiscoveryScope
from tests.support.waiting import wait_for


def _write_skill(root: Path, name: str = "demo", description: str = "Initial description") -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nmetadata:\n  owner: original\n---\nUse this skill.\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def skill_tree(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    folder = _write_skill(root)
    (folder / "guide.md").write_text("Resource text", encoding="utf-8")
    (folder / "run.py").write_text("print('unused')", encoding="utf-8")
    return root


def _config(root: Path, *, inline: str = "") -> SkillsConfig:
    return SkillsConfig(
        paths=[str(root)],
        auto_load_user_agents_skills=False,
        auto_load_cwd_agents_skills=False,
        inline=[] if not inline else [SkillConfig(name=inline, description=inline, instructions=inline)],
    )


async def test_one_build_discovers_and_stages_files_once_but_copies_every_mutable_layer(
    skill_tree: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=discovery_module.__name__)
    scan = create_autospec(discovery_module.discover_file_skills, side_effect=discovery_module.discover_file_skills)
    stat = create_autospec(provider_module._file_signature, side_effect=provider_module._file_signature)
    monkeypatch.setattr(discovery_module, "discover_file_skills", scan)
    monkeypatch.setattr(provider_module, "_file_signature", stat)
    async with SkillDiscoveryScope() as scope:
        providers = []
        for index in range(4):
            provider, warnings = await create_skills_provider(
                _config(skill_tree, inline=f"private-{index}"), discovery_scope=scope
            )
            assert provider is not None and warnings == []
            assert provider.skill_names() == ["demo", f"private-{index}"]
            providers.append(provider)

    assert scan.call_count == 1
    assert stat.call_count == 3  # SKILL.md, guide.md, run.py, once for this snapshot.
    first, second, third, fourth = [provider._skills[0] for provider in providers]
    assert len({id(skill) for skill in (first, second, third, fourth)}) == 4
    assert first.content is second.content
    assert first.revision == second.revision == third.revision != ""
    assert first.resources is not second.resources
    assert first.resources[0] is not second.resources[0]
    assert first.scripts is not second.scripts
    assert first.scripts[0] is not second.scripts[0]
    assert first.metadata is not second.metadata
    first.resources[0].description = "changed"
    first.scripts[0].description = "changed"
    assert first.metadata is not None
    first.metadata["owner"] = "changed"
    assert second.resources[0].description is None
    assert second.scripts[0].description is None
    assert second.metadata == {"owner": "original"}
    scans = [record for record in caplog.records if record.msg.startswith("Skill discovery scan:")]
    assert len(scans) == 1
    assert scans[0].args[:3] == ("discovered", 1, 0)
    assert scans[0].args[3] >= 0
    assert caplog.messages.count("Reusing this build's skill discovery") == 3
    assert all(str(skill_tree) not in record.getMessage() for record in scans)


async def test_next_refresh_reads_changes_and_does_not_retain_the_build_scope(skill_tree: Path) -> None:
    scope = SkillDiscoveryScope()
    scope_ref = weakref.ref(scope)
    async with scope:
        first, _ = await create_skills_provider(_config(skill_tree), discovery_scope=scope)
        second, _ = await create_skills_provider(_config(skill_tree), discovery_scope=scope)
    assert first is not None and second is not None
    del scope
    await asyncio.to_thread(gc.collect)
    assert scope_ref() is None
    original_revision = first._skills[0].revision
    await asyncio.to_thread(_write_skill, skill_tree, description="Changed description")
    await asyncio.to_thread(_write_skill, skill_tree, name="added")
    await asyncio.to_thread((skill_tree / "demo" / "guide.md").unlink)

    staged = await first.stage_context_refresh()
    assert first.skill_names() == second.skill_names() == ["demo"]
    first.commit_context_refresh(staged)
    assert set(first.skill_names()) == {"demo", "added"}
    demo = next(skill for skill in first._skills if skill.name == "demo")
    assert demo.description == "Changed description"
    assert demo.revision != original_revision
    assert demo.resources == []
    assert second._skills[0].description == "Initial description"
    assert second._skills[0].revision == original_revision
    await second.refresh_context()
    assert set(second.skill_names()) == {"demo", "added"}
    await asyncio.to_thread((skill_tree / "demo" / "SKILL.md").unlink)
    await first.refresh_context()
    assert first.skill_names() == ["added"]


async def test_ordered_paths_and_script_extensions_are_separate_discovery_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    await asyncio.to_thread(_write_skill, left, description="Left wins")
    right_skill = await asyncio.to_thread(_write_skill, right, description="Right wins")
    await asyncio.to_thread((right_skill / "run.py").write_text, "unused", encoding="utf-8")
    scan = create_autospec(discovery_module.discover_file_skills, side_effect=discovery_module.discover_file_skills)
    monkeypatch.setattr(discovery_module, "discover_file_skills", scan)
    async with SkillDiscoveryScope() as scope:
        a, _ = await scope.discover([str(left), str(right)], script_extensions=[".py"])
        b, _ = await scope.discover([str(right), str(left)], script_extensions=[".py"])
        c, _ = await scope.discover([str(right), str(left)], script_extensions=[".sh"])
        d, _ = await scope.discover([str(right), str(left)], script_extensions=[".py"])
    assert scan.call_count == 3
    assert a[0].description == "Left wins"
    assert b[0].description == c[0].description == d[0].description == "Right wins"
    assert [script.name for script in b[0].scripts] == ["run.py"]
    assert c[0].scripts == []


async def test_discovery_failure_is_shared_until_a_new_build(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=discovery_module.__name__)
    scan = create_autospec(discovery_module.discover_file_skills, side_effect=OSError("synthetic failure"))
    monkeypatch.setattr(discovery_module, "discover_file_skills", scan)
    async with SkillDiscoveryScope() as scope:
        for _ in range(3):
            with pytest.raises(OSError, match="synthetic failure"):
                await scope.discover(["unused"], script_extensions=[])
    assert scan.call_count == 1
    scan.side_effect = None
    scan.return_value = ([], [])
    async with SkillDiscoveryScope() as scope:
        assert await scope.discover(["unused"], script_extensions=[]) == ([], [])
    assert scan.call_count == 2
    scans = [record for record in caplog.records if record.msg.startswith("Skill discovery scan:")]
    assert [record.args[0] for record in scans] == ["failed", "discovered"]
    assert caplog.messages.count("Reusing this build's skill discovery") == 2


async def test_invalid_skill_warnings_belong_to_each_provider(skill_tree: Path) -> None:
    await asyncio.to_thread((skill_tree / "demo" / "SKILL.md").write_text, "broken", encoding="utf-8")
    async with SkillDiscoveryScope() as scope:
        first, first_warnings = await create_skills_provider(_config(skill_tree), discovery_scope=scope)
        second, second_warnings = await create_skills_provider(_config(skill_tree), discovery_scope=scope)
    assert first is not None and second is not None
    assert len(first_warnings) == len(second_warnings) == 1
    assert first_warnings == second_warnings
    assert await first.refresh_context() == []
    assert await second.refresh_context() == []
    await asyncio.to_thread(_write_skill, skill_tree, description="Repaired")
    await first.refresh_context()
    assert first.skill_names() == ["demo"]
    assert second.skill_names() == []


async def test_cancelled_waiter_does_not_cancel_a_shared_scan(
    skill_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = discovery_module._discover

    def held(key: discovery_module._DiscoveryKey) -> discovery_module._Snapshot:
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release discovery")
        return original(key)

    scan = create_autospec(original, side_effect=held)
    monkeypatch.setattr(discovery_module, "_discover", scan)
    second_entered = asyncio.Event()
    async with SkillDiscoveryScope() as scope:
        first = asyncio.create_task(scope.discover([str(skill_tree)], script_extensions=[".py"]))

        async def second_consumer():
            second_entered.set()
            return await scope.discover([str(skill_tree)], script_extensions=[".py"])

        second = None
        try:
            await wait_for(lambda: entered.is_set() or first.done(), description="skill scan started")
            if first.done():
                await first
            second = asyncio.create_task(second_consumer())
            await wait_for(lambda: second_entered.is_set() or second.done(), description="second scan consumer")
            assert not second.done()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            skills, failures = await second
            assert [skill.name for skill in skills] == ["demo"] and failures == []
            assert scan.call_count == 1
        finally:
            release.set()
            first.cancel()
            if second is not None:
                second.cancel()
            await asyncio.gather(first, *([] if second is None else [second]), return_exceptions=True)


async def test_closing_a_scope_drains_its_thread_even_when_the_waiter_left(
    skill_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = discovery_module._discover

    def held(key: discovery_module._DiscoveryKey) -> discovery_module._Snapshot:
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release discovery")
        try:
            return original(key)
        finally:
            finished.set()

    monkeypatch.setattr(discovery_module, "_discover", held)
    scope = SkillDiscoveryScope()
    consumer = asyncio.create_task(scope.discover([str(skill_tree)], script_extensions=[]))
    closing = None
    try:
        await wait_for(lambda: entered.is_set() or consumer.done(), description="skill scan started")
        if consumer.done():
            await consumer
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        closing = asyncio.create_task(scope.aclose())
        await wait_for(lambda: scope._closed or closing.done(), description="scope close started")
        assert not closing.done()
        with pytest.raises(RuntimeError, match="closed"):
            await scope.discover([str(skill_tree)], script_extensions=[])
        release.set()
        await closing
        assert finished.is_set()
        assert scope._tasks == {}
        await scope.aclose()
    finally:
        release.set()
        consumer.cancel()
        await asyncio.gather(consumer, *([] if closing is None else [closing]), return_exceptions=True)
        await scope.aclose()
