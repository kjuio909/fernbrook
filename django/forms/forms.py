"""
Form classes
"""

import asyncio
import copy
import datetime
import inspect

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.forms.fields import Field
from django.forms.utils import ErrorDict, ErrorList, RenderableFormMixin
from django.forms.widgets import Media, MediaDefiningClass
from django.utils.datastructures import MultiValueDict
from django.utils.translation import gettext as _

from .renderers import get_default_renderer

__all__ = ("BaseForm", "Form")

# Marker for "cleaned_data was never populated" (accessing it raises
# AttributeError, as on a form that has never been validated).
_UNSET = object()


def _snapshot_mapping(mapping):
    """Return a detached shallow copy of a bound data/files mapping.

    ``MultiValueDict`` copies keep their multi-value semantics; the mapping
    setters replace per-key value lists rather than mutating them, so data
    rebound or changed after the snapshot was taken cannot reach the copy.
    Plain mappings are copied with ``dict()``.
    """
    if isinstance(mapping, MultiValueDict):
        return copy.copy(mapping)
    return dict(mapping)


def _mapping_fingerprint(mapping):
    """Build a value-based, order-preserving fingerprint of a mapping.

    Equality compares the contained values (``==``), so in-place mutations of
    bound data invalidate a cached validation round even when the mapping
    object's identity is unchanged.
    """
    if isinstance(mapping, MultiValueDict):
        return tuple((key, tuple(values)) for key, values in mapping.lists())
    return tuple((key, mapping[key]) for key in mapping)


def _freeze_fingerprint_value(value):
    """Return an immutable snapshot of a fingerprint component.

    Mutable containers are copied so later in-place mutations (e.g.
    appending a validator or updating choices) change the live objects
    without retroactively changing the stored fingerprint.
    """
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_fingerprint_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_fingerprint_value(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (key, _freeze_fingerprint_value(item))
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        )
    return value


def _field_fingerprint(field):
    """Structural fingerprint of a field definition.

    Covers every instance attribute (validators, error messages,
    choices, input formats, coercion callables, subfields and so on) so
    that configuration changes invalidate a cached round. Mutable
    attributes are snapshotted; plain callables compare by identity.
    Nested subfields (ComboField / MultiValueField) and the widget are
    fingerprinted recursively.
    """
    items = []
    for key, value in vars(field).items():
        if (
            key == "fields"
            and isinstance(value, (list, tuple))
            and value
            and all(isinstance(sub, Field) for sub in value)
        ):
            value = tuple(_field_fingerprint(sub) for sub in value)
        elif key == "widget":
            value = (
                type(value),
                _freeze_fingerprint_value(vars(value)),
            )
        else:
            value = _freeze_fingerprint_value(value)
        items.append((key, value))
    return tuple(sorted(items, key=lambda item: item[0]))


def _fields_fingerprint(fields):
    return tuple((name, _field_fingerprint(field)) for name, field in fields.items())


class DeclarativeFieldsMetaclass(MediaDefiningClass):
    """Collect Fields declared on the base classes."""

    def __new__(mcs, name, bases, attrs):
        # Collect fields from current class and remove them from attrs.
        attrs["declared_fields"] = {
            key: attrs.pop(key)
            for key, value in list(attrs.items())
            if isinstance(value, Field)
        }

        new_class = super().__new__(mcs, name, bases, attrs)

        # Walk through the MRO.
        declared_fields = {}
        for base in reversed(new_class.__mro__):
            # Collect fields from base class.
            if hasattr(base, "declared_fields"):
                declared_fields.update(base.declared_fields)

            # Field shadowing.
            for attr, value in base.__dict__.items():
                if value is None and attr in declared_fields:
                    declared_fields.pop(attr)

        new_class.base_fields = declared_fields
        new_class.declared_fields = declared_fields

        return new_class


