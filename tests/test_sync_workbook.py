from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "sync_workbook.py"
SPEC = importlib.util.spec_from_file_location("sync_workbook", MODULE_PATH)
assert SPEC and SPEC.loader
sync_workbook = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync_workbook
SPEC.loader.exec_module(sync_workbook)


WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="课程" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

SHEET_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="drawing" Target="../drawings/drawing1.xml"/>
</Relationships>"""

DRAWING_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="image" Target="../media/image1.png"/>
  <Relationship Id="rId2" Type="image" Target="../media/image2.png"/>
</Relationships>"""

DRAWING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <xdr:oneCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:row>0</xdr:row></xdr:from>
    <xdr:pic><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic><xdr:clientData/>
  </xdr:oneCellAnchor>
  <xdr:oneCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:row>1</xdr:row></xdr:from>
    <xdr:pic><xdr:blipFill><a:blip r:embed="rId2"/></xdr:blipFill></xdr:pic><xdr:clientData/>
  </xdr:oneCellAnchor>
</xdr:wsDr>"""


def write_fixture(path: Path, first_value: str, include_second_row: bool) -> None:
    second_row = '<row r="2"><c r="A2" t="inlineStr"><is><t>第二行</t></is></c></row>' if include_second_row else ""
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{first_value}</t></is></c></row>{second_row}</sheetData>
  <drawing r:id="rId1"/>
</worksheet>"""
    drawing_xml = DRAWING_XML if include_second_row else DRAWING_XML.replace(
        '<xdr:oneCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:row>1</xdr:row></xdr:from>\n'
        '    <xdr:pic><xdr:blipFill><a:blip r:embed="rId2"/></xdr:blipFill></xdr:pic><xdr:clientData/>\n'
        '  </xdr:oneCellAnchor>\n',
        "",
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", WORKBOOK_XML)
        archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", SHEET_RELS)
        archive.writestr("xl/drawings/drawing1.xml", drawing_xml)
        archive.writestr("xl/drawings/_rels/drawing1.xml.rels", DRAWING_RELS)
        archive.writestr("xl/media/image1.png", b"same-image-content")
        archive.writestr("xl/media/image2.png", b"same-image-content")


class SyncWorkbookTest(unittest.TestCase):
  def test_forward_fills_a_to_f_without_using_headers(self) -> None:
    rows = {
      1: {"A1": "年级", "B1": "学季"},
      2: {"A2": "三年级", "B2": "暑", "G2": "题型1"},
      3: {"D3": "巧求周长"},
      5: {"A5": "四年级"},
    }

    filled = sync_workbook.forward_fill_rows(rows, {1, 2, 3, 4, 5})

    self.assertEqual(filled[2]["A2"], "三年级")
    self.assertNotIn("C2", filled[2])
    self.assertEqual(filled[3]["A3"], "三年级")
    self.assertEqual(filled[3]["B3"], "暑")
    self.assertEqual(filled[4]["A4"], "三年级")
    self.assertEqual(filled[4]["D4"], "巧求周长")
    self.assertEqual(filled[5]["A5"], "四年级")
    self.assertEqual(filled[5]["B5"], "暑")
    self.assertNotIn("G3", filled[3])

  def test_incremental_rows_and_deduplicated_images(self) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
      root = Path(temporary_directory)
      source = root / "source.xlsx"
      database = root / "app.sqlite"
      store = sync_workbook.LocalAssetStore(root / "assets")

      write_fixture(source, "初始值", include_second_row=True)
      first = sync_workbook.sync_workbook(source, database, store)
      self.assertEqual(first["rows_changed"], 2)
      self.assertEqual(first["assets_uploaded"], 1)

      unchanged = sync_workbook.sync_workbook(source, database, store)
      self.assertTrue(unchanged["skipped"])
      self.assertEqual(unchanged["rows_changed"], 0)

      write_fixture(source, "修改值", include_second_row=False)
      changed = sync_workbook.sync_workbook(source, database, store)
      self.assertFalse(changed["skipped"])
      self.assertEqual(changed["rows_changed"], 1)
      self.assertEqual(changed["rows_deleted"], 1)
      self.assertEqual(changed["assets_uploaded"], 0)

      with closing(sqlite3.connect(database)) as connection:
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM cell_images").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT value FROM cells WHERE cell_ref = 'A1'").fetchone()[0], "修改值")


if __name__ == "__main__":
    unittest.main()