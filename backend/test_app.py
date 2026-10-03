from pathlib import Path

from app import shortlist, table_from_upload, validate_result

ROOT = Path(__file__).resolve().parents[1]


def test_reference_workbooks_parse():
    release = table_from_upload((ROOT / "WhatsNew2026R2.xlsx").read_bytes(), "release")
    e2e = table_from_upload((ROOT / "e2e list.xlsx").read_bytes(), "e2e")
    assert release["row_count"] == 87
    assert e2e["row_count"] == 42
    assert release["rows"][0]["title"]
    assert e2e["rows"][0]["e2e"] == "ABS_001 - Leave Processing (Paid and Unpaid)"


def test_shortlist_returns_catalog_values():
    release = table_from_upload((ROOT / "WhatsNew2026R2.xlsx").read_bytes(), "release")
    e2e = table_from_upload((ROOT / "e2e list.xlsx").read_bytes(), "e2e")
    choices = shortlist(release["rows"][0], e2e["rows"])
    assert 1 <= len(choices) <= 8
    catalog = {row["e2e"] for row in e2e["rows"]}
    assert all(choice["e2e"] in catalog for choice in choices)


def test_invalid_model_selection_never_invents_e2e():
    result = validate_result({"decision": "match", "e2e_name": "invented", "confidence": 99}, ["known"])
    assert result["decision"] == "unable_to_identify"
    assert result["e2e_name"] is None
    assert result["confidence"] <= 49


def test_unresolved_result_always_requires_review():
    result = validate_result({"decision": "no_matching_e2e", "confidence": 0, "review_required": False}, ["known"])
    assert result["review_required"] is True


def test_evidence_string_is_kept_as_one_phrase():
    result = validate_result({"decision": "match", "e2e_name": "known", "confidence": 80, "evidence": "Change Job"}, [{"e2e": "known", "shortlist_score": 0.8}], "Change Job workflow update")
    assert result["evidence"] == ["Change Job"]
    assert result["evaluation"]["evidence_grounding"] == 100


def test_non_match_cannot_retain_selection():
    result = validate_result({'decision': 'no_matching_e2e', 'e2e_name': 'known'}, ['known'])
    assert result['e2e_name'] is None


def test_malformed_decision_and_infinite_confidence_require_review():
    result = validate_result({'decision':['match'],'e2e_name':'known','confidence':'Infinity'}, ['known'])
    assert result['decision']=='unable_to_identify'
    assert result['e2e_name'] is None
    assert result['review_required']


def test_2025_schema_adapter():
    import io
    from openpyxl import Workbook
    from app import RELEASE_2025_COLUMNS
    wb = Workbook()
    wb.active.append(list(RELEASE_2025_COLUMNS.values()))
    wb.active.append(['Time Tracking', 'Kiosk support', 'Android tablets', 'Events sent', None, 'Workday 2025 Release 1', 'https://example.test/note'])
    buffer = io.BytesIO()
    wb.save(buffer)
    row = table_from_upload(buffer.getvalue(), 'release')['rows'][0]
    assert row['title'] == 'Kiosk support'
    assert row['release_number'] == 'Workday 2025 Release 1'
    assert row['product_area'] == ''
