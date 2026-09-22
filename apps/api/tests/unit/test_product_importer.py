"""Pure-Python parsing behaviour of `app/products/importer.py` -- no
database, no event loop. `tests/integration/test_product_import.py` covers
the pipeline end to end (upload -> worker -> rows with vectors); this file
is what actually pins the per-row rules the brief cares about: which row
number a customer sees, which cell shapes are accepted, and which failures
abort the whole file versus just one row.
"""

import json

import pytest

from app.products.importer import (
    ImportParseError,
    RowError,
    UnsupportedImportType,
    _dedupe_by_external_id,
    _slugify,
    parse_import,
    resolve_import_mime_type,
)


def _csv(*rows: str, header: str = "external_id,name,price") -> bytes:
    return "\n".join([header, *rows]).encode("utf-8")


# ---------------------------------------------------------------------------
# CSV: happy path, row numbering, per-row isolation
# ---------------------------------------------------------------------------


def test_csv_parses_every_valid_row():
    data = _csv("sku-1,Camry,32999.00", "sku-2,Corolla,24999.00", "sku-3,RAV4,28999.00")
    result = parse_import(data, "text/csv")
    assert [row.product.external_id for row in result.rows] == ["sku-1", "sku-2", "sku-3"]
    assert result.errors == []
    assert result.total_rows == 3


def test_csv_row_numbers_are_1_based_and_header_aware():
    """Row 1 is the header; the first data row is row 2 -- the number a
    human looking at their own spreadsheet in a text editor would count,
    not a 0-based data index."""
    data = _csv("sku-1,Camry,32999.00", "sku-2,Corolla,24999.00")
    result = parse_import(data, "text/csv")
    assert [row.row for row in result.rows] == [2, 3]


def test_a_bad_row_is_reported_and_does_not_abort_the_file():
    """The brief's own example: a malformed price on one row reports that
    row and the other N-1 still import."""
    data = _csv(
        "sku-1,Camry,32999.00",
        "sku-2,Corolla,not-a-price",
        "sku-3,RAV4,28999.00",
    )
    result = parse_import(data, "text/csv")
    assert [row.product.external_id for row in result.rows] == ["sku-1", "sku-3"]
    assert len(result.errors) == 1
    [error] = result.errors
    assert error.row == 3  # header (1) + sku-1 (2) + sku-2, the bad row (3)
    assert error.external_id == "sku-2"
    assert "price" in error.message
    assert result.total_rows == 3


def test_a_missing_external_id_value_is_a_hard_row_error():
    data = _csv(",Camry,32999.00")
    result = parse_import(data, "text/csv")
    assert result.rows == []
    [error] = result.errors
    assert error.row == 2
    assert "external_id" in error.message


def test_csv_missing_a_required_header_column_aborts_the_whole_file():
    """Not a per-row error: every row would fail identically, so this is a
    file-level problem reported once, not N times."""
    data = b"name,price\nCamry,32999.00\n"
    with pytest.raises(ImportParseError, match="external_id"):
        parse_import(data, "text/csv")


def test_csv_with_a_utf8_bom_still_finds_its_header():
    """Excel's 'CSV UTF-8' export prepends a BOM; left un-stripped it lands
    inside the first header cell's value and makes that column invisible."""
    data = b"\xef\xbb\xbfexternal_id,name\nsku-1,Camry\n"
    result = parse_import(data, "text/csv")
    assert [row.product.external_id for row in result.rows] == ["sku-1"]


def test_csv_row_with_fewer_cells_than_the_header_does_not_crash():
    data = b"external_id,name,price\nsku-1,Camry\n"
    result = parse_import(data, "text/csv")
    assert [row.product.external_id for row in result.rows] == ["sku-1"]
    assert result.rows[0].product.price is None


# ---------------------------------------------------------------------------
# slug: auto-generated when absent, trusted when supplied
# ---------------------------------------------------------------------------


def test_slug_is_auto_generated_from_name_when_absent():
    data = _csv("sku-1,Camry Hybrid LE,32999.00", header="external_id,name,price")
    [row] = parse_import(data, "text/csv").rows
    assert row.product.slug == "camry-hybrid-le"


