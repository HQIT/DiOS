"""Read-only package catalog for trusted platform operators."""

from fastapi import Depends

from app.runtime.database import RuntimeErrorCode
from app.runtime.package_catalog import PackageCatalog


def register(router, service):
    dependency = Depends(service)

    def manager(s):
        if s.principal is not None:
            raise RuntimeErrorCode("AUTH_OPERATOR_REQUIRED")
        return PackageCatalog(s.db, s.scope)

    @router.get("/packages")
    async def packages(
        q: str = "",
        kind: str = "",
        version: str = "",
        admission: str = "",
        after: str = "",
        limit: int = 50,
        s=dependency,
    ):
        return await manager(s).list(
            q=q,
            kind=kind,
            version=version,
            admission=admission,
            after=after,
            limit=limit,
        )

    @router.get("/packages/{checksum}/releases")
    async def releases(checksum: str, after: str = "", limit: int = 50, s=dependency):
        return await manager(s).releases(checksum, after=after, limit=limit)

    @router.get("/packages/{checksum}")
    async def detail(checksum: str, s=dependency):
        return await manager(s).detail(checksum)
