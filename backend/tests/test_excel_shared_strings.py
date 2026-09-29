from io import BytesIO

import pytest

from app.services import excel_profile_reader as reader


def test_shared_strings_parse_only_requested_values_and_preserve_rich_text(monkeypatch):
    content = (f'<sst xmlns="{reader.SHEET_MAIN_NS}"><si><r><t>Rich </t></r><r><t>text</t></r></si>'
               '<si><t>unused</t></si><si><t>_x005F_value</t></si>'
               + '<si><t>unused tail</t></si>' * 10_000 + '</sst>')
    parsed = []
    original = reader.Text.from_tree
    def parse(node):
        parsed.append(1)
        return original(node)
    monkeypatch.setattr(reader.Text, 'from_tree', parse)
    assert reader._read_selected_strings(BytesIO(content.encode()), {0, 2}) == {0:'Rich text',2:'_value'}
    assert len(parsed) == 2


def test_missing_shared_string_is_rejected():
    with pytest.raises(ValueError, match='不存在'):
        reader._read_selected_strings(BytesIO(f'<sst xmlns="{reader.SHEET_MAIN_NS}"><si><t>x</t></si></sst>'.encode()), {2})


def test_string_discovery_is_bounded_by_physical_row_and_column():
    source = f'<worksheet xmlns="{reader.SHEET_MAIN_NS}"><sheetData><row r="1"><c r="A1" t="s"><v>2</v></c></row><row r="100000"><c r="A100000" t="s"><v>1000</v></c></row></sheetData></worksheet>'
    assert reader._sample_string_indices(BytesIO(source.encode()),10,5) == {2}
    with pytest.raises(ValueError, match='列数'):
        reader._sample_string_indices(BytesIO(source.replace('A1"','Z1"').encode()),10,5)