def test_a_supplied_slug_is_trusted_as_is():
    data = b"external_id,name,slug\nsku-1,Camry,my-custom-slug\n"
    [row] = parse_import(data, "text/csv").rows
    assert row.product.slug == "my-custom-slug"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Camry Hybrid LE", "camry-hybrid-le"),
        ("  Extra   Spaces  ", "extra-spaces"),
        ("100% Cotton T-Shirt!", "100-cotton-t-shirt"),
        ("", "product"),
    ],
)
def test_slugify(name, expected):
    assert _slugify(name) == expected


# ---------------------------------------------------------------------------
# attributes/metadata: JSON-in-a-cell for CSV, native objects for JSON
# ---------------------------------------------------------------------------


def test_csv_attributes_column_is_parsed_as_json():
    data = b'external_id,name,attributes\nsku-1,Camry,"{""seats"": 5, ""fuel"": ""hybrid""}"\n'
    [row] = parse_import(data, "text/csv").rows
    assert row.product.attributes == {"seats": 5, "fuel": "hybrid"}


def test_csv_blank_attributes_defaults_to_empty_dict():
    data = b"external_id,name,attributes\nsku-1,Camry,\n"
    [row] = parse_import(data, "text/csv").rows
    assert row.product.attributes == {}


def test_csv_malformed_attributes_json_is_a_row_error_not_a_crash():
    data = b"external_id,name,attributes\nsku-1,Camry,{not valid json\n"
    result = parse_import(data, "text/csv")
    assert result.rows == []
    [error] = result.errors
    assert error.row == 2
    assert "JSON" in error.message


def test_json_import_accepts_native_attributes_objects():
    payload = json.dumps(
        [{"external_id": "sku-1", "name": "Camry", "attributes": {"seats": 5}}]
    ).encode()
    [row] = parse_import(payload, "application/json").rows
    assert row.product.attributes == {"seats": 5}


# ---------------------------------------------------------------------------
# JSON: happy path, row numbering, structural failures
# ---------------------------------------------------------------------------


def test_json_parses_every_valid_item():
    payload = json.dumps(
        [
            {"external_id": "sku-1", "name": "Camry"},
            {"external_id": "sku-2", "name": "Corolla"},
        ]
    ).encode()
    result = parse_import(payload, "application/json")
    assert [row.product.external_id for row in result.rows] == ["sku-1", "sku-2"]
    assert result.total_rows == 2


def test_json_row_numbers_are_1_based_with_no_header_to_offset_past():
    payload = json.dumps(
        [{"external_id": "sku-1", "name": "Camry"}, {"external_id": "sku-2", "name": "Corolla"}]
    ).encode()
    result = parse_import(payload, "application/json")
    assert [row.row for row in result.rows] == [1, 2]


def test_json_top_level_not_a_list_aborts_the_whole_file():
    payload = json.dumps({"external_id": "sku-1", "name": "Camry"}).encode()
    with pytest.raises(ImportParseError, match="array"):
        parse_import(payload, "application/json")


def test_json_invalid_syntax_aborts_the_whole_file():
    with pytest.raises(ImportParseError):
        parse_import(b"{not valid json", "application/json")


def test_json_item_that_is_not_an_object_is_a_row_error():
    payload = json.dumps([{"external_id": "sku-1", "name": "Camry"}, "not an object"]).encode()
    result = parse_import(payload, "application/json")
    assert len(result.rows) == 1
    [error] = result.errors
    assert error.row == 2
    assert "object" in error.message


def test_json_a_bad_row_does_not_abort_the_rest():
    payload = json.dumps(
        [
            {"external_id": "sku-1", "name": "Camry", "price": "not-a-price"},
            {"external_id": "sku-2", "name": "Corolla"},
        ]
    ).encode()
    result = parse_import(payload, "application/json")
    assert [row.product.external_id for row in result.rows] == ["sku-2"]
    assert len(result.errors) == 1
    assert result.errors[0].row == 1


def test_json_row_missing_external_id_key_entirely_is_a_hard_error():
    payload = json.dumps([{"name": "Camry"}]).encode()
    result = parse_import(payload, "application/json")
    assert result.rows == []
    [error] = result.errors
    assert "external_id" in error.message


# ---------------------------------------------------------------------------
# Security-relevant: an import's first phase never lets the source file
# supply its own embedding.
# ---------------------------------------------------------------------------


def test_an_embedding_supplied_in_the_source_json_is_ignored():
    payload = json.dumps(
        [
            {
                "external_id": "sku-1",
                "name": "Camry",
                "embedding": [0.1] * 1536,
                "embedding_model": "attacker-supplied",
            }
        ]
    ).encode()
    [row] = parse_import(payload, "application/json").rows
    assert row.product.embedding is None
    assert row.product.embedding_model is None


