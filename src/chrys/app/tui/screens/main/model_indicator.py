# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure state computation for the status-bar model indicator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from chrys.foundation.events.types import RuntimeModelDetails
from chrys.foundation.i18n import Localizer, MessageRef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar

if TYPE_CHECKING:
    from chrys.service.profiles.models.registry import ModelProfileRegistry

_SELECT_MODEL = msg("tui.model_indicator.label.select", fallback="Select Model")
_CONFIGURE_TOOLTIP = msg("tui.model_indicator.tooltip.configure", fallback="Open model settings")
_SELECT_TOOLTIP = msg("tui.model_indicator.tooltip.select", fallback="Choose a model profile")
_DETAILS_TOOLTIP = msg(
    "tui.model_indicator.tooltip.details",
    fallback="{name} · {provider} · {model_id} · {context} context · {stream} · {vision}",
)
_DETAILS_WITH_STYLE_TOOLTIP = msg(
    "tui.model_indicator.tooltip.details_with_style",
    fallback="{name} · {provider} · {api_style} · {model_id} · {context} context · {stream} · {vision}",
)
_STREAM_ON = msg("tui.model_indicator.tooltip.stream.on", fallback="Streaming")
_STREAM_OFF = msg("tui.model_indicator.tooltip.stream.off", fallback="Non-streaming")
_VISION_ON = msg("tui.model_indicator.tooltip.vision.on", fallback="Vision")
_VISION_OFF = msg("tui.model_indicator.tooltip.vision.off", fallback="Text-only")
_AGENT_LOCKED_TOOLTIP = msg(
    "tui.model_indicator.tooltip.locked.agent",
    fallback="Model selection is controlled by {agent}.",
)
_OVERRIDE_LOCKED_TOOLTIP = msg(
    "tui.model_indicator.tooltip.locked.override",
    fallback="This session is pinned to this model.",
)
_GENERIC_LOCKED_TOOLTIP = msg(
    "tui.model_indicator.tooltip.locked.generic",
    fallback="Model selection is locked.",
)
_AGENT_LOCKED_NOTICE = msg(
    "tui.model_indicator.notice.locked.agent",
    fallback=(
        "{agent} is bound to the model {model}, so the model cannot be switched here. "
        "To change it, press F2 and edit the agent's model on its Basic tab."
    ),
)


@dataclass(frozen=True, slots=True)
class ModelIndicatorState:
    """Display and interaction state for the status-bar model indicator."""

    label: str
    tooltip: str
    mode: Literal["configure", "select", "locked"]
    profile_id: str
    visible: bool


# The two sources that leave the choice with the user; every other value
# means some other layer already decided which model this session speaks to.
_USER_OWNED_SOURCES = frozenset({"active", "default"})


def is_model_selection_locked(details: RuntimeModelDetails, *, runtime_confirmed: bool) -> bool:
    """Return whether runtime policy, not the user, owns the model choice.

    Mirrors the ``"locked"`` mode of :func:`compute_model_indicator_state`
    over the same ``selection_source`` values so the status-bar tag and the
    inline ``$`` picker cannot hand out different answers.

    Unconfirmed runtime details deliberately do NOT lock: the indicator hides
    itself until the backend answers, while the picker falls back to the
    activated profile id and stays usable through that window.
    """
    if not runtime_confirmed:
        return False
    return details.selection_source not in _USER_OWNED_SOURCES


def model_lock_notice(details: RuntimeModelDetails, agent_label: str) -> MessageRef:
    """Explain a locked model tag, naming where the user can lift the lock.

    Picks its reason by the same ``selection_source`` branches as the locked
    tooltip in :func:`compute_model_indicator_state`.
    """
    source = details.selection_source
    if source == "agent" and agent_label:
        return _AGENT_LOCKED_NOTICE.bind(agent=agent_label, model=details.name)
    if source == "override":
        return _OVERRIDE_LOCKED_TOOLTIP.bind()
    return _GENERIC_LOCKED_TOOLTIP.bind()


def fmt_context_size(tokens: int) -> str:
    """Format token count as compact size string."""
    if tokens >= 1_000_000:
        millions = tokens / 1_000_000
        return f"{millions:.0f}m" if millions == int(millions) else f"{millions:.1f}m"
    if tokens >= 1000:
        thousands = tokens / 1000
        return f"{thousands:.0f}k" if thousands == int(thousands) else f"{thousands:.1f}k"
    return str(tokens)


