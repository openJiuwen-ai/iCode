# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Localized approval concerns from structured judge results."""

from __future__ import annotations

from chrys.app.tui.i18n import render_str
from chrys.foundation.i18n import Localizer, msg
from chrys.service.approval.judge import JudgeVerdict

_SHARED = msg("tui.approval.risk.shared_mutation", fallback="Changes to shared or production resources")
_SENSITIVE = msg("tui.approval.risk.sensitive_data", fallback="Sensitive data or account operations")
_SECURITY = msg("tui.approval.risk.security_boundary", fallback="Security or permission changes")
_UNTRUSTED = msg("tui.approval.risk.untrusted_execution", fallback="Untrusted code or externally exposed services")
_DATA_EXFILTRATION = msg(
    "tui.approval.risk.data_exfiltration", fallback="Sensitive data transfer outside its trust boundary"
)
_FILE_FOLDER_DELETION = msg("tui.approval.risk.file_folder_deletion", fallback="File or folder deletion")
_REMOTE_REPOINT = msg("tui.approval.risk.remote_repoint", fallback="Changing a remote destination")
_GIT_DESTRUCTIVE = msg("tui.approval.risk.git_destructive", fallback="Destructive Git operations")
_LEAK_PATH = msg("tui.approval.risk.leak_path", fallback="A change that would leak data when run")
_USER_BOUNDARY = msg("tui.approval.risk.user_boundary", fallback="An active user boundary")
_EXFIL_SCOUTING = msg("tui.approval.risk.exfil_scouting", fallback="Preparation for sensitive data exfiltration")
_AGENT_STATE_TAMPER = msg("tui.approval.risk.agent_state_tamper", fallback="Tampering with agent state")
_UNVERIFIED_DELETE = msg("tui.approval.risk.unverified_delete", fallback="Deletion with unverified targets")
_CREDENTIAL_LEAKAGE = msg("tui.approval.risk.credential_leakage", fallback="Credential leakage")
_RISKS = {
    "security_boundary_change": _SECURITY,
    "data_exfiltration": _DATA_EXFILTRATION,
    "file_folder_deletion": _FILE_FOLDER_DELETION,
    "remote_repoint": _REMOTE_REPOINT,
    "git_destructive": _GIT_DESTRUCTIVE,
    "leak_path": _LEAK_PATH,
    "user_boundary": _USER_BOUNDARY,
    "exfil_scouting": _EXFIL_SCOUTING,
    "agent_state_tamper": _AGENT_STATE_TAMPER,
    "unverified_delete": _UNVERIFIED_DELETE,
    "credential_leakage": _CREDENTIAL_LEAKAGE,
    "production_or_shared_infrastructure_mutation": _SHARED,
    "sensitive_data_handling": _SENSITIVE,
    "untrusted_code_execution_or_exposure": _UNTRUSTED,
}
_TRIGGERED = msg("tui.approval.review.triggered", fallback="Requires human confirmation: {conditions}.")
_FAILED = msg("tui.approval.review.failed", fallback="Automatic review failed. Please check the action.")
_INPUT = msg("tui.approval.review.invalid_input", fallback="There is not enough valid context to review this action.")
_ASSET = msg("tui.approval.review.asset_unavailable", fallback="The risk definitions could not be loaded.")
_TIMEOUT = msg("tui.approval.review.timeout", fallback="Automatic review timed out.")
_SHAPE = msg("tui.approval.review.invalid_shape", fallback="The risk check returned an invalid response.")
_VERDICT = msg("tui.approval.review.invalid_verdict", fallback="The approval model returned an invalid response.")
_AUTH = msg(
    "tui.approval.review.auth_failed",
    fallback="The approval model rejected authentication or access. Check the API key and model permissions.",
)
_RATE_LIMITED = msg(
    "tui.approval.review.rate_limited",
    fallback="The approval model is rate limiting requests. Please try again later.",
)
_SERVER = msg(
    "tui.approval.review.server_error",
    fallback="The approval model service returned an internal error. Please try again later.",
)
_FAILURES = {
    "invalid_input": _INPUT,
    "asset_unavailable": _ASSET,
    "timeout": _TIMEOUT,
    "invalid_shape": _SHAPE,
    "invalid_verdict": _VERDICT,
    "auth_failed": _AUTH,
    "rate_limited": _RATE_LIMITED,
    "server_error": _SERVER,
}


def review_reason(verdict: JudgeVerdict, localizer: Localizer) -> str:
    """Render program-generated concerns; model-written reasons remain verbatim."""
    if verdict.failure_reason:
        return render_str(localizer, _FAILURES.get(verdict.failure_reason, _FAILED).bind())
    if verdict.triggered and all(key in _RISKS for key in verdict.triggered):
        conditions = ", ".join(render_str(localizer, _RISKS[key].bind()) for key in verdict.triggered)
        return render_str(localizer, _TRIGGERED.bind(conditions=conditions))
    return verdict.reason
