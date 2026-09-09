"""Read bounded, user-uploaded reference content as data for draft analysis."""
import io
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .importer import scalar, workbook

MAX_TEXT = 60000


def reference_text(content: bytes, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".json", ".sql", ".csv"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("gb18030")
            except UnicodeDecodeError as exc:
                raise ValueError("参考文件编码无法读取，请另存为 UTF-8") from exc
    elif suffix == ".xlsx":
        wb = workbook(content)
        try:
            sheets = []
            for ws in wb:
                if ws.max_row > 1000 or ws.max_column > 100:
                    raise ValueError("参考 Excel 每张表最多 1,000 行、100 列，请只上传相关字段资料")
                rows = []
                for row_number, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    values = [scalar(value) for value in row]
                    while values and values[-1] in {None, ""}:
                        values.pop()
                    if any(value not in {None, ""} for value in values):
                        rows.append({"row": row_number, "values": values})
                if rows:
                    sheets.append({"sheet": ws.title, "rows": rows})
            text = json.dumps(sheets, ensure_ascii=False)
        finally:
            wb.close()
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if len(archive.infolist()) > 3000 or sum(i.file_size for i in archive.infolist()) > 50 * 1024 * 1024:
                    raise ValueError("Word 解压内容过大")
                xml = archive.read("word/document.xml")
            if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                raise ValueError("Word 含不支持的 XML 声明")
            root = ElementTree.fromstring(xml)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            text = "\n".join("".join(t.text or "" for t in p.findall(".//w:t", ns)) for p in root.findall(".//w:p", ns))
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
            raise ValueError("请上传有效的 .docx 文件") from exc
    else:
        raise ValueError("参考资料支持 XLSX、DOCX、TXT、MD、JSON、CSV 或 SQL；PDF/图片请上传到样张区")
    if not text.strip():
        raise ValueError("参考文件中没有可读取的文字；扫描件请作为样张上传")
    if len(text) > MAX_TEXT:
        raise ValueError("参考文字超过 60,000 字符，请缩小文件范围；不会静默截断")
    return text
