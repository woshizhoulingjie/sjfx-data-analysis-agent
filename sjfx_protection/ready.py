"""Immutable provenance for generated results crossing threads/processes."""
import time
import uuid

from . import business, cache
from .store import ProtectionError


def publish_many(namespace, kind, outputs, sources):
    if not business.enabled():
        return outputs
    owner = (business.context() or {}).get('owner')
    records = []
    for target, payload in outputs.items():
        payload['_protection_ready_id'] = uuid.uuid4().hex
        payload['_protection_ready_target'] = str(target)
        payload.setdefault('created_at', time.time())
        key = cache.resource_key(namespace, kind, target, payload['_protection_ready_id'])
        records.append((key, payload, owner))
    cache.record_payloads(records, sources, producer_revision='generated-ready-v1')
    return outputs


def require(namespace, kind, target, payload):
    if not business.enabled():
        return payload
    if (not isinstance(payload, dict) or not payload.get('_protection_ready_id')
            or payload.get('_protection_ready_target') != str(target)):
        raise ProtectionError('预取结果缺少生成时的核准来源，请重新生成', 423,
                              'ready_provenance_missing')
    key = cache.resource_key(namespace, kind, target, payload['_protection_ready_id'])
    cache.require_payload(key, payload)
    return payload
