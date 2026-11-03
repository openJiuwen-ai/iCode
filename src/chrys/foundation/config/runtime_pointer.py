# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Origin registry for the live model-profile pointer.

``CHRYS_MODEL_PROFILE`` in ``os.environ`` is not a configuration source but a
**live process pointer**: activating a profile writes it and the very next
settings reload reads it back. The carrier can say *what* the pointer is but
never *who* wrote it — and the writer cannot be reconstructed from the value
(a restored session may write the same id the shell exported; ``--model`` may
match the frozen snapshot). So every process-level writer records its own
origin here, and the loader reports the pointer under that origin instead of
blaming the environment.

Per-host selections (a session pin, headless ``--model``) never touch this
registry — it is process-global, and in an ACP process one session's choice
must not rewrite another session's provenance. Those stay host-local pins on
their own ``LoadedSettings``.

Value and origin are two variables that mean one thing, and the writers no
longer all run on the same thread — profile activation moved to a worker so
the settings file's lock cannot stall an event loop. So every access takes
:data:`_lock`, and readers take *both* halves inside it: a read that fetches
the value and then the origin can pair one writer's value with another
writer's origin, which is precisely the mistake this registry exists to
prevent. Undoing a write needs more than the lock, hence :attr:`_generation`
— see :func:`restore_model_pointer`.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Final

from chrys.foundation.config.errors import ConfigError
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.i18n import msg

MODEL_POINTER_ENV: Final = "CHRYS_MODEL_PROFILE"
"""The pointer's carrier variable in ``os.environ``."""

MODEL_POINTER_KEY: Final = "model.profile.active"
"""The settings key the pointer resolves as (``Settings.model_profile``)."""


@dataclass(frozen=True, slots=True)
class PointerToken:
    """One captured pointer state, and the write it was captured before.

    ``origin`` is ``None`` for a pointer nobody here wrote: an absent one, or
    a real shell export, which the loader attributes to ``Source.ENV`` itself.
    """

    value: str | None
    origin: SettingOrigin | None
    generation: int
    """The registry's write counter as it stood *before* the capturing write."""


_lock: Final = threading.Lock()
_registered_origin: SettingOrigin | None = None
_generation = 0


_file_choice = ""
_launch_choice = ""


_MODEL_SELECTION_CHANGED = msg(
    "settings.model_selection_changed", fallback="Model selection changed during reload. Run /reload again."
)


class StaleModelFileChange(ConfigError, RuntimeError):
    """A newer selection superseded the in-flight rebuild."""


@dataclass(frozen=True, slots=True)
class ModelFileChange:
    """An external model edit awaiting a successful TUI rebuild."""

    file_value: str
    value: str
    origin: SettingOrigin
    generation: int

    def commit(self) -> None:
        global _file_choice
        with _lock:
            if self.generation != _generation:
                raise StaleModelFileChange(_MODEL_SELECTION_CHANGED.bind())
            _write_pointer(self.value or None, self.origin)
            _file_choice = self.file_value


@dataclass(frozen=True, slots=True)
class SessionDefaultModel:
    """The default model one session settled on, kept apart from the process pointer.

    An ACP process serves many sessions from one pointer, so a full reload's
    choice stays with the session that made it and travels on every later load
    of that session. An empty ``value`` records that the default is gone, so a
    load cannot bring it back from a pointer that still names it. ``revision`` is
    :func:`model_selection_revision` when it was settled: any process-wide model
    choice since then came after it, and the session follows that instead.
    """

    value: str
    origin: SettingOrigin
    revision: int


def model_selection_revision() -> int:
    with _lock:
        return _generation


def check_model_selection_revision(expected: int) -> None:
    if model_selection_revision() != expected:
        raise StaleModelFileChange(_MODEL_SELECTION_CHANGED.bind())


