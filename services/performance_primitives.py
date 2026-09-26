"""Bounded, output-preserving helpers; no model, storage or startup side effects."""
from collections import OrderedDict
import threading


class WeightedLRU:
    """Thread-safe LRU bounded by both entries and retained input characters."""
    def __init__(self, capacity=256, max_weight=2_000_000):
        self.capacity = max(1, int(capacity))
        self.max_weight = max(1, int(max_weight))
        self._items = OrderedDict()
        self._weight = 0
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            self._items.move_to_end(key)
            return entry[0]

    def put(self, key, value, weight=1):
        weight = max(1, int(weight))
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._weight -= previous[1]
            if weight > self.max_weight:
                return
            self._items[key] = (value, weight)
            self._weight += weight
            while len(self._items) > self.capacity or self._weight > self.max_weight:
                _, removed = self._items.popitem(last=False)
                self._weight -= removed[1]

    def clear(self):
        with self._lock:
            self._items.clear()
            self._weight = 0


def parse_order(items, enabled=True):
    """Start likely long jobs early; indices keep output/source order unchanged."""
    def size(index):
        try:
            return max(0, int(items[index].get('size') or 0))
        except (TypeError, ValueError, OverflowError):
            return 0
    indices = list(range(len(items)))
    return sorted(indices, key=lambda index: (-size(index), index)) if enabled else indices


def unique_embedding_paths(paths, cache_keys):
    """One vector per identical content/profile key; preserve first-seen order."""
    representatives, aliases = [], {}
    for path in paths:
        key = cache_keys[path]
        if key not in aliases:
            representatives.append(path)
            aliases[key] = []
        aliases[key].append(path)
    return representatives, aliases
