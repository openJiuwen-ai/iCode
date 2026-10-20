# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-correlated interactive handoff; received means a visible composer draft."""

from __future__ import annotations

import argparse
import asyncio
import errno
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_owner_only_text, read_owner_verified_bounded
from chrys.foundation.util.lock import FileLock

if TYPE_CHECKING:
    from chrys.foundation.events.types import AgentRuntimeDetails
    from chrys.service.profiles.agents.schema import AgentProfile

PROTOCOL_VERSION = 1
MAX_MANIFEST_BYTES = 256_000


class InteractivePreparationError(ValueError):
    """A known preparation failure that did not create a native session."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128_000 or "\x00" in value:
        raise ValueError("Invalid interactive launch text")
    return value


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid interactive launch fields")
    return value


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LaunchSkill:
    name: str
    directory: Path
    content_sha256: str
    required: bool
    source: str
    files: tuple[dict[str, Any], ...]
    origin: dict[str, Any]


@dataclass(frozen=True)
class LaunchManifest:
    request_id: str
    cwd: Path
    mode: str
    session_id: str
    text: str
    text_sha256: str
    skills: tuple[LaunchSkill, ...]
    request_sha256: str
    configuration: dict[str, str]

    @classmethod
    def read(cls, path: Path) -> LaunchManifest:
        if not path.is_absolute():
            raise ValueError("Interactive launch manifest must use an absolute path")
        raw = json.loads(read_owner_verified_bounded(path, max_bytes=MAX_MANIFEST_BYTES))
        keys = {"protocolVersion", "requestId", "cwd", "mode", "context", "skills", "configuration"}
        if isinstance(raw, dict) and raw.get("mode") == "resume":
            keys.add("sessionId")
        value = _object(raw, keys)
        if type(value["protocolVersion"]) is not int or value["protocolVersion"] != PROTOCOL_VERSION:
            raise ValueError("Unsupported interactive launch protocol")
        request_id = _text(value["requestId"])
        if len(request_id) > 160:
            raise ValueError("Interactive launch request id is too long")
        cwd = Path(_text(value["cwd"]))
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("Interactive launch cwd must be an existing absolute directory")
        cwd = cwd.resolve(strict=True)
        if value["mode"] not in {"new", "resume"}:
            raise ValueError("Invalid interactive launch mode")
        context = _object(value["context"], {"text", "sha256"})
        text = _text(context["text"])
        if context["sha256"] != _digest(text):
            raise ValueError("Interactive launch material checksum mismatch")
        configuration = value["configuration"]
        if not isinstance(configuration, dict) or set(configuration) - {
            "role",
            "model",
            "roleFingerprint",
            "modelFingerprint",
        }:
            raise ValueError("Invalid interactive launch configuration")
        configuration = {key: _text(item) for key, item in configuration.items()}
        skills: list[LaunchSkill] = []
        if not isinstance(value["skills"], list) or len(value["skills"]) > 64:
            raise ValueError("Invalid interactive launch skills")
        names: set[str] = set()
        for item in value["skills"]:
            pin = _object(item, {"name", "directory", "contentSha256", "required", "source", "files", "origin"})
            name = _text(pin["name"])
            directory = Path(_text(pin["directory"]))
            digest = _text(pin["contentSha256"])
            if not directory.is_absolute() or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Invalid interactive launch skill pin")
            if type(pin["required"]) is not bool or pin["source"] not in {"repository", "shared"}:
                raise ValueError("Invalid interactive launch skill source")
            if name.casefold() in names:
                raise ValueError("Duplicate interactive launch skill name")
            names.add(name.casefold())
            directory = directory.resolve()
            if directory.is_relative_to(cwd) != (pin["source"] == "repository"):
                raise ValueError("Interactive launch skill scope mismatch")
            files = pin["files"]
            if not isinstance(files, list) or not 1 <= len(files) <= 128:
                raise ValueError("Invalid interactive launch skill file manifest")
            paths: set[str] = set()
            for file in files:
                _object(file, {"path", "sha256", "executable"})
                relative = Path(_text(file["path"]))
                checksum = _text(file["sha256"])
                if relative.is_absolute() or ".." in relative.parts or "\\" in file["path"] or file["path"] in paths:
                    raise ValueError("Invalid interactive launch skill file path")
                if (
                    type(file["executable"]) is not bool
                    or len(checksum) != 64
                    or any(c not in "0123456789abcdef" for c in checksum)
                ):
                    raise ValueError("Invalid interactive launch skill file checksum")
                paths.add(file["path"])
            if "SKILL.md" not in paths:
                raise ValueError("Interactive launch skill manifest has no SKILL.md")
            origin = pin["origin"]
            _object(
                origin,
                {"repositoryId", "relativePath"}
                if pin["source"] == "repository"
                else {"resourceId", "version", "packageSha256", "localResourceId", "installationDigest"},
            )
            if any(not isinstance(item, str) or not item for item in origin.values()):
                raise ValueError("Invalid interactive launch skill origin")
            if pin["source"] == "repository" and (cwd / origin["relativePath"]).resolve() != directory:
                raise ValueError("Interactive launch repository skill origin mismatch")
            skills.append(LaunchSkill(name, directory, digest, pin["required"], pin["source"], tuple(files), origin))
        return cls(
            request_id,
            cwd,
            value["mode"],
            _text(value["sessionId"]) if value["mode"] == "resume" else "",
            text,
            context["sha256"],
            tuple(skills),
            _digest(json.dumps(value, sort_keys=True, separators=(",", ":"))),
            configuration,
        )


class InteractiveLaunch:
    """One durable claim; a crash leaves unknown and cannot create a second session."""

    def __init__(self, manifest: LaunchManifest) -> None:
        self.manifest = manifest
        self.path = receipt_path(manifest.request_id)
        self.receipt: dict[str, Any] = {}

    def claim(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.path.with_suffix(".lock"), timeout=5):
            try:
                existing = read_receipt(self.manifest.request_id)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
                existing = None
            if existing is not None:
                if existing["requestSha256"] != self.manifest.request_sha256:
                    raise ValueError("Interactive launch request id conflicts with different content")
                self.receipt = existing
                return False
            self.receipt = {
                "protocolVersion": PROTOCOL_VERSION,
                "requestId": self.manifest.request_id,
                "requestSha256": self.manifest.request_sha256,
                "cwd": str(self.manifest.cwd),
                "mode": self.manifest.mode,
                "status": "unknown",
                "materials": "not-staged",
                "submitted": "unverified",
                "read": "unverified",
                "adopted": "unverified",
                "skills": [],
                "pins": [
                    {
                        "name": pin.name,
                        "directory": str(pin.directory),
                        "contentSha256": pin.content_sha256,
                        "required": pin.required,
                        "source": pin.source,
                        "files": list(pin.files),
                        "origin": pin.origin,
                    }
                    for pin in self.manifest.skills
                ],
            }
            self._save()
            return True

    def _save(self) -> None:
        atomic_write_owner_only_text(self.path, json.dumps(self.receipt, ensure_ascii=False))

    def fail(self, message: str, session_id: str | None = None) -> None:
        self.receipt.update(status="failed", error=message)
        if session_id:
            self.receipt["sessionId"] = session_id
        self._save()

    def verify_skills(self, details: AgentRuntimeDetails, *, project_enabled: bool, profile: AgentProfile) -> None:
        reports: list[dict[str, Any]] = []
        for pin in self.manifest.skills:
            actual = next(
                (skill for skill in details.skill_details if skill.name.casefold() == pin.name.casefold()), None
            )
            status = "verified"
            if actual is None or (
                pin.source == "repository" and (not project_enabled or not profile.skills.auto_load_cwd_agents_skills)
            ):
                status = "unavailable"
            elif not actual.file_based or Path(actual.source).resolve() != pin.directory:
                status = "shadowed"
            elif actual.content_sha256 != pin.content_sha256:
                status = "content-mismatch"
            if status == "verified":
                status = _verify_files(pin)
            reports.append(
                {
                    "origin": pin.origin,
                    "name": pin.name,
                    "status": status,
                    "source": actual.source if actual else None,
                    "contentSha256": actual.content_sha256 if actual else None,
                    "reading": "unverified",
                }
            )
        self.receipt["skills"] = reports
        if any(
            pin.required and report["status"] != "verified"
            for pin, report in zip(self.manifest.skills, reports, strict=True)
        ):
            raise ValueError("A required interactive launch skill is unavailable or differs from its pin")

    def observe_session(self, session_id: str, cwd: str) -> None:
        """Persist the acquired identity even if later material staging crashes."""
        if not session_id or Path(cwd).resolve() != self.manifest.cwd:
            raise ValueError("Interactive launch native session identity mismatch")
        if self.manifest.mode == "resume" and session_id != self.manifest.session_id:
            raise ValueError("Interactive launch restored a different session")
        self.receipt.update(sessionId=session_id, phase="session-acquired", observedAt=datetime.now(UTC).isoformat())
        self._save()

    def confirm(self, session_id: str, cwd: str) -> None:
        if not session_id or Path(cwd).resolve() != self.manifest.cwd:
            raise ValueError("Interactive launch native session identity mismatch")
        if self.manifest.mode == "resume" and session_id != self.manifest.session_id:
            raise ValueError("Interactive launch restored a different session")
        self.receipt.update(
            status="confirmed",
            phase="composer-staged",
            sessionId=session_id,
            materials="composer-staged",
            stagedAt=datetime.now(UTC).isoformat(),
            contextSha256=self.manifest.text_sha256,
            materialSurface="composer-draft",
        )
        self._save()


def _verify_files(pin: LaunchSkill) -> str:
    """Compare every installed byte, executable bit and member, including attachments."""
    actual: dict[str, dict[str, Any]] = {}
    total = 0

    def walk_error(error: OSError) -> None:
        raise error

    for root, directories, filenames in os.walk(pin.directory, followlinks=False, onerror=walk_error):
        if any((Path(root) / name).is_symlink() for name in directories):
            return "content-mismatch"
        for name in filenames:
            file = Path(root) / name
            relative = file.relative_to(pin.directory).as_posix()
            if relative == ".teams-resource.json" and pin.source == "shared":
                continue
            if file.is_symlink() or not file.is_file():
                return "content-mismatch"
            total += file.stat().st_size
            if total > 2 * 1024 * 1024 or len(actual) >= 128:
                return "content-mismatch"
            actual[relative] = {
                "path": relative,
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "executable": bool(file.stat().st_mode & 0o111),
            }
    if actual != {file["path"]: file for file in pin.files}:
        return "content-mismatch"
    if pin.source == "shared":
        marker = json.loads((pin.directory / ".teams-resource.json").read_text(encoding="utf-8"))
        if marker != {
            "schemaVersion": 1,
            "resourceId": pin.origin["localResourceId"],
            "digest": pin.origin["installationDigest"],
        }:
            return "source-mismatch"
    return "verified"


def receipt_path(request_id: str) -> Path:
    return get_platform().config_dir / "interactive-launches" / f"{_digest(_text(request_id))}.json"


def read_receipt(request_id: str) -> dict[str, Any]:
    value = json.loads(read_owner_verified_bounded(receipt_path(request_id), max_bytes=MAX_MANIFEST_BYTES))
    if (
        not isinstance(value, dict)
        or value.get("protocolVersion") != PROTOCOL_VERSION
        or value.get("requestId") != request_id
        or not {"requestSha256", "cwd", "mode", "status", "materials", "skills", "pins"}.issubset(value)
        or value.get("status") not in {"unknown", "confirmed", "failed"}
    ):
        raise ValueError("Invalid interactive launch receipt")
    return value


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="icode launch")
    parser.add_argument("operation", choices=["capabilities", "inspect", "preflight"])
    parser.add_argument("--request-id", default="")
    parser.add_argument("--manifest", default="")
    args = parser.parse_args(argv)
    if args.operation == "capabilities":
        sys.stdout.write(
            json.dumps(
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "interactiveLaunch": True,
                    "strictResume": True,
                    "skillVerification": True,
                    "materialSurface": "composer-draft",
                }
            )
            + "\n"
        )
        return 0
    if args.operation == "preflight":
        if not args.manifest:
            parser.error("preflight requires --manifest")
        try:
            result = asyncio.run(preflight(LaunchManifest.read(Path(args.manifest))))
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
            return 0
        except (OSError, ValueError) as exc:
            sys.stdout.write(
                json.dumps(
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "status": "blocked",
                        "code": exc.code if isinstance(exc, InteractivePreparationError) else "invalid-input",
                        "error": str(exc),
                    }
                )
                + "\n"
            )
            return 1
    if not args.request_id:
        parser.error("inspect requires --request-id")
    try:
        result = read_receipt(args.request_id)
        result["composerCurrent"] = "unverified"
        result["currentSkills"] = [
            {
                "name": raw["name"],
                "status": _verify_files(
                    LaunchSkill(
                        raw["name"],
                        Path(raw["directory"]),
                        raw["contentSha256"],
                        raw["required"],
                        raw["source"],
                        tuple(raw["files"]),
                        raw["origin"],
                    )
                ),
            }
            for raw in result.get("pins", [])
        ]
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    except (OSError, ValueError) as exc:
        sys.stdout.write(json.dumps({"error": str(exc)}) + "\n")
        return 1
    return 0


async def preflight(manifest: LaunchManifest) -> dict[str, Any]:
    """Read native profile/settings and discover exact skills without creating a session."""
    from chrys.foundation.events.types import AgentRuntimeDetails
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.orchestration.startup import bootstrap_runtime
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_profile_selector as resolve_model_selector
    from chrys.service.skills.adapter import create_skills_provider
    from chrys.service.state.store import JsonFileStateStore

    loaded = bootstrap_runtime(dotenv_override=True, project_root=manifest.cwd).loaded
    registry = AgentProfileRegistry()
    await asyncio.to_thread(registry.load_all)
    selector = manifest.configuration.get("role") or loaded.settings.default_agent
    if manifest.mode == "resume":
        meta = await JsonFileStateStore().load_session_meta(manifest.session_id, strict=True)
        if meta is None or meta.session_id != manifest.session_id or meta.kind != "chat":
            raise InteractivePreparationError(
                "session-unavailable", "Strict interactive session was not found by its complete identity"
            )
        if Path(meta.primary_cwd).resolve() != manifest.cwd:
            raise InteractivePreparationError(
                "session-unavailable", "Strict interactive restore cannot relocate a session"
            )
        selector = meta.agent_profile_id or meta.agent_profile
    models = ModelProfileRegistry()
    await asyncio.to_thread(models.load_all)
    model_selector = manifest.configuration.get("model") or loaded.settings.model_profile
    model = resolve_model_selector(models, model_selector)
    if model is None:
        raise InteractivePreparationError(
            "configuration-unavailable", "Interactive launch model configuration is unavailable"
        )
    profile = registry.resolve_selector(selector)
    if profile is None or profile.sub_agent_only or profile.acp is not None:
        raise InteractivePreparationError("configuration-unavailable", "Interactive launch role is unavailable")
    provider, warnings = await create_skills_provider(
        profile.skills,
        runtime=SessionEnvironment(cwd=str(manifest.cwd), platform=get_platform()),
        project_skills_enabled=loaded.settings.project_skills_enabled,
    )
    launch = InteractiveLaunch(manifest)
    try:
        await asyncio.to_thread(
            launch.verify_skills,
            AgentRuntimeDetails(skill_details=provider.skill_details() if provider else []),
            project_enabled=loaded.settings.project_skills_enabled,
            profile=profile,
        )
    except ValueError as exc:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "status": "blocked",
            "error": str(exc),
            "skills": launch.receipt.get("skills", []),
        }
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "status": "ready",
        "modelProfileId": model.id,
        "requestSha256": manifest.request_sha256,
        "roleId": profile.id,
        "roleName": profile.name,
        "skills": launch.receipt.get("skills", []),
        "warnings": [warning.code for warning in warnings],
    }
