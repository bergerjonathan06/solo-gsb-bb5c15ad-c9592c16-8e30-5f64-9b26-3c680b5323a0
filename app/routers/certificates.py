from fastapi import APIRouter, Depends, HTTPException
from sqlite3 import Connection

from .. import crud
from ..database import get_db
from ..schemas import CertificateIn, CertificateOut

router = APIRouter(prefix="/api/certificates", tags=["certificates"])


def _row(r):
    return {k: r[k] for k in (
        "id", "instrument_code", "cert_number", "issued_at", "valid_from",
        "valid_until", "uncertainty", "unit", "digest_alg",
        "signature_digest", "signed_payload", "created_at",
    )}


@router.post("", response_model=CertificateOut, status_code=201)
def create_or_update_certificate(
    body: CertificateIn, db: Connection = Depends(get_db)
):
    try:
        crud.upsert_certificate(db, body)
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))
    return _row(crud.get_certificate(db, body.id))


@router.get("", response_model=list[CertificateOut])
def list_certificates(
    instrument_code: str | None = None, db: Connection = Depends(get_db)
):
    return [_row(r) for r in crud.list_certificates(db, instrument_code)]


@router.get("/{cert_id}", response_model=CertificateOut)
def get_certificate(cert_id: str, db: Connection = Depends(get_db)):
    row = crud.get_certificate(db, cert_id)
    if row is None:
        raise HTTPException(404, f"证书不存在: {cert_id}")
    return _row(row)
