from decimal import Decimal

from bridge.config import ConsumeLine, Rule
from bridge.rules import analysis_keyword, matches, match_rule, normalize, sample_id, sample_type


def rule(**kwargs) -> Rule:
    defaults = dict(
        id="r1",
        keywords=("TPC",),
        sample_types=(),
        consume=(ConsumeLine(part_ipn="X", quantity=Decimal("1")),),
    )
    defaults.update(kwargs)
    return Rule(**defaults)  # type: ignore[arg-type]


def test_normalize_ignores_case_accents_and_whitespace():
    assert normalize("  Total   Plate COUNT ") == "total plate count"


def test_keyword_matches_as_substring_and_sample_type_exactly():
    assert matches(rule(), "Total Plate Count (TPC)", "")
    assert not matches(rule(sample_types=("water",)), "TPC", "drinking water")
    assert matches(rule(sample_types=("water",)), "TPC", "Water")


def test_empty_filters_match_anything():
    assert matches(rule(keywords=(), sample_types=()), "anything", "anywhere")


def test_match_rule_uses_first_matching_rule_and_reads_senaite_fields():
    first = rule(id="first", keywords=("chlorine",))
    second = rule(id="second", keywords=("Total Plate Count", "TPC"))
    analysis = {"getKeyword": "Total Plate Count", "uid": "a1"}
    sample = {"getSampleTypeTitle": "water", "getRequestID": "S-1"}

    assert match_rule((first, second), analysis, sample).id == "second"
    assert analysis_keyword(analysis) == "Total Plate Count"
    assert sample_type(sample) == "water"
    assert sample_id(sample, analysis) == "S-1"
    assert match_rule((first,), {"getKeyword": "nitrate"}, sample) is None
