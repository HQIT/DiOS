"""Platform operator budget administration, separate from agent-reported usage."""

from fastapi import Depends

from app.runtime.consumption import Consumption
from app.runtime.consumption_contract import AccountUpdate, Settlement
from app.runtime.database import RuntimeErrorCode


def register(router, service):
    dependency = Depends(service)

    def manager(s):
        if s.principal is not None:
            raise RuntimeErrorCode("AUTH_OPERATOR_REQUIRED")
        return Consumption(s.db, s.scope)

    @router.get("/consumption")
    async def status(s=dependency):
        return await manager(s).status()

    @router.put("/consumption/accounts")
    async def account(body: AccountUpdate, s=dependency):
        return await manager(s).put(**body.model_dump())

    @router.post("/consumption/settlements")
    async def settle(body: Settlement, s=dependency):
        return await manager(s).settle(**body.model_dump())
