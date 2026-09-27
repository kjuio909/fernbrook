"Base Cache class."

import asyncio
import inspect
import threading
import time
import warnings

from asgiref.sync import sync_to_async

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string
from django.utils.regex_helper import _lazy_re_compile


class InvalidCacheBackendError(ImproperlyConfigured):
    pass


class CacheKeyWarning(RuntimeWarning):
    pass


class InvalidCacheKey(ValueError):
    pass


class _InFlightGeneration:
    """
    Coordination point for a single round of concurrent get_or_set() misses.

    The leading thread runs the default generation and reports the outcome:
    either the committed value or the exception raised by the default. Every
    other thread coalescing on the same cache entry waits for the round to
    finish and observes that very same outcome.
    """

    def __init__(self):
        self.event = threading.Event()
        self.value = None
        self.exception = None
        # Number of threads currently waiting for the leading thread.
        self.waiters = 0

    def wait(self):
        self.event.wait()
        if self.exception is not None:
            raise self.exception
        return self.value

    def finish(self, value):
        self.value = value
        self.event.set()

    def fail(self, exc):
        self.exception = exc
        self.event.set()


class _AsyncInFlightGeneration:
    """
    Coordination point for a single round of concurrent aget_or_set() misses.

    A dedicated task runs the default generation and holds the round's single
    outcome: a value or an exception. Every other caller coalescing on the
    same cache entry joins the round and awaits that very same task, so
    cancelling one waiter cannot cancel a generation that still has
    participants.

    ``participants`` counts callers that have joined the round and not yet left
    it. When it reaches zero the round is detached from the registry before
    the generation task is abandoned, so a cancelled round can never be
    mistaken for the next one and no later caller can observe a half-abandoned
    task.
    """

    __slots__ = ("task", "participants")

    def __init__(self, coro):
        self.task = asyncio.ensure_future(coro)
        self.participants = 1

    def join(self):
        self.participants += 1


def _discard_generation_result(task):
    # Retire the outcome of a generation task once the round is over so an
    # abandoned failure isn't reported as "exception was never retrieved". The
    # outcome belongs to whoever awaited the round; once nobody is left there
    # is no one to surface it to.
    if not task.cancelled():
        task.exception()


# Stub class to ensure not passing in a `timeout` argument results in
# the default timeout
DEFAULT_TIMEOUT = object()

# Memcached does not accept keys longer than this.
MEMCACHE_MAX_KEY_LENGTH = 250


def default_key_func(key, key_prefix, version):
    """
    Default function to generate keys.

    Construct the key used by all other methods. By default, prepend
    the `key_prefix`. KEY_FUNCTION can be used to specify an alternate
    function with custom key making behavior.
    """
    return "%s:%s:%s" % (key_prefix, version, key)


def get_key_func(key_func):
    """
    Function to decide which key function to use.

    Default to ``default_key_func``.
    """
    if key_func is not None:
        if callable(key_func):
            return key_func
        else:
            return import_string(key_func)
    return default_key_func