def _live_label(details: RuntimeModelDetails, registry: ModelProfileRegistry | None) -> str:
    """Return the profile's current name from the registry if available.

    The runtime details are a snapshot taken when the agent started. A catalog
    sync can rewrite the profile file while the session is live, so the label
    shown in the indicator must track the registry rather than the snapshot.
    """
    if registry is not None and (profile := registry.get(details.profile_id)) is not None:
        return profile.name
    return details.name


# AIxCoding catalog: label tracks the live registry — a catalog sync rewrites
# the profile while a session runs (aixcoding/docs/batches/catalog-sync/design.md).
def compute_model_indicator_state(
    details: RuntimeModelDetails | None,
    has_selectable_profile: bool,
    agent_label: str,
    localizer: Localizer,
    *,
    runtime_confirmed: bool = True,
    model_registry: ModelProfileRegistry | None = None,
) -> ModelIndicatorState:
    """Compute model indicator state from confirmed runtime details and registry availability."""
    if not runtime_confirmed:
        return _hidden_state()
    if details is None:
        return _action_state_if_agent_ready(has_selectable_profile, agent_label, localizer)
    if not has_selectable_profile:
        return _action_state_if_agent_ready(False, agent_label, localizer)

    source = details.selection_source
    if source == "default":
        return _action_state_if_agent_ready(True, agent_label, localizer)

    tooltip = _details_tooltip(details, localizer)
    label = _live_label(details, model_registry)
    if source == "active":
        return ModelIndicatorState(
            label=label,
            tooltip=tooltip,
            mode="select",
            profile_id=details.profile_id,
            visible=True,
        )

    if source == "agent" and agent_label:
        reason = _render(localizer, _AGENT_LOCKED_TOOLTIP.bind(agent=agent_label))
    elif source == "override":
        reason = _render(localizer, _OVERRIDE_LOCKED_TOOLTIP.bind())
    else:
        reason = _render(localizer, _GENERIC_LOCKED_TOOLTIP.bind())
    return ModelIndicatorState(
        label=label,
        tooltip=f"{tooltip}\n{reason}",
        mode="locked",
        profile_id=details.profile_id,
        visible=True,
    )


def _render(localizer: Localizer, reference: MessageRef) -> str:
    return sanitize_legacy_scalar(localizer.render(reference))


def _hidden_state() -> ModelIndicatorState:
    return ModelIndicatorState(label="", tooltip="", mode="locked", profile_id="", visible=False)


def _action_state_if_agent_ready(
    has_selectable_profile: bool,
    agent_label: str,
    localizer: Localizer,
) -> ModelIndicatorState:
    if not agent_label:
        return _hidden_state()
    return _action_state(has_selectable_profile, localizer)


def _action_state(has_selectable_profile: bool, localizer: Localizer) -> ModelIndicatorState:
    if has_selectable_profile:
        return ModelIndicatorState(
            label=_render(localizer, _SELECT_MODEL.bind()),
            tooltip=_render(localizer, _SELECT_TOOLTIP.bind()),
            mode="select",
            profile_id="",
            visible=True,
        )
    # The label stays "Select Model" even with nothing selectable; the
    # configure mode routes the click straight to the model config modal.
    return ModelIndicatorState(
        label=_render(localizer, _SELECT_MODEL.bind()),
        tooltip=_render(localizer, _CONFIGURE_TOOLTIP.bind()),
        mode="configure",
        profile_id="",
        visible=True,
    )


def _details_tooltip(details: RuntimeModelDetails, localizer: Localizer) -> str:
    context = fmt_context_size(details.max_context_tokens)
    stream = _render(localizer, (_STREAM_ON if details.stream else _STREAM_OFF).bind())
    vision = _render(localizer, (_VISION_ON if details.vision else _VISION_OFF).bind())
    if details.api_style:
        return _render(
            localizer,
            _DETAILS_WITH_STYLE_TOOLTIP.bind(
                name=details.name,
                provider=details.provider,
                api_style=details.api_style,
                model_id=details.model_id,
                context=context,
                stream=stream,
                vision=vision,
            ),
        )
    return _render(
        localizer,
        _DETAILS_TOOLTIP.bind(
            name=details.name,
            provider=details.provider,
            model_id=details.model_id,
            context=context,
            stream=stream,
            vision=vision,
        ),
    )