class _AsyncValidationState:
    """Bookkeeping for a single asynchronous validation round.

    Several concurrent ``BaseForm.ais_valid()`` calls share one state (and
    one runner task) so that side-effectful validators run only once per
    round.
    """

    def __init__(self, *, epoch, is_bound, inputs, fields, fingerprint):
        self.runner_task = None
        # Number of ais_valid() callers currently awaiting the runner task.
        self.waiters = 0
        # Set once the round has reached a terminal state (completed,
        # abandoned by its waiters, or detached because all waiters left).
        self.finalized = False
        # Set when the round has been moved off the active slot because a
        # newer round superseded it. The runner keeps driving its own
        # snapshot for its remaining waiters but can never publish.
        self.detached = False
        # Validation epoch the round was started in. A synchronous
        # full_clean() while the round is in flight advances the epoch, so a
        # late async completion can never publish over the newer synchronous
        # result.
        self.epoch = epoch
        # Whether the form was bound when the round started.
        self.is_bound = is_bound
        # Detached snapshots the round cleans against. Inputs rebound or
        # mutated after the round started never reach the runner.
        self.data = inputs[0]
        self.files = inputs[1]
        self.initial = inputs[2]
        self.fields = fields
        # BoundField objects private to the runner task; lazily cached
        # values (initial, subwidgets) are computed against the snapshot.
        self.bound_fields_cache = {}
        # Value-based fingerprint of the snapshot, used to decide whether a
        # later call can reuse the completed result or must supersede the
        # in-flight round.
        self.fingerprint = fingerprint
        # Private staging of the round. The form's last successfully
        # published self._errors / self._cleaned_data / changed_data stay
        # untouched until this round completes and atomically swaps them in,
        # so external readers never observe a half-finished result.
        self.errors = None
        self.cleaned_data = _UNSET
        # Names of the fields whose snapshot values differ from the round's
        # initial data; computed once from the round snapshot. The empty
        # permitted short-circuit consults it and the result is published
        # together with errors and cleaned_data.
        self.changed_data = None
        # The business result of a completed round, shared with every waiter
        # so callers observe one identical conclusion even if the form is
        # re-validated or reset between the runner finishing and the waiters
        # resuming.
        self.result = None


