from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import HTTPException, UploadFile, status
from pypdf import PdfReader


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


async def resolve_input_file(
    *,
    upload: UploadFile | None,
    source_path: str | None,
    suffix: str = "",
) -> Path:
    if upload is not None:
        temp = NamedTemporaryFile(delete=False, suffix=suffix or Path(upload.filename or "upload").suffix)
        temp.write(await upload.read())
        temp.close()
        return Path(temp.name)
    if source_path:
        path = Path(source_path)
        if not path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No existe el archivo: {source_path}")
        return path
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Debes enviar un archivo o source_path")


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)
