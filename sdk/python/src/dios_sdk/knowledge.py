"""Provider structural interface; bind trusted authorization outside tool input.

Implementations advertise supported operations. Search returns exact source and
object versions; mutations distinguish accepted from applied. See the versioned
knowledge Provider contract. No model, database or business schema dependency.
"""
from typing import Protocol

CONTRACT = 'dios.knowledge/v1'


class KnowledgeProvider(Protocol):
    def capabilities(self) -> dict: ...
    async def search(self, query: str, limit: int = 3) -> dict: ...
    async def source(self, source_id: str) -> dict: ...
    async def change(self, source_id: str, body: dict) -> dict: ...