# ---------------------------------------------------------------------------
# availability / is_active: blank means "use the default", not an error
# ---------------------------------------------------------------------------


def test_csv_blank_availability_and_is_active_use_their_defaults():
    data = b"external_id,name,availability,is_active\nsku-1,Camry,,\n"
    [row] = parse_import(data, "text/csv").rows
    from app.db.models import ProductAvailability

    assert row.product.availability == ProductAvailability.IN_STOCK
    assert row.product.is_active is True


def test_csv_supplied_availability_and_is_active_are_honoured():
    data = b"external_id,name,availability,is_active\nsku-1,Camry,out_of_stock,false\n"
    [row] = parse_import(data, "text/csv").rows
    from app.db.models import ProductAvailability

    assert row.product.availability == ProductAvailability.OUT_OF_STOCK
    assert row.product.is_active is False


# ---------------------------------------------------------------------------
# Optional blankable scalars: an empty cell means None, not a parse failure
# ---------------------------------------------------------------------------


def test_csv_blank_optional_fields_become_none_not_a_validation_error():
    data = b"external_id,name,price,currency,stock_quantity\nsku-1,Camry,,,\n"
    [row] = parse_import(data, "text/csv").rows
    assert row.product.price is None
    assert row.product.currency is None
    assert row.product.stock_quantity is None


# ---------------------------------------------------------------------------
# Duplicate external_id within one file
# ---------------------------------------------------------------------------


def test_dedupe_keeps_the_last_occurrence_and_flags_the_earlier_one():
    data = _csv("sku-1,First Name,1.00", "sku-1,Second Name,2.00", header="external_id,name,price")
    parsed = parse_import(data, "text/csv").rows
    deduped, duplicate_errors = _dedupe_by_external_id(parsed)

    assert [row.product.name for row in deduped] == ["Second Name"]
    [dup] = duplicate_errors
    assert dup.row == 2  # the EARLIER occurrence is what gets flagged
    assert dup.external_id == "sku-1"
    assert "row 3" in dup.message  # names the later, winning row


def test_dedupe_with_no_duplicates_is_a_no_op():
    data = _csv("sku-1,Camry,1.00", "sku-2,Corolla,2.00", header="external_id,name,price")
    parsed = parse_import(data, "text/csv").rows
    deduped, duplicate_errors = _dedupe_by_external_id(parsed)
    assert deduped == parsed
    assert duplicate_errors == []


# ---------------------------------------------------------------------------
# The succeeded+failed == total_rows invariant `run_import` relies on
# ---------------------------------------------------------------------------


def test_total_rows_equals_valid_plus_invalid_rows_csv():
    data = _csv(
        "sku-1,Camry,32999.00", "sku-2,Corolla,not-a-price", header="external_id,name,price"
    )
    result = parse_import(data, "text/csv")
    assert result.total_rows == len(result.rows) + len(result.errors)


def test_total_rows_equals_valid_plus_invalid_rows_json():
    payload = json.dumps(
        [{"external_id": "sku-1", "name": "Camry"}, {"name": "no external id"}]
    ).encode()
    result = parse_import(payload, "application/json")
    assert result.total_rows == len(result.rows) + len(result.errors)


# ---------------------------------------------------------------------------
# mime type resolution and rejection
# ---------------------------------------------------------------------------


def test_unsupported_mime_type_raises():
    with pytest.raises(UnsupportedImportType):
        parse_import(b"whatever", "application/zip")


@pytest.mark.parametrize(
    ("reported", "filename", "expected"),
    [
        ("", "catalogue.csv", "text/csv"),
        ("application/octet-stream", "catalogue.csv", "text/csv"),
        ("", "catalogue.json", "application/json"),
        ("", "CATALOGUE.CSV", "text/csv"),  # extension match is case-insensitive
    ],
)
def test_resolve_import_mime_type_falls_back_to_the_extension(reported, filename, expected):
    assert resolve_import_mime_type(reported, filename) == expected


def test_resolve_import_mime_type_believes_a_declared_type_over_the_extension():
    assert resolve_import_mime_type("application/json", "catalogue.csv") == "application/json"


def test_row_error_to_dict_round_trips_for_jsonb_storage():
    error = RowError(row=5, external_id="sku-9", message="boom")
    assert error.to_dict() == {"row": 5, "external_id": "sku-9", "message": "boom"}
