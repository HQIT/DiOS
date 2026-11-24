"""Request-bound tool API authorization, separate from automatic Worker controls."""
import asyncio
from app.runtime.database import RuntimeErrorCode

class AuthorizedTools:
    def __init__(self,console):self.console=console;self.gate=console.tools
    async def list(self,run_id):
        await self.console.authorize(run_id,'tool.read');return await self.gate.list(run_id)
    async def get(self,run_id,call_id):
        await self.console.authorize(run_id,'tool.read');return await self.gate.get(run_id,call_id)
    async def decide(self,run_id,call_id,version,decision,token,reason):
        await self.console.authorize(run_id,'tool.approve')
        expected=self.console.principal['subject'] if self.console.principal else None
        return await self.gate.decide(run_id,call_id,version,decision,token,reason,required_subject=expected,access_guard=lambda run:self.console.check_access(run,'tool.approve'))
    async def operator_action(self,name,run_id,*args):
        if self.console.principal is not None:raise RuntimeErrorCode('AUTH_ACTION_DENIED')
        await self.console.authorize(run_id,'run.read')
        return await getattr(self.gate,name)(run_id,*args)
    async def request(self,run_id,*args):return await self.operator_action('request',run_id,*args)
    async def dispatch(self,run_id,*args):return await self.operator_action('dispatch',run_id,*args)
    async def reconcile_overdue(self,run_id,*args):return await self.operator_action('reconcile_overdue',run_id,*args)
    async def recover_receipt(self,run_id,*args):return await self.operator_action('recover_receipt',run_id,*args)
