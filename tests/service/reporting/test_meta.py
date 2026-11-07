# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from chrys.service.reporting.meta import (
    MAX_REPOSITORY_IDENTITIES,
    MetaEnvironment,
    ReportMeta,
    primary_cwd_digest,
    session_meta_document,
)
from chrys.service.reporting.repository import (
    URL_KIND_ORIGIN,
    RepositoryIdentity,
    parse_remote_url,
    probe_repository,
    repository_reason,
)


def test_the_meta_document_is_the_session_slash_one_whitelist() -> None:
    meta = ReportMeta(
        session_id="session-1",
        title="Fix the parser",
        agent_profile="reviewer",
        model_provider="anthropic",
        model_id="claude",
        parent_session_id="session-0",
        primary_cwd="/workspace/repo",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:01:00Z",
        schema_version=3,
        turn_count=2,
        message_count=9,
        total_tokens=1234,
        repository_identities=("a", "b", "a"),
    )

    document = session_meta_document(
        meta,
        MetaEnvironment(agent_config_sha256="cfg", agent_config_size=10, workspace_kind="git"),
    )

    assert document["schema"] == "session/1.0"
    assert document["sessionId"] == "session-1"
    assert document["primaryCwdDigest"] == primary_cwd_digest("/workspace/repo")
    assert document["repositoryIdentities"] == ["a", "b"]
    assert document["environment"] == {
        "agentConfigSha256": "cfg",
        "agentConfigSize": 10,
        "workspaceKind": "git",
    }
    assert "/workspace/repo" not in json.dumps(document)


def test_an_unknown_workspace_kind_is_not_reported() -> None:
    document = session_meta_document(ReportMeta(session_id="s"), MetaEnvironment(workspace_kind="svn"))

    assert document["environment"]["workspaceKind"] == ""


def test_the_identity_list_is_bounded() -> None:
    identities = tuple(f"identity-{index}" for index in range(MAX_REPOSITORY_IDENTITIES + 5))

    document = session_meta_document(ReportMeta(session_id="s", repository_identities=identities), MetaEnvironment())

    assert len(document["repositoryIdentities"]) == MAX_REPOSITORY_IDENTITIES


def test_the_digest_is_stable_and_injective_for_a_path() -> None:
    assert primary_cwd_digest("") == ""
    assert primary_cwd_digest("/a/b") == primary_cwd_digest("/a/b")
    assert len(primary_cwd_digest("/a/b")) == 32
    assert primary_cwd_digest("/a/b") != primary_cwd_digest("/a/c")


def test_the_identity_payload_is_the_repository_slash_one_document() -> None:
    identity = RepositoryIdentity(
        identity="repo-1",
        host="example.com",
        path="/owner/repo",
        url_kind=URL_KIND_ORIGIN,
        remote_digest="digest",
        observed_at="2026-01-01T00:00:00Z",
    )

    document = json.loads(identity.payload())

    assert document["schema"] == "repository/1.0"
    assert document["repositoryIdentity"] == "repo-1"
    assert document["remote"] == {"host": "example.com", "path": "/owner/repo", "urlKind": "origin"}
    assert document["remoteDigest"] == "digest"


def test_a_git_repository_is_probed_for_its_identity(tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
    repo = git_repo_factory(tmp_path / "repo")

    probe = probe_repository(str(repo))

    assert probe.workspace_kind == "git"
    assert Path(probe.root) == repo
    assert probe.head_sha
    assert probe.identity is not None
    assert len(probe.identity.identity) == 64
    assert repository_reason(probe) == ""


def test_a_directory_outside_git_is_classified_as_non_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    probe = probe_repository(str(plain))

    assert probe.workspace_kind == "non_git"
    assert probe.identity is None
    assert repository_reason(probe) == "not_a_repository"


def test_an_empty_working_directory_is_no_repository() -> None:
    probe = probe_repository("")

    assert probe.workspace_kind == "no_repo"
    assert repository_reason(probe) == "no_cwd"


def test_remote_urls_split_into_host_and_path() -> None:
    assert parse_remote_url("git@example.com:owner/repo.git") == ("example.com", "/owner/repo.git")
    assert parse_remote_url("https://example.com/owner/repo.git") == ("example.com", "/owner/repo.git")
    assert parse_remote_url("/srv/git/repo.git") == ("", "/srv/git/repo.git")
    assert parse_remote_url("") == ("", "")


def test_the_meta_document_names_this_build() -> None:
    from chrys import __version__

    document = session_meta_document(ReportMeta(session_id="s"), MetaEnvironment())

    assert document["appVersion"] == __version__
