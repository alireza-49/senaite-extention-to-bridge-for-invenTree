"""Match verified SENAITE analyses against the configured consumption rules."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from .config import Rule

_WHITESPACE = re.compile(r"\s+")


def normalize(value: Any) -> str:
    """Lowercase, strip accents and collapse whitespace for comparisons."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = _WHITESPACE.sub(" ", text).strip()
    return text


def analysis_keyword(analysis: Mapping[str, Any]) -> str:
    """Return the analysis keyword, falling back to older field names."""

    for key in ("getKeyword", "Keyword", "keyword", "title"):
        value = analysis.get(key)
        if value:
            return str(value)
    return ""


def analysis_uid(analysis: Mapping[str, Any]) -> str:
    uid = analysis.get("uid")
    if not uid:
        raise ValueError("analysis payload has no uid")
    return str(uid)


def sample_type(sample: Mapping[str, Any] | None) -> str:
    if not sample:
        return ""
    for key in ("getSampleTypeTitle", "SampleTypeTitle", "getSampleType", "sample_type"):
        value = sample.get(key)
        if value:
            return str(value)
    return ""


def sample_id(sample: Mapping[str, Any] | None, analysis: Mapping[str, Any] | None = None) -> str:
    if sample:
        for key in ("getRequestID", "id", "getId", "title"):
            value = sample.get(key)
            if value:
                return str(value)
    if analysis:
        value = analysis.get("getRequestID") or analysis.get("parent_id")
        if value:
            return str(value)
    return ""


def matches(rule: Rule, keyword: str, sample_type_title: str) -> bool:
    """A rule matches when every non-empty filter list contains a hit.

    Keywords are compared as substrings so that ``"TPC"`` also matches a
    keyword such as ``"Total Plate Count (TPC)"``. An empty filter list means
    "any".
    """

    normalized_keyword = normalize(keyword)
    normalized_sample_type = normalize(sample_type_title)

    if rule.keywords:
        if not any(normalize(item) in normalized_keyword for item in rule.keywords):
            return False
    if rule.sample_types:
        if not any(normalize(item) == normalized_sample_type for item in rule.sample_types):
            return False
    return True


def match_rule(
    rules: Sequence[Rule],
    analysis: Mapping[str, Any],
    sample: Mapping[str, Any] | None,
) -> Rule | None:
    keyword = analysis_keyword(analysis)
    type_title = sample_type(sample)
    for rule in rules:
        if matches(rule, keyword, type_title):
            return rule
    return None


def describe_analysis(analysis: Mapping[str, Any], sample: Mapping[str, Any] | None) -> str:
    parts: Iterable[str] = (
        f"keyword={analysis_keyword(analysis) or '?'}",
        f"result={analysis.get('getResult', analysis.get('Result', '')) or '?'}",
        f"unit={analysis.get('getUnit', analysis.get('Unit', '')) or '?'}",
        f"sample={sample_id(sample, analysis) or '?'}",
        f"state={analysis.get('review_state', '?')}",
    )
    return " ".join(parts)
