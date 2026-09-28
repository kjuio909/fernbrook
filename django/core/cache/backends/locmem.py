"Thread-safe in-memory cache backend."

import pickle
import time
from collections import OrderedDict
from threading import Lock

from django.core.cache.backends.base import DEFAULT_TIMEOUT, BaseCache

# Global in-memory store of cache data. Keyed by name, to provide
# multiple named local memory caches.
_caches = {}
_expire_info = {}
_locks = {}


class LocMemCache(BaseCache):
    pickle_protocol = pickle.HIGHEST_PROTOCOL

    def __init__(self, name, params):
        super().__init__(params)
        self._cache = _caches.setdefault(name, OrderedDict())
        self._expire_info = _expire_info.setdefault(name, {})
        self._lock = _locks.setdefault(name, Lock())

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_and_validate_key(key, version=version)
        pickled = pickle.dumps(value, self.pickle_protocol)
        # Hold the coordination lock across the insertion (and a possible
        # cull) so a generation's commit fence and an eviction can't race.
        with self._aget_or_set_lock:
            with self._lock:
                if self._has_expired(key):
                    self._set(key, pickled, timeout)
                    return True
                return False

    def get(self, key, default=None, version=None):
        key = self.make_and_validate_key(key, version=version)
        with self._lock:
            if self._has_expired(key):
                self._delete(key)
                return default
            pickled = self._cache[key]
            self._cache.move_to_end(key, last=False)
        return pickle.loads(pickled)

    def _set(self, key, value, timeout=DEFAULT_TIMEOUT):
        if len(self._cache) >= self._max_entries:
            self._cull()
        self._cache[key] = value
        self._cache.move_to_end(key, last=False)
        self._expire_info[key] = self.get_backend_timeout(timeout)

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_and_validate_key(key, version=version)
        pickled = pickle.dumps(value, self.pickle_protocol)
        # Hold the coordination lock across the write (and a possible cull):
        # a cull can evict an entry whose generation is in flight, and the
        # round must be detached while commits are fenced off.
        with self._aget_or_set_lock:
            with self._lock:
                self._set(key, pickled, timeout)
            # An unconditional write outlives any generation in flight: if it
            # is later deleted or expires, a later miss must not rejoin that
            # round.
            self._note_aget_or_set_repopulation(key)

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_and_validate_key(key, version=version)
        with self._lock:
            if self._has_expired(key):
                return False
            self._expire_info[key] = self.get_backend_timeout(timeout)
            return True

    def incr(self, key, delta=1, version=None):
        key = self.make_and_validate_key(key, version=version)
        with self._lock:
            if self._has_expired(key):
                self._delete(key)
                raise ValueError("Key '%s' not found" % key)
            pickled = self._cache[key]
            value = pickle.loads(pickled)
            new_value = value + delta
            pickled = pickle.dumps(new_value, self.pickle_protocol)
            self._cache[key] = pickled
            self._cache.move_to_end(key, last=False)
        return new_value

    def has_key(self, key, version=None):
        key = self.make_and_validate_key(key, version=version)
        with self._lock:
            if self._has_expired(key):
                self._delete(key)
                return False
            return True

    def _has_expired(self, key):
        exp = self._expire_info.get(key, -1)
        return exp is not None and exp <= time.time()

    def _cull(self):
        # Called from set()/add(), which already hold the coordination lock.
        if self._cull_frequency == 0:
            # A full cull is equivalent to clear(): every entry (and thus
            # every registered round) is invalidated. Partial culls can only
            # evict entries already committed; an in-flight entry is present
            # solely via an unconditional external write, which set its
            # round's repopulated flag and keeps the generation's commit
            # fenced off without detaching the round.
            self._cache.clear()
            self._expire_info.clear()
            self._retire_aget_or_set_all_locked()
        else:
            count = len(self._cache) // self._cull_frequency
            for i in range(count):
                key, _ = self._cache.popitem()
                del self._expire_info[key]

    def _delete(self, key):
        try:
            del self._cache[key]
            del self._expire_info[key]
        except KeyError:
            return False
        return True

    def delete(self, key, version=None):
        with self._retiring_aget_or_set(key, version) as made_key:
            with self._lock:
                return self._delete(made_key)

    def clear(self):
        with self._retiring_all_aget_or_set():
            with self._lock:
                self._cache.clear()
                self._expire_info.clear()
