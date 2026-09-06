import io
import threading
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps

PDF_LOCK = threading.Lock()  # PDFium is not thread-safe.
MAX_PAGES = 15


def render_pages(content: bytes, suffix: str, destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)

    def save_image(image, index):
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((2200, 3000))
        image.save(destination / f"{index}.jpg", "JPEG", quality=88)

    try:
        if suffix == ".pdf":
            with PDF_LOCK:
                doc = pdfium.PdfDocument(content)
                try:
                    count = len(doc)
                    if count < 1 or count > MAX_PAGES:
                        raise ValueError(f"一次最多支持 {MAX_PAGES} 页，请拆分文件；不会丢弃多余页面")
                    for i in range(count):
                        page = doc[i]
                        bitmap = None
                        try:
                            width, height = page.get_size()
                            scale = min(2, 2200 / max(width, 1), 3000 / max(height, 1))
                            bitmap = page.render(scale=scale)
                            save_image(bitmap.to_pil(), i + 1)
                        finally:
                            if bitmap is not None:
                                bitmap.close()
                            page.close()
                finally:
                    doc.close()
                return count
        if suffix not in {".png", ".jpg", ".jpeg"}:
            raise ValueError("请上传 PDF、PNG 或 JPEG")
        with Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("图片过大，请缩小到 4000 万像素以内")
            save_image(image, 1)
        return 1
    except ValueError:
        raise
    except Exception as e:
        raise ValueError("文件无法读取，请检查是否损坏或 PDF 是否需要密码") from e