class BaseCache:
    _missing_key = object()

    def __init__(self, params):
        timeout = params.get("timeout", params.get("TIMEOUT", 300))
        if timeout is not None:
            try:
                timeout = int(timeout)
            except (ValueError, TypeError):
                timeout = 300
        self.default_timeout = timeout

        options = params.get("OPTIONS", {})
        max_entries = params.get("max_entries", options.get("MAX_ENTRIES", 300))
        try:
            self._max_entries = int(max_entries)
        except (ValueError, TypeError):
            self._max_entries = 300

        cull_frequency = params.get("cull_frequency", options.get("CULL_FREQUENCY", 3))
        try:
            self._cull_frequency = int(cull_frequency)
        except (ValueError, TypeError):
            self._cull_frequency = 3

        self.key_prefix = params.get("KEY_PREFIX", "")
        self.version = params.get("VERSION", 1)
        self.key_func = get_key_func(params.get("KEY_FUNCTION"))
        # In-flight aget_or_set() default generations, keyed by the fully
        # constructed cache key so that versions (and custom key functions)
        # are treated as distinct cache entries.
        self._aget_or_set_in_flight = {}
        # In-flight get_or_set() default generations, likewise keyed by the
        # fully constructed cache key. Each entry is an _InFlightGeneration
        # shared by all threads coalescing on the same cache entry.
        self._get_or_set_in_flight = {}
        self._get_or_set_lock = threading.Lock()

    def get_backend_timeout(self, timeout=DEFAULT_TIMEOUT):
        """
        Return the timeout value usable by this backend based upon the provided
        timeout.
        """
        if timeout == DEFAULT_TIMEOUT:
            timeout = self.default_timeout
        elif timeout == 0:
            # ticket 21147 - avoid time.time() related precision issues
            timeout = -1
        return None if timeout is None else time.time() + timeout

    def make_key(self, key, version=None):
        """
        Construct the key used by all other methods. By default, use the
        key_func to generate a key (which, by default, prepends the
        `key_prefix' and 'version'). A different key function can be provided
        at the time of cache construction; alternatively, you can subclass the
        cache backend to provide custom key making behavior.
        """
        if version is None:
            version = self.version

        return self.key_func(key, self.key_prefix, version)

    def validate_key(self, key):
        """
        Warn about keys that would not be portable to the memcached
        backend. This encourages (but does not force) writing backend-portable
        cache code.
        """
        for warning in memcache_key_warnings(key):
            warnings.warn(warning, CacheKeyWarning)

    def make_and_validate_key(self, key, version=None):
        """Helper to make and validate keys."""
        key = self.make_key(key, version=version)
        self.validate_key(key)
        return key

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        """
        Set a value in the cache if the key does not already exist. If
        timeout is given, use that timeout for the key; otherwise use the
        default cache timeout.

        Return True if the value was stored, False otherwise.
        """
        raise NotImplementedError(
            "subclasses of BaseCache must provide an add() method"
        )

    async def aadd(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return await sync_to_async(self.add, thread_sensitive=True)(
            key, value, timeout, version
        )

    def get(self, key, default=None, version=None):
        """
        Fetch a given key from the cache. If the key does not exist, return
        default, which itself defaults to None.
        """
        raise NotImplementedError("subclasses of BaseCache must provide a get() method")

    async def aget(self, key, default=None, version=None):
        return await sync_to_async(self.get, thread_sensitive=True)(
            key, default, version
        )

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        """
        Set a value in the cache. If timeout is given, use that timeout for the
        key; otherwise use the default cache timeout.
        """
        raise NotImplementedError("subclasses of BaseCache must provide a set() method")

    async def aset(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return await sync_to_async(self.set, thread_sensitive=True)(
            key, value, timeout, version
        )

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        """
        Update the key's expiry time using timeout. Return True if successful
        or False if the key does not exist.
        """
        raise NotImplementedError(
            "subclasses of BaseCache must provide a touch() method"
        )

    async def atouch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        return await sync_to_async(self.touch, thread_sensitive=True)(
            key, timeout, version
        )

    def delete(self, key, version=None):
        """
        Delete a key from the cache and return whether it succeeded, failing
        silently.
        """
        raise NotImplementedError(
            "subclasses of BaseCache must provide a delete() method"
        )

    async def adelete(self, key, version=None):
        return await sync_to_async(self.delete, thread_sensitive=True)(key, version)

    def get_many(self, keys, version=None):
        """
        Fetch a bunch of keys from the cache. For certain backends (memcached,
        pgsql) this can be *much* faster when fetching multiple values.

        Return a dict mapping each key in keys to its value. If the given
        key is missing, it will be missing from the response dict.
        """
        d = {}
        for k in keys:
            val = self.get(k, self._missing_key, version=version)
            if val is not self._missing_key:
                d[k] = val
        return d

    async def aget_many(self, keys, version=None):
        """See get_many()."""
        if self.get_many.__func__ is not BaseCache.get_many:
            return await sync_to_async(self.get_many, thread_sensitive=True)(
                keys, version=version
            )
        d = {}
        for k in keys:
            val = await self.aget(k, self._missing_key, version=version)
            if val is not self._missing_key:
                d[k] = val
        return d

    def get_or_set(self, key, default, timeout=DEFAULT_TIMEOUT, version=None):
        """
        Fetch a given key from the cache. If the key does not exist,
        add the key and set it to the default value. The default value can
        also be any callable. If timeout is given, use that timeout for the
        key; otherwise use the default cache timeout.

        Return the value of the key stored or retrieved.
        """
        made_key = self.make_key(key, version=version)
        # Coalesce concurrent misses for the same cache entry: only the first
        # thread generates the default, every other concurrent thread waits
        # for (and receives) the very same result or failure. Different keys
        # (including different versions) have separate in-flight generations
        # and never block each other. The lock guards the registry only; it
        # is never held while the default runs.
        with self._get_or_set_lock:
            in_flight = self._get_or_set_in_flight.get(made_key)
            if in_flight is None:
                in_flight = _InFlightGeneration()
                self._get_or_set_in_flight[made_key] = in_flight
                leader = True
            else:
                in_flight.waiters += 1
                leader = False
        if not leader:
            try:
                return in_flight.wait()
            finally:
                with self._get_or_set_lock:
                    in_flight.waiters -= 1
        try:
            result = self._get_or_set_generate(key, default, timeout, version)
        except BaseException as exc:
            in_flight.fail(exc)
            raise
        else:
            in_flight.finish(result)
            return result
        finally:
            with self._get_or_set_lock:
                self._get_or_set_in_flight.pop(made_key, None)

    def _get_or_set_generate(self, key, default, timeout, version):
        val = self.get(key, self._missing_key, version=version)
        if val is not self._missing_key:
            # The entry was populated before this generation started; return
            # it without resetting its expiry.
            return val
        if callable(default):
            default = default()
        self.add(key, default, timeout=timeout, version=version)
        # Fetch the value again to avoid a race condition if another writer
        # added a value between the first get() and the add() above: the value
        # already in the cache wins and must not be overwritten.
        return self.get(key, default, version=version)

    async def aget_or_set(self, key, default, timeout=DEFAULT_TIMEOUT, version=None):
        """See get_or_set()."""
        if self.get_or_set.__func__ is not BaseCache.get_or_set:
            return await sync_to_async(self.get_or_set, thread_sensitive=True)(
                key, default, timeout=timeout, version=version
            )
        made_key = self.make_key(key, version=version)
        # Coalesce concurrent misses for the same cache entry: only one
        # generation runs per round, and every participant observes its very
        # same value or failure. Different keys (including different versions)
        # have separate rounds and never block each other. The registry is
        # only touched at synchronous points here, so it needs no lock.
        in_flight = self._aget_or_set_in_flight.get(made_key)
        if in_flight is not None and in_flight.task.done():
            # The registered round already reached its outcome but its
            # participants haven't finished unwinding, so it hasn't been
            # cleaned up yet. A new miss request must not join a finished
            # round — inheriting a stale failure (or an uncommitted result)
            # would make the outcome depend on event loop scheduling. Retire
            # the round now so this call re-determines the state of the
            # cache entry from scratch. The finished round's participants
            # still hold their own reference to it and observe its outcome;
            # the identity checks below keep their cleanup away from the
            # round started here.
            self._aget_or_set_in_flight.pop(made_key, None)
            _discard_generation_result(in_flight.task)
            in_flight = None
        if in_flight is not None:
            in_flight.join()
        else:
            in_flight = _AsyncInFlightGeneration(
                self._aget_or_set_generate(key, default, timeout, version)
            )
            self._aget_or_set_in_flight[made_key] = in_flight
        try:
            # Shield the shared generation task from this participant's
            # cancellation. Whether a still-running generation is abandoned
            # is decided below by the participant count, never by a single
            # participant's cancellation.
            return await asyncio.shield(in_flight.task)
        finally:
            in_flight.participants -= 1
            task = in_flight.task
            if task.done():
                # The round reached an outcome. Only detach the generation of
                # the current round (identity checked), so a round that
                # started after the outcome became visible is untouched.
                if self._aget_or_set_in_flight.get(made_key) is in_flight:
                    self._aget_or_set_in_flight.pop(made_key, None)
                    _discard_generation_result(task)
            elif in_flight.participants == 0:
                # The last participant left while the generation was still
                # running, which can only be its own cancellation. Detach the
                # round from the registry before abandoning it, so the next
                # caller starts a fresh round and can never observe this one.
                # The generation never deletes entries, so a value committed
                # by another writer is left in place.
                if self._aget_or_set_in_flight.get(made_key) is in_flight:
                    self._aget_or_set_in_flight.pop(made_key, None)
                task.add_done_callback(_discard_generation_result)
                task.cancel()

    async def _aget_or_set_generate(self, key, default, timeout, version):
        val = await self.aget(key, self._missing_key, version=version)
        if val is not self._missing_key:
            # The entry was populated before this generation started; return
            # it without resetting its expiry.
            return val
        if callable(default):
            default = await self._acall_default(default)
        await self.aadd(key, default, timeout=timeout, version=version)
        # Fetch the value again to avoid a race condition if another writer
        # added a value between the first aget() and the aadd() above: the
        # value already in the cache wins and must not be overwritten.
        return await self.aget(key, default, version=version)

    async def _acall_default(self, default):
        result = default()
        if inspect.isawaitable(result):
            result = await result
        return result

    def _retire_aget_or_set_in_flight(self, key, version=None):
        """
        Retire any in-flight aget_or_set() generation round for the key.

        Deleting the entry invalidates the round that was generating its
        value: a later miss must confirm the state of the cache entry from
        scratch instead of joining a round whose outcome predates the
        deletion. Only the round registered right now is retired; a round
        started after the deletion is untouched. The detached round keeps
        running for its remaining participants, who still share its
        outcome, but its late result can no longer be observed by new
        callers, and its commit (an add()) can't overwrite a value written
        after this point.
        """
        made_key = self.make_key(key, version=version)
        in_flight = self._aget_or_set_in_flight.pop(made_key, None)
        if in_flight is not None and in_flight.task.done():
            _discard_generation_result(in_flight.task)

    def has_key(self, key, version=None):
        """
        Return True if the key is in the cache and has not expired.
        """
        return (
            self.get(key, self._missing_key, version=version) is not self._missing_key
        )

    async def ahas_key(self, key, version=None):
        if self.has_key.__func__ is not BaseCache.has_key:
            return await sync_to_async(self.has_key, thread_sensitive=True)(
                key, version=version
            )
        return (
            await self.aget(key, self._missing_key, version=version)
            is not self._missing_key
        )

    def incr(self, key, delta=1, version=None):
        """
        Add delta to value in the cache. If the key does not exist, raise a
        ValueError exception.
        """
        value = self.get(key, self._missing_key, version=version)
        if value is self._missing_key:
            raise ValueError("Key '%s' not found" % key)
        new_value = value + delta
        self.set(key, new_value, version=version)
        return new_value

    async def aincr(self, key, delta=1, version=None):
        """See incr()."""
        if self.incr.__func__ is not BaseCache.incr:
            return await sync_to_async(self.incr, thread_sensitive=True)(
                key, delta=delta, version=version
            )
        value = await self.aget(key, self._missing_key, version=version)
        if value is self._missing_key:
            raise ValueError("Key '%s' not found" % key)
        new_value = value + delta
        await self.aset(key, new_value, version=version)
        return new_value

    def decr(self, key, delta=1, version=None):
        """
        Subtract delta from value in the cache. If the key does not exist,
        raise a ValueError exception.
        """
        return self.incr(key, -delta, version=version)

    async def adecr(self, key, delta=1, version=None):
        return await self.aincr(key, -delta, version=version)

    def __contains__(self, key):
        """
        Return True if the key is in the cache and has not expired.
        """
        # This is a separate method, rather than just a copy of has_key(),
        # so that it always has the same functionality as has_key(), even
        # if a subclass overrides it.
        return self.has_key(key)

    def set_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        """
        Set a bunch of values in the cache at once from a dict of key/value
        pairs. For certain backends (memcached), this is much more efficient
        than calling set() multiple times.

        If timeout is given, use that timeout for the key; otherwise use the
        default cache timeout.

        On backends that support it, return a list of keys that failed
        insertion, or an empty list if all keys were inserted successfully.
        """
        for key, value in data.items():
            self.set(key, value, timeout=timeout, version=version)
        return []

    async def aset_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        if self.set_many.__func__ is not BaseCache.set_many:
            return await sync_to_async(self.set_many, thread_sensitive=True)(
                data, timeout=timeout, version=version
            )
        for key, value in data.items():
            await self.aset(key, value, timeout=timeout, version=version)
        return []

    def delete_many(self, keys, version=None):
        """
        Delete a bunch of values in the cache at once. For certain backends
        (memcached), this is much more efficient than calling delete() multiple
        times.
        """
        for key in keys:
            self.delete(key, version=version)

    async def adelete_many(self, keys, version=None):
        if self.delete_many.__func__ is not BaseCache.delete_many:
            return await sync_to_async(self.delete_many, thread_sensitive=True)(
                keys, version=version
            )
        for key in keys:
            await self.adelete(key, version=version)

    def clear(self):
        """Remove *all* values from the cache at once."""
        raise NotImplementedError(
            "subclasses of BaseCache must provide a clear() method"
        )

    async def aclear(self):
        return await sync_to_async(self.clear, thread_sensitive=True)()

    def incr_version(self, key, delta=1, version=None):
        """
        Add delta to the cache version for the supplied key. Return the new
        version.
        """
        if version is None:
            version = self.version

        value = self.get(key, self._missing_key, version=version)
        if value is self._missing_key:
            raise ValueError("Key '%s' not found" % key)

        self.set(key, value, version=version + delta)
        self.delete(key, version=version)
        return version + delta

    async def aincr_version(self, key, delta=1, version=None):
        """See incr_version()."""
        if self.incr_version.__func__ is not BaseCache.incr_version:
            return await sync_to_async(self.incr_version, thread_sensitive=True)(
                key, delta=delta, version=version
            )
        if version is None:
            version = self.version

        value = await self.aget(key, self._missing_key, version=version)
        if value is self._missing_key:
            raise ValueError("Key '%s' not found" % key)

        await self.aset(key, value, version=version + delta)
        await self.adelete(key, version=version)
        return version + delta

    def decr_version(self, key, delta=1, version=None):
        """
        Subtract delta from the cache version for the supplied key. Return the
        new version.
        """
        return self.incr_version(key, -delta, version)

    async def adecr_version(self, key, delta=1, version=None):
        return await self.aincr_version(key, -delta, version)

    def close(self, **kwargs):
        """Close the cache connection"""
        pass

    async def aclose(self, **kwargs):
        return await sync_to_async(self.close, thread_sensitive=True)(**kwargs)


memcached_error_chars_re = _lazy_re_compile(r"[\x00-\x20\x7f]")


def memcache_key_warnings(key):
    if len(key) > MEMCACHE_MAX_KEY_LENGTH:
        yield (
            "Cache key will cause errors if used with memcached: %r "
            "(longer than %s)" % (key, MEMCACHE_MAX_KEY_LENGTH)
        )
    if memcached_error_chars_re.search(key):
        yield (
            "Cache key contains characters that will cause errors if used with "
            f"memcached: {key!r}"
        )