def remember_file_model(value: str) -> None:
    """Record bootstrap or a successful write of this particular key."""
    global _file_choice, _generation
    with _lock:
        _file_choice = value
        _generation += 1


def _launch_or_file_model(value: str, file_origin: SettingOrigin, exported: str) -> tuple[str, SettingOrigin]:
    """What the launch and the file say the model is, leaving out the live pointer: caller holds :data:`_lock`."""
    if _launch_choice:
        return _launch_choice, SettingOrigin(layer=Source.CLI)
    if exported:
        return exported, SettingOrigin(layer=Source.ENV)
    return value, file_origin


def external_model_edit(value: str, *, file_origin: SettingOrigin, exported: str) -> ModelFileChange | None:
    with _lock:
        if value == _file_choice:
            return None
        selected, origin = _launch_or_file_model(value, file_origin, exported)
        return ModelFileChange(value, selected, origin, _generation)


def session_model_decision(value: str, *, file_origin: SettingOrigin, exported: str) -> ModelFileChange:
    """Decide one session's default afresh: never a change to the pointer, so nothing here commits it."""
    with _lock:
        selected, origin = _launch_or_file_model(value, file_origin, exported)
        return ModelFileChange(value, selected, origin, _generation)


def _write_pointer(value: str | None, origin: SettingOrigin) -> None:
    global _registered_origin, _generation
    if value is None:
        os.environ.pop(MODEL_POINTER_ENV, None)
        _registered_origin = None
    else:
        os.environ[MODEL_POINTER_ENV] = value
        _registered_origin = origin
    _generation += 1


def set_model_pointer(value: str | None, *, origin: SettingOrigin) -> PointerToken:
    """Write the pointer and record who wrote it; return the replaced state.

    ``None`` clears the pointer (and the registration with it — an absent
    value has no writer). The returned token restores the previous state via
    :func:`restore_model_pointer`, which is the only correct undo: writing the
    old value back into ``os.environ`` by hand leaves the origin describing
    the write that was just rolled back.
    """
    global _launch_choice
    with _lock:
        token = PointerToken(os.environ.get(MODEL_POINTER_ENV), _registered_origin, _generation)
        if origin.layer is Source.CLI:
            _launch_choice = value or ""
        _write_pointer(value, origin)
        return token


def restore_model_pointer(token: PointerToken) -> bool:
    """Put back a captured state, value and origin together.

    Refuses — and reports it — when anything has written the pointer since the
    write this token came from. A token records the state *before* one write,
    so it is only an undo of that write; replayed over a third party's later
    write it is not a rollback but a silent overwrite with a stale value, and
    the caller unwinding a failed restore has no way to know the user picked a
    different profile while it was running. Their write is the newer intent
    and stands; a locked read-modify-write cannot decide that, which is why
    the counter and not just :data:`_lock` closes this.
    """
    global _registered_origin, _generation
    with _lock:
        if _generation != token.generation + 1:
            return False
        if token.value is None:
            os.environ.pop(MODEL_POINTER_ENV, None)
        else:
            os.environ[MODEL_POINTER_ENV] = token.value
        _registered_origin = token.origin
        _generation += 1
        return True


def get_model_pointer() -> tuple[str, SettingOrigin | None]:
    """Return the live pointer value ("" when unset) and its registered origin.

    The pair is read under :data:`_lock` and must stay a pair: a caller that
    takes the value here and the origin from a second call can be handed one
    writer's value with another writer's origin. ``None`` origin means no
    registered writer: either the pointer is unset, or the value came from the
    real environment (the shell's export) — the loader attributes that case to
    ``Source.ENV`` itself.
    """
    with _lock:
        return os.environ.get(MODEL_POINTER_ENV, ""), _registered_origin


def _reset_model_pointer_for_tests() -> None:
    global _registered_origin, _generation, _file_choice, _launch_choice
    with _lock:
        _registered_origin = None
        _generation = 0
        _file_choice = ""
        _launch_choice = ""
