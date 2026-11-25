"""Deployment-selected adapters; query input cannot select or downgrade a provider."""
from app.config import settings
from app.runtime.database import RuntimeErrorCode
from .knowledge import ObjectKnowledgeProvider
from .knowledge_fts import SQLiteFtsKnowledgeProvider


def knowledge_provider(console):
    implementations = {
        'object-read-through-v1': ObjectKnowledgeProvider,
        'sqlite-fts5-v1': SQLiteFtsKnowledgeProvider,
    }
    implementation = implementations.get(settings.runtime_knowledge_provider)
    if implementation is None:
        raise RuntimeErrorCode('KNOWLEDGE_PROVIDER_UNAVAILABLE')
    return implementation(console)
