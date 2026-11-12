# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn-aware compaction package facade."""

import asyncio  # noqa: F401  -- intentional transitive re-export

from chrys.foundation.text.tokenizer import MixedLanguageTokenizer as MixedLanguageTokenizer
from chrys.kernel import GROUP_ANNOTATION_KEY as GROUP_ANNOTATION_KEY
from chrys.kernel import SUMMARIZED_BY_SUMMARY_ID_KEY as SUMMARIZED_BY_SUMMARY_ID_KEY
from chrys.kernel.compaction import _group_id as _group_id

from .breaker import MAX_DROP_ROUNDS_PER_TURN as MAX_DROP_ROUNDS_PER_TURN
from .events import _REASON_COMPRESSION as _REASON_COMPRESSION
from .events import _REASON_CURRENT_TURN_DROP as _REASON_CURRENT_TURN_DROP
from .events import CachedCompressedContextSummary as CachedCompressedContextSummary
from .events import CachedSummary as CachedSummary
from .events import CompactionInfo as CompactionInfo
from .events import CompressInfo as CompressInfo
from .events import PendingCompression as PendingCompression
from .events import PreCompactInfo as PreCompactInfo
from .exclusions import _ExclusionAnchor as _ExclusionAnchor
from .groups import _content_signature as _content_signature
from .groups import _dedup_message_ids as _dedup_message_ids
from .groups import _group_kind_map as _group_kind_map
from .groups import _group_messages_by_id as _group_messages_by_id
from .groups import _ordered_group_ids as _ordered_group_ids
from .spill import SpillBatchResult as SpillBatchResult
from .spill import SpillManifestItem as SpillManifestItem
from .spill import SpillQuota as SpillQuota
from .spill import turn_directory_name as turn_directory_name
from .spill import write_spill_batch as write_spill_batch
from .strategy import CompactionMode as CompactionMode
from .strategy import LastWordsGeneratorLike as LastWordsGeneratorLike
from .strategy import UnifiedContextStrategy as UnifiedContextStrategy
from .summaries import _build_summary as _build_summary
from .summaries import _set_summarized as _set_summarized
from .turns import ResolvedTurns as ResolvedTurns
from .turns import TurnSpan as TurnSpan
from .turns import _resolve_turns as _resolve_turns
