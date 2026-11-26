# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Atomic catalog bundles and safe semantic-message rendering."""

from __future__ import annotations

import gettext
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache

from chrys.foundation.i18n import formatting as _formatting
from chrys.foundation.i18n.catalogs import CatalogLoadError, CatalogRoot, load_catalog
from chrys.foundation.i18n.formatting import format_message, has_visible_content, validate_authored_template
from chrys.foundation.i18n.locale import ENGLISH_LOCALE, normalize_locale
from chrys.foundation.i18n.messages import MessageRef, _render_arguments

logger = logging.getLogger(__name__)


class CatalogWarningCode(StrEnum):
    """Stable kinds of catalog warning returned to composition roots."""

    LOAD_FAILED = "i18n_catalog_load_failed"


@dataclass(frozen=True, slots=True)
class CatalogLoadWarning:
    """Typed notice that a requested catalog could not replace English."""

    requested_locale: str
    code: CatalogWarningCode = field(default=CatalogWarningCode.LOAD_FAILED, init=False)


type _TemplateValidator = Callable[[str, bool], frozenset[str] | None]


@dataclass(frozen=True, slots=True)
class _CatalogBundle:
    locale_name: str
    translations: gettext.GNUTranslations | None
    validate_template: _TemplateValidator | None


@dataclass(frozen=True, slots=True)
class _LocalizerState:
    requested_locale: str
    bundle: _CatalogBundle
    first_load_warning: CatalogLoadWarning | None = None


_ENGLISH_BUNDLE = _CatalogBundle(
    locale_name=ENGLISH_LOCALE,
    translations=None,
    validate_template=None,
)


class Localizer:
    """Render ``MessageRef`` values through one atomically replaceable bundle."""

    __slots__ = ("_catalog_root", "_state")

    def __init__(self, requested_locale: str, *, catalog_root: CatalogRoot | None = None) -> None:
        self._catalog_root = catalog_root
        self._state = _LocalizerState(requested_locale=requested_locale, bundle=_ENGLISH_BUNDLE)

        effective_locale = normalize_locale(requested_locale)
        if effective_locale == ENGLISH_LOCALE:
            return
        try:
            bundle = self._build_bundle(effective_locale)
        except CatalogLoadError:
            logger.warning("Requested i18n catalog unavailable; using English.")
            self._state = _LocalizerState(
                requested_locale=requested_locale,
                bundle=_ENGLISH_BUNDLE,
                first_load_warning=CatalogLoadWarning(requested_locale=requested_locale),
            )
            return
        self._state = _LocalizerState(requested_locale=requested_locale, bundle=bundle)

    @property
    def requested_locale(self) -> str:
        """Return the persisted/requested locale value without rewriting it."""
        return self._state.requested_locale

    @property
    def effective_locale(self) -> str:
        """Return the locale of the currently active immutable bundle."""
        return self._state.bundle.locale_name

    @property
    def first_load_warning(self) -> CatalogLoadWarning | None:
        """Return the typed warning produced by a failed initial catalog load."""
        return self._state.first_load_warning

    def switch_locale(self, requested_locale: str) -> CatalogLoadWarning | None:
        """Build and atomically install a locale bundle, retaining state on failure."""
        prior = self._state
        effective_locale = normalize_locale(requested_locale)
        if effective_locale == prior.bundle.locale_name:
            self._state = _LocalizerState(requested_locale=requested_locale, bundle=prior.bundle)
            return None
        if effective_locale == ENGLISH_LOCALE:
            self._state = _LocalizerState(requested_locale=requested_locale, bundle=_ENGLISH_BUNDLE)
            return None
        try:
            bundle = self._build_bundle(effective_locale)
        except CatalogLoadError:
            logger.warning("Requested i18n catalog unavailable; keeping active locale.")
            return CatalogLoadWarning(requested_locale=requested_locale)
        self._state = _LocalizerState(requested_locale=requested_locale, bundle=bundle)
        return None

    def render(self, reference: MessageRef) -> str:
        """Render a bound semantic message through the active locale bundle."""
        bundle = self._state.bundle
        translations = bundle.translations
        if translations is None:
            return format_message(reference)

        definition = reference.definition
        plural_id = f"{definition.key}#plural"
        try:
            # ZeroDivisionError: a loadable MO's Plural-Forms expression may
            # divide by zero only once ngettext evaluates it for a count.
            if reference.count is None:
                template = translations.gettext(definition.key)
                lookup_ids = {definition.key}
            else:
                template = translations.ngettext(definition.key, plural_id, reference.count)
                lookup_ids = {definition.key, plural_id}
        except LookupError, OSError, TypeError, ValueError, ZeroDivisionError:
            logger.warning("Invalid i18n catalog lookup; using English fallback.")
            return format_message(reference, render_nested=self.render)

        if _formatting._normalized_lookup_candidate(template) in lookup_ids:
            logger.debug("Missing i18n catalog entry; using English fallback.")
            return format_message(reference, render_nested=self.render)

        validator = bundle.validate_template
        if validator is None:
            return format_message(reference, render_nested=self.render)
        names = validator(template, definition.multiline)
        if names is None:
            logger.warning("Invalid i18n catalog entry; using English fallback.")
            return format_message(reference, render_nested=self.render)

        parameters = _render_arguments(reference, self.render)
        parameter_names = set(parameters)
        if names - {"count"} != parameter_names - {"count"} or ("count" in names and "count" not in parameter_names):
            logger.warning("Invalid i18n catalog entry; using English fallback.")
            return format_message(reference, render_nested=self.render)
        try:
            translated = template.format_map(parameters)
        except KeyError, ValueError:
            logger.warning("Invalid i18n catalog entry; using English fallback.")
            return format_message(reference, render_nested=self.render)
        if not has_visible_content(translated):
            logger.warning("Invalid i18n catalog entry; using English fallback.")
            return format_message(reference, render_nested=self.render)
        return translated

    def _build_bundle(self, locale_name: str) -> _CatalogBundle:
        translations = load_catalog(locale_name, catalog_root=self._catalog_root)
        return _CatalogBundle(
            locale_name=locale_name,
            translations=translations,
            validate_template=_new_template_validator(),
        )


def _new_template_validator() -> _TemplateValidator:
    @cache
    def validate(template: str, multiline: bool) -> frozenset[str] | None:
        try:
            names = validate_authored_template(template, multiline=multiline)
        except TypeError, ValueError:
            return None
        if _formatting._is_lookup_id(template):
            return None
        return names

    return validate
