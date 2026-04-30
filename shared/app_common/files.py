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
    full_text = []

    for page in reader.pages:
        page_text = page.extract_text() or ""
        
        # Extracción segura de enlaces (evita el Error 500)
        try:
            if "/Annots" in page:
                annots = page["/Annots"]
                # Resolvemos la referencia indirecta si existe
                if hasattr(annots, "get_object"):
                    annots = annots.get_object()
                
                # Verificamos que sea una lista antes de iterar
                if isinstance(annots, list):
                    for annot in annots:
                        if hasattr(annot, "get_object"):
                            annot_obj = annot.get_object()
                            if "/A" in annot_obj and "/URI" in annot_obj["/A"]:
                                uri = str(annot_obj["/A"]["/URI"])
                                if uri.startswith("mailto:"):
                                    email = uri.replace("mailto:", "").strip()
                                    page_text += f" {email} "
        except Exception:
            # Si hay un error estructural en los enlaces de la página, lo ignoramos y seguimos
            pass

        full_text.append(page_text)

    return "\n".join(full_text)