class BaseForm(RenderableFormMixin):
    """
    The main implementation of all the Form logic. Note that this class is
    different than Form. See the comments by the Form class for more info. Any
    improvements to the form API should be made to this class, not to the Form
    class.
    """

    default_renderer = None
    field_order = None
    prefix = None
    use_required_attribute = True

    template_name_div = "django/forms/div.html"
    template_name_p = "django/forms/p.html"
    template_name_table = "django/forms/table.html"
    template_name_ul = "django/forms/ul.html"
    template_name_label = "django/forms/label.html"

    bound_field_class = None

    def __init__(
        self,
        data=None,
        files=None,
        auto_id="id_%s",
        prefix=None,
        initial=None,
        error_class=ErrorList,
        label_suffix=None,
        empty_permitted=False,
        field_order=None,
        use_required_attribute=None,
        renderer=None,
        bound_field_class=None,
    ):
        self.is_bound = data is not None or files is not None
        self._data = MultiValueDict() if data is None else data
        self._files = MultiValueDict() if files is None else files
        self.auto_id = auto_id
        if prefix is not None:
            self.prefix = prefix
        self._initial = initial or {}
        self.error_class = error_class
        # Translators: This is the default suffix added to form field labels
        self.label_suffix = label_suffix if label_suffix is not None else _(":")
        self.empty_permitted = empty_permitted
        self._errors = None  # Stores the errors after clean() has been called.
        # cleaned_data is only populated during a (a)full_clean(); before
        # that it raises AttributeError like a plain unset attribute.
        self._cleaned_data = _UNSET
        # State for the newest in-progress asynchronous validation round
        # (ais_valid()); None when no round is active. Every unfinished
        # round -- a superseded one keeps running for its remaining waiters
        # -- is tracked in _async_rounds so the runner task always reads and
        # writes its own private snapshot and staging.
        self._async_validation = None
        self._async_rounds = []
        # Fingerprint of the inputs/field definitions used by the last
        # successfully completed validation round, for reuse checks.
        self._validation_fingerprint = None
        # Bumped by every synchronous full_clean(). An async round whose
        # epoch no longer matches keeps serving its own waiters but can never
        # publish, so an async completion can never overwrite a newer
        # synchronous result.
        self._validation_epoch = 0

        # The base_fields class attribute is the *class-wide* definition of
        # fields. Because a particular *instance* of the class might want to
        # alter self.fields, we create self.fields here by copying base_fields.
        # Instances should always modify self.fields; they should not modify
        # self.base_fields.
        self._fields = copy.deepcopy(self.base_fields)
        self._bound_fields_cache = {}
        self.order_fields(self.field_order if field_order is None else field_order)

        if use_required_attribute is not None:
            self.use_required_attribute = use_required_attribute

        if self.empty_permitted and self.use_required_attribute:
            raise ValueError(
                "The empty_permitted and use_required_attribute arguments may "
                "not both be True."
            )

        # Initialize form renderer. Use a global default if not specified
        # either as an argument or as self.default_renderer.
        if renderer is None:
            if self.default_renderer is None:
                renderer = get_default_renderer()
            else:
                renderer = self.default_renderer
                if isinstance(self.default_renderer, type):
                    renderer = renderer()
        self.renderer = renderer

        self.bound_field_class = (
            bound_field_class
            or self.bound_field_class
            or getattr(self.renderer, "bound_field_class", None)
        )

    def order_fields(self, field_order):
        """
        Rearrange the fields according to field_order.

        field_order is a list of field names specifying the order. Append
        fields not included in the list in the default order for backward
        compatibility with subclasses not overriding field_order. If
        field_order is None, keep all fields in the order defined in the class.
        Ignore unknown fields in field_order to allow disabling fields in form
        subclasses without redefining ordering.
        """
        if field_order is None:
            return
        fields = {}
        for key in field_order:
            try:
                fields[key] = self.fields.pop(key)
            except KeyError:  # ignore unknown fields
                pass
        fields.update(self.fields)  # add remaining fields in original order
        self.fields = fields

    def __repr__(self):
        if self._errors is None:
            is_valid = "Unknown"
        else:
            is_valid = self.is_bound and not self._errors
        return "<%(cls)s bound=%(bound)s, valid=%(valid)s, fields=(%(fields)s)>" % {
            "cls": self.__class__.__name__,
            "bound": self.is_bound,
            "valid": is_valid,
            "fields": ";".join(self.fields),
        }

    def _bound_items(self):
        """Yield (name, bf) pairs, where bf is a BoundField object."""
        for name in self.fields:
            yield name, self[name]

    def __iter__(self):
        """Yield the form's fields as BoundField objects."""
        for name in self.fields:
            yield self[name]

    def _current_async_state(self):
        """Return the round the running task drives, if any.

        A superseded round keeps executing for its remaining waiters even
        though it is no longer the newest round, so look-up is by runner task
        rather than by the active slot.
        """
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            # No running event loop (synchronous code path).
            return None
        if current_task is None:
            return None
        for state in self._async_rounds:
            if current_task is state.runner_task:
                return state
        return None

    def __getitem__(self, name):
        """Return a BoundField with the given name."""
        state = self._current_async_state()
        if state is not None:
            # The runner uses its own BoundField objects so lazily cached
            # values computed against the round snapshot never leak onto the
            # BoundFields shared with the outside world.
            cache = state.bound_fields_cache
            fields = state.fields
        else:
            cache = self._bound_fields_cache
            fields = self._fields
        try:
            field = fields[name]
        except KeyError:
            raise KeyError(
                "Key '%s' not found in '%s'. Choices are: %s."
                % (
                    name,
                    self.__class__.__name__,
                    ", ".join(sorted(fields)),
                )
            )
        if name not in cache:
            cache[name] = field.get_bound_field(self, name)
        return cache[name]

    @property
    def data(self):
        # The task running an asynchronous round reads from its detached
        # snapshot; everyone else reads the live bound data.
        state = self._current_async_state()
        return state.data if state is not None else self._data

    @data.setter
    def data(self, value):
        self._data = value

    @property
    def files(self):
        state = self._current_async_state()
        return state.files if state is not None else self._files

    @files.setter
    def files(self, value):
        self._files = value

    @property
    def initial(self):
        state = self._current_async_state()
        return state.initial if state is not None else self._initial

    @initial.setter
    def initial(self, value):
        self._initial = value

    @property
    def fields(self):
        state = self._current_async_state()
        return state.fields if state is not None else self._fields

    @fields.setter
    def fields(self, value):
        self._fields = value

    @property
    def errors(self):
        """Return an ErrorDict for the data provided for the form."""
        # The task running an asynchronous round reads and writes that
        # round's private staging collection.
        state = self._current_async_state()
        if state is not None:
            return state.errors
        # While any round is in flight, every other caller keeps seeing the
        # last successfully completed result. On a form that has never
        # completed validation, hand back a detached empty ErrorDict rather
        # than a runner's half-finished collection.
        if self._async_rounds:
            if self._errors is not None:
                return self._errors
            return ErrorDict(renderer=self.renderer)
        if self._errors is None:
            self.full_clean()
        return self._errors

    @property
    def cleaned_data(self):
        state = self._current_async_state()
        if state is not None:
            cleaned = state.cleaned_data
        elif self._async_rounds:
            # Never expose partially populated data to code outside the
            # running round; keep serving the last completed result while a
            # new one is being produced.
            cleaned = self._cleaned_data
            if cleaned is _UNSET:
                return {}
        else:
            cleaned = self._cleaned_data
        if cleaned is _UNSET:
            raise AttributeError(
                "'%s' object has no attribute 'cleaned_data'"
                % self.__class__.__name__
            )
        return cleaned

    @cleaned_data.setter
    def cleaned_data(self, value):
        state = self._current_async_state()
        if state is not None:
            state.cleaned_data = value
        else:
            self._cleaned_data = value

    def is_valid(self):
        """Return True if the form has no errors, or False otherwise."""
        return self.is_bound and not self.errors

    async def ais_valid(self):
        """
        Async counterpart of is_valid().

        Run the field and form cleaning pipeline asynchronously, awaiting
        any awaitable results returned by field validators, ``clean_<field>``
        hooks or the form-wide ``clean()`` hook. The business result, error
        attribution and error format match the synchronous path.

        Concurrent calls on the same unchanged snapshot share a single
        cleaning round: side-effectful validators run only once, and callers
        joining later cannot change the inputs or outcome. If the bound data,
        initial data or field definitions change while a round is running,
        the next call starts a fresh round solely against the new snapshot;
        the superseded round keeps serving its own waiters from its snapshot
        but can never publish over the newer result. If every waiting caller
        is cancelled, the unfinished round is discarded (including its
        temporary errors) and the next call performs a full retry.
        """
        state = self._async_validation
        if state is None:
            if self._can_reuse_validation():
                return self.is_bound and not self._errors
            state = self._start_async_validation()
        elif self._current_validation_fingerprint() != state.fingerprint:
            # Inputs, initial data or field definitions changed while the
            # round was in flight: supersede it and clean solely against the
            # current snapshot. The previous round is detached rather than
            # cancelled so its remaining waiters still get its conclusion.
            state.detached = True
            state = self._start_async_validation()
        state.waiters += 1
        try:
            await asyncio.shield(state.runner_task)
        except asyncio.CancelledError:
            if state.waiters == 1 and not state.finalized:
                # The last waiter has given up: discard the unfinished round
                # synchronously so that a follow-up call starts a fresh one
                # even if the runner task has not processed its cancellation
                # yet.
                self._discard_async_validation(state)
                state.runner_task.cancel()
            raise
        finally:
            state.waiters -= 1
        # Return the round's own conclusion rather than re-reading the live
        # form state: the form may have been re-validated or reset between
        # the runner finishing and this waiter resuming.
        return state.result

    def _validation_inputs(self):
        """The live (data, files, initial) a new round would clean."""
        return (self._data, self._files, self._initial)

    def _snapshot_validation_inputs(self):
        """Detached shallow copies of the live bound inputs."""
        data, files, initial = self._validation_inputs()
        return (
            _snapshot_mapping(data),
            _snapshot_mapping(files),
            dict(initial),
        )

    def _clone_field(self, field):
        """Detach a field definition without detaching its validators.

        The field object is shallow-copied and its mutable definition
        containers (validators, choices, error messages, widget, nested
        subfields) are copied, so reconfiguring the live field while a round
        is in progress cannot reach the runner. Validator callables -- which
        may be bound methods of the live form -- keep their identity.
        """
        clone = copy.copy(field)
        for attr, value in list(vars(clone).items()):
            if attr == "widget":
                clone.widget = copy.deepcopy(value)
            elif (
                attr == "fields"
                and isinstance(value, (list, tuple))
                and value
                and all(isinstance(sub, Field) for sub in value)
            ):
                cloned = [self._clone_field(sub) for sub in value]
                clone.fields = tuple(cloned) if isinstance(value, tuple) else cloned
            elif isinstance(value, list):
                setattr(clone, attr, list(value))
            elif isinstance(value, dict):
                setattr(clone, attr, dict(value))
            elif isinstance(value, set):
                setattr(clone, attr, set(value))
        return clone

    def _snapshot_fields(self):
        """A detached copy of the field definitions for an async round.

        Fields added, removed, replaced or reconfigured after the round
        starts -- validators appended to a field, choices mutated in place or
        a toggled ``required`` flag -- cannot reach the runner.
        """
        return {name: self._clone_field(field) for name, field in self._fields.items()}

    def _freeze_fingerprint_value(self, value):
        return _freeze_fingerprint_value(value)

    def _field_fingerprint(self, field):
        return _field_fingerprint(field)

    def _fields_fingerprint(self, fields):
        return _fields_fingerprint(fields)

    def _current_validation_fingerprint(self):
        """Fingerprint of the live inputs and the live field definitions."""
        data, files, initial = self._validation_inputs()
        return (
            _mapping_fingerprint(data),
            _mapping_fingerprint(files),
            tuple(sorted(initial.items(), key=lambda item: str(item[0]))),
            self._fields_fingerprint(self._fields),
        )

    def _can_reuse_validation(self):
        """Reuse a completed round when its fingerprint still matches."""
        if self._errors is None or self._validation_fingerprint is None:
            return False
        return self._current_validation_fingerprint() == self._validation_fingerprint

    def _start_async_validation(self):
        loop = asyncio.get_running_loop()
        inputs = self._snapshot_validation_inputs()
        fields = self._snapshot_fields()
        state = _AsyncValidationState(
            is_bound=self.is_bound,
            inputs=inputs,
            fields=fields,
            fingerprint=self._build_snapshot_fingerprint(inputs, fields),
            epoch=self._validation_epoch,
        )
        # The round keeps private staging collections (installed as its first
        # step). The form's last successfully completed self._errors /
        # self._cleaned_data stay in place until the new round atomically
        # publishes, so the previous result remains observable in the
        # meantime.
        self._async_rounds.append(state)
        self._async_validation = state
        state.runner_task = loop.create_task(self._arun_async_validation(state))
        return state

    def _build_snapshot_fingerprint(self, inputs, fields):
        """Fingerprint matching _current_validation_fingerprint() for a
        detached round snapshot."""
        data, files, initial = inputs
        return (
            _mapping_fingerprint(data),
            _mapping_fingerprint(files),
            tuple(sorted(initial.items(), key=lambda item: str(item[0]))),
            self._fields_fingerprint(fields),
        )

    def _retire_async_validation(self, state):
        """Remove a terminal round from the active rounds list."""
        try:
            self._async_rounds.remove(state)
        except ValueError:
            pass

    async def _arun_async_validation(self, state):
        # The round may already have been discarded synchronously (its last
        # waiter cancelled) before this task got its first step.
        if state.finalized:
            self._retire_async_validation(state)
            return
        completed = False
        try:
            await self._afull_clean(state)
            completed = True
        finally:
            if state.finalized:
                # Already discarded by its last waiter.
                self._retire_async_validation(state)
            elif completed:
                self._finish_async_validation(state)
            else:
                # Cancelled (or otherwise interrupted) without a waiter
                # discarding the round first: drop it without publishing.
                self._discard_async_validation(state)

    def _finish_async_validation(self, state):
        state.finalized = True
        state.result = state.is_bound and not state.errors
        if (
            not state.detached
            and self._async_validation is state
            and self._validation_epoch == state.epoch
        ):
            # Atomically publish this round's staging as the form result.
            self._errors = state.errors
            self._cleaned_data = state.cleaned_data
            self.changed_data = state.changed_data
            self._validation_fingerprint = state.fingerprint
            self._async_validation = None
        # A detached (superseded) round keeps its conclusion only to serve
        # its own waiters via state.result; it never writes the form state.
        # Likewise a round whose epoch was superseded by a synchronous
        # full_clean() keeps its conclusion for its waiters but cannot
        # overwrite the newer synchronous result.
        self._retire_async_validation(state)

    def _discard_async_validation(self, state):
        state.finalized = True
        state.result = False
        # A detached runner that somehow reaches here must never publish.
        state.detached = True
        self._retire_async_validation(state)
        if self._async_validation is state:
            # The round's temporary collections were private, so the last
            # published result needs no restoration; clear the cached
            # fingerprint so the next call performs a complete retry.
            self._async_validation = None
            self._validation_fingerprint = None

    def add_prefix(self, field_name):
        """
        Return the field name with a prefix appended, if this Form has a
        prefix set.

        Subclasses may wish to override.
        """
        return "%s-%s" % (self.prefix, field_name) if self.prefix else field_name

    def add_initial_prefix(self, field_name):
        """Add an 'initial' prefix for checking dynamic initial values."""
        return "initial-%s" % self.add_prefix(field_name)

    def _widget_data_value(self, widget, html_name):
        # value_from_datadict() gets the data from the data dictionaries.
        # Each widget type knows how to retrieve its own data, because some
        # widgets split data over several HTML fields.
        return widget.value_from_datadict(self.data, self.files, html_name)

    @property
    def template_name(self):
        return self.renderer.form_template_name

    def get_context(self):
        fields = []
        hidden_fields = []
        top_errors = self.non_field_errors().copy()
        for name, bf in self._bound_items():
            if bf.is_hidden:
                if bf.errors:
                    top_errors += [
                        _("(Hidden field %(name)s) %(error)s")
                        % {"name": name, "error": str(e)}
                        for e in bf.errors
                    ]
                hidden_fields.append(bf)
            else:
                fields.append((bf, bf.errors))
        return {
            "form": self,
            "fields": fields,
            "hidden_fields": hidden_fields,
            "errors": top_errors,
        }

    def non_field_errors(self):
        """
        Return an ErrorList of errors that aren't associated with a particular
        field -- i.e., from Form.clean(). Return an empty ErrorList if there
        are none.
        """
        return self.errors.get(
            NON_FIELD_ERRORS,
            self.error_class(error_class="nonfield", renderer=self.renderer),
        )

    def add_error(self, field, error):
        """
        Update the content of `self._errors`.

        The `field` argument is the name of the field to which the errors
        should be added. If it's None, treat the errors as NON_FIELD_ERRORS.

        The `error` argument can be a single error, a list of errors, or a
        dictionary that maps field names to lists of errors. An "error" can be
        either a simple string or an instance of ValidationError with its
        message attribute set and a "list or dictionary" can be an actual
        `list` or `dict` or an instance of ValidationError with its
        `error_list` or `error_dict` attribute set.

        If `error` is a dictionary, the `field` argument *must* be None and
        errors will be added to the fields that correspond to the keys of the
        dictionary.
        """
        if not isinstance(error, ValidationError):
            # Normalize to ValidationError and let its constructor
            # do the hard work of making sense of the input.
            error = ValidationError(error)

        if hasattr(error, "error_dict"):
            if field is not None:
                raise TypeError(
                    "The argument `field` must be `None` when the `error` "
                    "argument is a dictionary."
                )
            else:
                error = error.error_dict
        else:
            error = {field or NON_FIELD_ERRORS: error.error_list}

        for field, error_list in error.items():
            if field not in self.errors:
                if field != NON_FIELD_ERRORS and field not in self.fields:
                    raise ValueError(
                        "'%s' has no field named '%s'."
                        % (self.__class__.__name__, field)
                    )
                if field == NON_FIELD_ERRORS:
                    self.errors[field] = self.error_class(
                        error_class="nonfield", renderer=self.renderer
                    )
                else:
                    self.errors[field] = self.error_class(
                        renderer=self.renderer,
                        field_id=self[field].auto_id,
                    )
            self.errors[field].extend(error_list)
            if field in self.cleaned_data:
                del self.cleaned_data[field]

    def has_error(self, field, code=None):
        return field in self.errors and (
            code is None
            or any(error.code == code for error in self.errors.as_data()[field])
        )

    def full_clean(self):
        """
        Clean all of self.data and populate self._errors and self.cleaned_data.
        """
        # A synchronous validation is the newest validation generation.
        # Detach in-flight async rounds so a late async completion -- success,
        # failure or cancellation -- can never overwrite or delete the
        # synchronous result about to be produced. The rounds keep running
        # for their own waiters, which still receive the round's conclusion.
        self._validation_epoch += 1
        for state in self._async_rounds:
            state.detached = True
        self._async_validation = None
        # The bound inputs are about to define a new result, so a changed_data
        # list cached for earlier inputs must not survive the pass; the checks
        # below repopulate it from the current data and initial.
        del self.changed_data
        self._errors = ErrorDict(renderer=self.renderer)
        if not self.is_bound:  # Stop further processing.
            self._record_validation_fingerprint()
            return
        self.cleaned_data = {}
        # If the form is permitted to be empty, and none of the form data has
        # changed from the initial data, short circuit any validation.
        if self.empty_permitted and not self.has_changed():
            self._record_validation_fingerprint()
            return

        self._clean_fields()
        self._clean_form()
        self._post_clean()
        self._record_validation_fingerprint()

    def _record_validation_fingerprint(self):
        # The synchronous path never consults the fingerprint itself (it
        # always re-runs), but it lets a later ais_valid() reuse the result.
        self._validation_fingerprint = self._current_validation_fingerprint()

    def _clean_fields(self):
        for name, bf in self._bound_items():
            field = bf.field
            try:
                self.cleaned_data[name] = field._clean_bound_field(bf)
                if hasattr(self, "clean_%s" % name):
                    value = getattr(self, "clean_%s" % name)()
                    self.cleaned_data[name] = value
            except ValidationError as e:
                self.add_error(name, e)

    def _async_raise_if_discarded(self, state):
        """Stop a runner whose unfinished round was discarded.

        A runner can keep running after its task was cancelled if user code
        (a validator or a clean hook) swallows CancelledError. Once the last
        waiter has discarded the round it must not keep producing results, so
        cancellation is re-raised at the next pipeline boundary. A merely
        superseded (detached) round is not discarded: it keeps running to
        completion for its remaining waiters, but its result is never
        published.
        """
        if state.finalized:
            raise asyncio.CancelledError()

    async def _aclean_fields(self, state):
        for name, bf in self._bound_items():
            self._async_raise_if_discarded(state)
            field = bf.field
            try:
                value = await field._aclean_bound_field(bf)
                # The value is written to cleaned_data only after any
                # awaitable field cleaning has resolved, so partial results
                # never persist.
                self._async_raise_if_discarded(state)
                self.cleaned_data[name] = value
                if hasattr(self, "clean_%s" % name):
                    hook = getattr(self, "clean_%s" % name)
                    value = hook()
                    if inspect.isawaitable(value):
                        value = await value
                        self._async_raise_if_discarded(state)
                    self.cleaned_data[name] = value
            except ValidationError as e:
                await self._aadd_error(name, e, state)

    def _clean_form(self):
        try:
            cleaned_data = self.clean()
        except ValidationError as e:
            self.add_error(None, e)
        else:
            if cleaned_data is not None:
                self.cleaned_data = cleaned_data

    async def _aclean_form(self, state):
        self._async_raise_if_discarded(state)
        try:
            cleaned_data = self.clean()
            if inspect.isawaitable(cleaned_data):
                cleaned_data = await cleaned_data
                self._async_raise_if_discarded(state)
        except ValidationError as e:
            await self._aadd_error(None, e, state)
        else:
            if cleaned_data is not None:
                self.cleaned_data = cleaned_data

    async def _apost_clean(self, state):
        """
        Async counterpart of _post_clean(). The default hook is a no-op, as
        on the synchronous path, and returns an awaitable result if a
        subclass's _post_clean() happens to produce one.
        """
        result = self._post_clean()
        if inspect.isawaitable(result):
            await result
            self._async_raise_if_discarded(state)

    async def _afull_clean(self, state):
        """
        Async counterpart of full_clean(). Field cleaning runs in
        declaration order and the form-wide clean hook only reads field
        results that have already completed.
        """
        # Fresh private staging for this round; the previously published
        # collections stay untouched until the round finishes.
        state.errors = ErrorDict(renderer=self.renderer)
        # Determine the changed fields from the round snapshot before any
        # early return so every published result carries a concrete list.
        state.changed_data = self.changed_data
        if not state.is_bound:  # Stop further processing.
            return
        self.cleaned_data = {}
        # If the form is permitted to be empty, and none of the form data has
        # changed from the initial data, short circuit any validation.
        if self.empty_permitted and not self.has_changed():
            return

        await self._aclean_fields(state)
        await self._aclean_form(state)
        await self._apost_clean(state)

    async def _aadd_error(self, field, error, state):
        self._async_raise_if_discarded(state)
        result = self.add_error(field, error)
        if inspect.isawaitable(result):
            await result
            self._async_raise_if_discarded(state)

    def _post_clean(self):
        """
        An internal hook for performing additional cleaning after form cleaning
        is complete. Used for model validation in model forms.
        """
        pass

    def clean(self):
        """
        Hook for doing any extra form-wide cleaning after Field.clean() has
        been called on every field. Any ValidationError raised by this method
        will not be associated with a particular field; it will have a
        special-case association with the field named '__all__'.
        """
        return self.cleaned_data

    def has_changed(self):
        """Return True if data differs from initial."""
        return bool(self.changed_data)

    @property
    def changed_data(self):
        state = self._current_async_state()
        if state is not None:
            # Determine the changed fields once from the round snapshot and
            # keep the result with the round so the empty permitted short
            # circuit decision and the published changed_data agree. The
            # shared cache is never populated, so it stays tied to the live
            # inputs.
            if state.changed_data is None:
                state.changed_data = [
                    name for name, bf in self._bound_items() if bf._has_changed()
                ]
            return state.changed_data
        try:
            return self.__dict__["changed_data"]
        except KeyError:
            value = [name for name, bf in self._bound_items() if bf._has_changed()]
            self.__dict__["changed_data"] = value
            return value

    @changed_data.setter
    def changed_data(self, value):
        # Preserve cached_property's writable-instance-dict semantics.
        self.__dict__["changed_data"] = value

    @changed_data.deleter
    def changed_data(self):
        self.__dict__.pop("changed_data", None)

    @property
    def media(self):
        """Return all media required to render the widgets on this form."""
        media = Media()
        for field in self.fields.values():
            media += field.widget.media
        return media

    def is_multipart(self):
        """
        Return True if the form needs to be multipart-encoded, i.e. it has
        FileInput, or False otherwise.
        """
        return any(field.widget.needs_multipart_form for field in self.fields.values())

    def hidden_fields(self):
        """
        Return a list of all the BoundField objects that are hidden fields.
        Useful for manual form layout in templates.
        """
        return [field for field in self if field.is_hidden]

    def visible_fields(self):
        """
        Return a list of BoundField objects that aren't hidden fields.
        The opposite of the hidden_fields() method.
        """
        return [field for field in self if not field.is_hidden]

    def get_initial_for_field(self, field, field_name):
        """
        Return initial data for field on form. Use initial data from the form
        or the field, in that order. Evaluate callable values.
        """
        value = self.initial.get(field_name, field.initial)
        if callable(value):
            value = value()
        # If this is an auto-generated default date, nix the microseconds
        # for standardized handling. See #22502.
        if (
            isinstance(value, (datetime.datetime, datetime.time))
            and not field.widget.supports_microseconds
        ):
            value = value.replace(microsecond=0)
        return value


class Form(BaseForm, metaclass=DeclarativeFieldsMetaclass):
    "A collection of Fields, plus their associated data."

    # This is a separate class from BaseForm in order to abstract the way
    # self.fields is specified. This class (Form) is the one that does the
    # fancy metaclass stuff purely for the semantic sugar -- it allows one
    # to define a form using declarative syntax.
    # BaseForm itself has no way of designating self.fields.
