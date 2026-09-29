import asyncio
import inspect

from django.core.exceptions import ValidationError
from django.forms.fields import BooleanField, Field, IntegerField
from django.forms.forms import (
    Form,
    _mapping_fingerprint,
    _snapshot_mapping,
    _UNSET,
)
from django.forms.renderers import get_default_renderer
from django.forms.utils import ErrorList, RenderableFormMixin
from django.forms.widgets import CheckboxInput, HiddenInput, NumberInput
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext_lazy

__all__ = ("BaseFormSet", "formset_factory", "all_valid")

# special field names
TOTAL_FORM_COUNT = "TOTAL_FORMS"
INITIAL_FORM_COUNT = "INITIAL_FORMS"
MIN_NUM_FORM_COUNT = "MIN_NUM_FORMS"
MAX_NUM_FORM_COUNT = "MAX_NUM_FORMS"
ORDERING_FIELD_NAME = "ORDER"
DELETION_FIELD_NAME = "DELETE"

# default minimum number of forms in a formset
DEFAULT_MIN_NUM = 0

# default maximum number of forms in a formset, to prevent memory exhaustion
DEFAULT_MAX_NUM = 1000


class ManagementForm(Form):
    """
    Keep track of how many form instances are displayed on the page. If adding
    new forms via JavaScript, you should increment the count field of this form
    as well.
    """

    TOTAL_FORMS = IntegerField(widget=HiddenInput)
    INITIAL_FORMS = IntegerField(widget=HiddenInput)
    # MIN_NUM_FORM_COUNT and MAX_NUM_FORM_COUNT are output with the rest of the
    # management form, but only for the convenience of client-side code. The
    # POST value of them returned from the client is not checked.
    MIN_NUM_FORMS = IntegerField(required=False, widget=HiddenInput)
    MAX_NUM_FORMS = IntegerField(required=False, widget=HiddenInput)

    def clean(self):
        cleaned_data = super().clean()
        # When the management form is invalid, we don't know how many forms
        # were submitted.
        cleaned_data.setdefault(TOTAL_FORM_COUNT, 0)
        cleaned_data.setdefault(INITIAL_FORM_COUNT, 0)
        return cleaned_data


class _AsyncFormSetValidationState:
    """Bookkeeping for a single asynchronous formset validation round.

    Several concurrent ``BaseFormSet.ais_valid()`` calls on an unchanged
    snapshot share one state (and one runner task), so each sub-form
    validator and the formset-wide ``clean()`` hook run only once per round.
    """

    def __init__(self, *, epoch, is_bound, inputs, fingerprint):
        self.runner_task = None
        # Number of ais_valid() callers currently awaiting the runner task.
        self.waiters = 0
        # Set once the round reaches a terminal state (completed, abandoned
        # by all waiters, or discarded).
        self.finalized = False
        # Set when a newer round or a synchronous full_clean() superseded the
        # round. The runner keeps driving its own snapshot for its remaining
        # waiters but can never publish.
        self.detached = False
        # Validation epoch the round was started in. A synchronous
        # full_clean() while the round is in flight advances the epoch, so a
        # late async completion can never publish over the newer synchronous
        # result.
        self.epoch = epoch
        # Whether the formset was bound when the round started.
        self.is_bound = is_bound
        # Detached snapshots the round cleans against.
        self.data, self.files, self.initial = inputs
        # Value-based fingerprint of the snapshot, used to decide whether a
        # later call can reuse the completed result or must supersede the
        # in-flight round.
        self.fingerprint = fingerprint
        # The form instances the runner drives, built once from the
        # snapshot. They stay private to the round; the formset's live
        # ``forms`` collection is untouched until the round publishes.
        self.forms = None
        # A snapshot management form, only for bound rounds.
        self.management_form = None
        self.total_form_count = 0
        self.initial_form_count = 0
        # Indexes of empty extra forms and forms marked for deletion,
        # collected while the child forms are cleaned.
        self.empty_forms_count = 0
        self.deleted_indexes = []
        # Private staging of the round: one ErrorDict per child form and the
        # formset-wide non-form errors. The last published collections stay
        # untouched until the round atomically swaps them in.
        self.errors = None
        self.non_form_errors = None
        # The business result of a completed round, shared with every waiter.
        self.result = None


class BaseFormSet(RenderableFormMixin):
    """
    A collection of instances of the same Form class.
    """

    deletion_widget = CheckboxInput
    ordering_widget = NumberInput
    default_error_messages = {
        "missing_management_form": _(
            "ManagementForm data is missing or has been tampered with. Missing fields: "
            "%(field_names)s. You may need to file a bug report if the issue persists."
        ),
        "too_many_forms": ngettext_lazy(
            "Please submit at most %(num)d form.",
            "Please submit at most %(num)d forms.",
            "num",
        ),
        "too_few_forms": ngettext_lazy(
            "Please submit at least %(num)d form.",
            "Please submit at least %(num)d forms.",
            "num",
        ),
    }

    template_name_div = "django/forms/formsets/div.html"
    template_name_p = "django/forms/formsets/p.html"
    template_name_table = "django/forms/formsets/table.html"
    template_name_ul = "django/forms/formsets/ul.html"

    def __init__(
        self,
        data=None,
        files=None,
        auto_id="id_%s",
        prefix=None,
        initial=None,
        error_class=ErrorList,
        form_kwargs=None,
        error_messages=None,
    ):
        self._is_bound = data is not None or files is not None
        self.prefix = prefix or self.get_default_prefix()
        self.auto_id = auto_id
        self._data = data or {}
        self._files = files or {}
        self._initial = initial
        self.form_kwargs = form_kwargs or {}
        self.error_class = error_class
        self._errors = None
        self._non_form_errors = None
        # Lazily built, live form instances and management form.
        self._forms_cache = _UNSET
        self._management_form_cache = None
        # State for the newest in-progress asynchronous validation round
        # (ais_valid()); None when no round is active. Every unfinished
        # round -- a superseded one keeps running for its remaining waiters
        # -- is tracked in _async_rounds so the runner task always reads and
        # writes its own private snapshot and staging.
        self._async_validation = None
        self._async_rounds = []
        # Fingerprint of the inputs/configuration used by the last
        # successfully completed validation round, for reuse checks.
        self._validation_fingerprint = None
        # Bumped by every synchronous full_clean(). An async round whose
        # epoch no longer matches keeps serving its own waiters but can never
        # publish, so an async completion can never overwrite a newer
        # synchronous result.
        self._validation_epoch = 0
        self.form_renderer = self.renderer
        self.renderer = self.renderer or get_default_renderer()

        messages = {}
        for cls in reversed(type(self).__mro__):
            messages.update(getattr(cls, "default_error_messages", {}))
        if error_messages is not None:
            messages.update(error_messages)
        self.error_messages = messages

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

    @property
    def is_bound(self):
        state = self._current_async_state()
        return state.is_bound if state is not None else self._is_bound

    @is_bound.setter
    def is_bound(self, value):
        self._is_bound = value

    @property
    def data(self):
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

    def __iter__(self):
        """Yield the forms in the order they should be rendered."""
        return iter(self.forms)

    def __getitem__(self, index):
        """Return the form at the given index, based on the rendering order."""
        return self.forms[index]

    def __len__(self):
        return len(self.forms)

    def __bool__(self):
        """
        Return True since all formsets have a management form which is not
        included in the length.
        """
        return True

    def __repr__(self):
        if self._errors is None:
            is_valid = "Unknown"
        else:
            is_valid = (
                self.is_bound
                and not self._non_form_errors
                and not any(form_errors for form_errors in self._errors)
            )
        return "<%s: bound=%s valid=%s total_forms=%s>" % (
            self.__class__.__qualname__,
            self.is_bound,
            is_valid,
            self.total_form_count(),
        )

    @property
    def management_form(self):
        """Return the ManagementForm instance for this FormSet."""
        state = self._current_async_state()
        if state is not None:
            # An async round builds and fully cleans a detached management
            # form once, before any child form is touched.
            if state.management_form is None:
                state.management_form = self._build_management_form()
            return state.management_form
        if self._async_rounds:
            # Do not cache a management form built against the live inputs
            # while a round is in flight: the runner owns a private snapshot
            # instance and publishes it on completion.
            if self._management_form_cache is not None:
                return self._management_form_cache
            return self._build_management_form()
        if self._management_form_cache is None:
            self._management_form_cache = self._build_management_form()
        return self._management_form_cache

    def _build_management_form(self):
        if self.is_bound:
            form = ManagementForm(
                self.data,
                auto_id=self.auto_id,
                prefix=self.prefix,
                renderer=self.renderer,
            )
            form.full_clean()
        else:
            form = ManagementForm(
                auto_id=self.auto_id,
                prefix=self.prefix,
                initial={
                    TOTAL_FORM_COUNT: self.total_form_count(),
                    INITIAL_FORM_COUNT: self.initial_form_count(),
                    MIN_NUM_FORM_COUNT: self.min_num,
                    MAX_NUM_FORM_COUNT: self.max_num,
                },
                renderer=self.renderer,
            )
        return form

    def total_form_count(self):
        """Return the total number of forms in this FormSet."""
        if self.is_bound:
            # return absolute_max if it is lower than the actual total form
            # count in the data; this is DoS protection to prevent clients
            # from forcing the server to instantiate arbitrary numbers of
            # forms
            return min(
                self.management_form.cleaned_data[TOTAL_FORM_COUNT], self.absolute_max
            )
        else:
            initial_forms = self.initial_form_count()
            total_forms = max(initial_forms, self.min_num) + self.extra
            # Allow all existing related objects/inlines to be displayed,
            # but don't allow extra beyond max_num.
            if initial_forms > self.max_num >= 0:
                total_forms = initial_forms
            elif total_forms > self.max_num >= 0:
                total_forms = self.max_num
        return total_forms

    def initial_form_count(self):
        """Return the number of forms that are required in this FormSet."""
        if self.is_bound:
            return self.management_form.cleaned_data[INITIAL_FORM_COUNT]
        else:
            # Use the length of the initial data if it's there, 0 otherwise.
            initial_forms = len(self.initial) if self.initial else 0
        return initial_forms

    @property
    def forms(self):
        """Instantiate forms at first property access."""
        state = self._current_async_state()
        if state is not None:
            # The runner's child forms are private to the round; they are
            # built only after the management form has been validated.
            return state.forms
        # While a round is in flight, every other caller keeps observing the
        # last complete collection. Building fresh live forms here would run
        # their synchronous validation against the live inputs and leak a
        # half-finished state, so serve the published forms (or nothing when
        # no validation ever completed).
        if self._async_rounds:
            return [] if self._forms_cache is _UNSET else self._forms_cache
        if self._forms_cache is _UNSET:
            # DoS protection is included in total_form_count().
            self._forms_cache = self._build_forms()
        return self._forms_cache

    @forms.setter
    def forms(self, value):
        state = self._current_async_state()
        if state is not None:
            state.forms = value
        else:
            self._forms_cache = value

    def _build_forms(self):
        """Construct a fresh collection of child forms for this context."""
        return [
            self._construct_form(i, **self.get_form_kwargs(i))
            for i in range(self.total_form_count())
        ]

    def get_form_kwargs(self, index):
        """
        Return additional keyword arguments for each individual formset form.

        index will be None if the form being constructed is a new empty
        form.
        """
        return self.form_kwargs.copy()

    def _construct_form(self, i, **kwargs):
        """Instantiate and return the i-th form instance in a formset."""
        defaults = {
            "auto_id": self.auto_id,
            "prefix": self.add_prefix(i),
            "error_class": self.error_class,
            # Don't render the HTML 'required' attribute as it may cause
            # incorrect validation for extra, optional, and deleted
            # forms in the formset.
            "use_required_attribute": False,
            "renderer": self.form_renderer,
        }
        if self.is_bound:
            defaults["data"] = self.data
            defaults["files"] = self.files
        if self.initial and "initial" not in kwargs:
            try:
                defaults["initial"] = self.initial[i]
            except IndexError:
                pass
        # Allow extra forms to be empty, unless they're part of
        # the minimum forms.
        if i >= self.initial_form_count() and i >= self.min_num:
            defaults["empty_permitted"] = True
        defaults.update(kwargs)
        form = self.form(**defaults)
        self.add_fields(form, i)
        return form

    @property
    def initial_forms(self):
        """Return a list of all the initial forms in this formset."""
        return self.forms[: self.initial_form_count()]

    @property
    def extra_forms(self):
        """Return a list of all the extra forms in this formset."""
        return self.forms[self.initial_form_count() :]

    @property
    def empty_form(self):
        form_kwargs = {
            **self.get_form_kwargs(None),
            "auto_id": self.auto_id,
            "prefix": self.add_prefix("__prefix__"),
            "empty_permitted": True,
            "use_required_attribute": False,
            "renderer": self.form_renderer,
        }
        form = self.form(**form_kwargs)
        self.add_fields(form, None)
        return form

    @property
    def cleaned_data(self):
        """
        Return a list of form.cleaned_data dicts for every form in self.forms.
        """
        if not self.is_valid():
            raise AttributeError(
                "'%s' object has no attribute 'cleaned_data'" % self.__class__.__name__
            )
        return [form.cleaned_data for form in self.forms]

    @property
    def deleted_forms(self):
        """Return a list of forms that have been marked for deletion."""
        if not self.is_valid() or not self.can_delete:
            return []
        # construct _deleted_form_indexes which is just a list of form indexes
        # that have had their deletion widget set to True
        cache = self._current_async_state() or self
        if not hasattr(cache, "_deleted_form_indexes"):
            indexes = []
            for i, form in enumerate(self.forms):
                # If this is an extra form and hasn't changed, ignore it.
                if i >= self.initial_form_count() and not form.has_changed():
                    continue
                if self._should_delete_form(form):
                    indexes.append(i)
            cache._deleted_form_indexes = indexes
        return [self.forms[i] for i in cache._deleted_form_indexes]

    @property
    def ordered_forms(self):
        """
        Return a list of form in the order specified by the incoming data.
        Raise an AttributeError if ordering is not allowed.
        """
        if not self.is_valid() or not self.can_order:
            raise AttributeError(
                "'%s' object has no attribute 'ordered_forms'" % self.__class__.__name__
            )
        # Construct _ordering, which is a list of (form_index,
        # order_field_value) tuples. After constructing this list, we'll sort
        # it by order_field_value so we have a way to get to the form indexes
        # in the order specified by the form data.
        cache = self._current_async_state() or self
        if not hasattr(cache, "_ordering"):
            ordering = []
            for i, form in enumerate(self.forms):
                # If this is an extra form and hasn't changed, ignore it.
                if i >= self.initial_form_count() and not form.has_changed():
                    continue
                # don't add data marked for deletion to self.ordered_data
                if self.can_delete and self._should_delete_form(form):
                    continue
                ordering.append((i, form.cleaned_data[ORDERING_FIELD_NAME]))
            # After we're done populating ordering, sort it.
            # A sort function to order things numerically ascending, but
            # None should be sorted below anything else. Allowing None as
            # a comparison value makes it so we can leave ordering fields
            # blank.

            def compare_ordering_key(k):
                if k[1] is None:
                    return (1, 0)  # +infinity, larger than any number
                return (0, k[1])

            ordering.sort(key=compare_ordering_key)
            cache._ordering = ordering
        # Return a list of form.cleaned_data dicts in the order specified by
        # the form data.
        return [self.forms[i[0]] for i in cache._ordering]

    @classmethod
    def get_default_prefix(cls):
        return "form"

    @classmethod
    def get_deletion_widget(cls):
        return cls.deletion_widget

    @classmethod
    def get_ordering_widget(cls):
        return cls.ordering_widget

    def non_form_errors(self):
        """
        Return an ErrorList of errors that aren't associated with a particular
        form -- i.e., from formset.clean(). Return an empty ErrorList if there
        are none.
        """
        state = self._current_async_state()
        if state is not None:
            return state.non_form_errors
        # While a round is in flight, every other caller keeps seeing the
        # last successfully completed result. On a formset that has never
        # completed validation, hand back a detached empty ErrorList rather
        # than a runner's half-finished collection.
        if self._async_rounds:
            if self._non_form_errors is not None:
                return self._non_form_errors
            return self.error_class(
                error_class="nonform", renderer=self.renderer
            )
        if self._non_form_errors is None:
            self.full_clean()
        return self._non_form_errors

    @property
    def errors(self):
        """Return a list of form.errors for every form in self.forms."""
        state = self._current_async_state()
        if state is not None:
            return state.errors
        # While a round is in flight, keep serving the last complete result
        # (or a detached empty list when no validation ever completed) rather
        # than the runner's half-finished collection.
        if self._async_rounds:
            if self._errors is not None:
                return self._errors
            return []
        if self._errors is None:
            self.full_clean()
        return self._errors

    def total_error_count(self):
        """Return the number of errors across all forms in the formset."""
        return len(self.non_form_errors()) + sum(
            len(form_errors) for form_errors in self.errors
        )

    def _should_delete_form(self, form):
        """Return whether or not the form was marked for deletion."""
        return form.cleaned_data.get(DELETION_FIELD_NAME, False)

    def is_valid(self):
        """Return True if every form in self.forms is valid."""
        if not self.is_bound:
            return False
        # Accessing errors triggers a full clean the first time only.
        self.errors
        # List comprehension ensures is_valid() is called for all forms.
        # Forms due to be deleted shouldn't cause the formset to be invalid.
        forms_valid = all(
            [
                form.is_valid()
                for form in self.forms
                if not (self.can_delete and self._should_delete_form(form))
            ]
        )
        return forms_valid and not self.non_form_errors()

    async def ais_valid(self):
        """
        Async counterpart of is_valid().

        Run the management form, child form and formset-wide cleaning
        pipeline asynchronously, awaiting any awaitable results returned by
        child validators, ``clean_<field>`` hooks or the formset-wide
        ``clean()`` hook. The business result, error attribution, error
        text and cleaned_data match the synchronous path item for item.

        Concurrent calls on the same unchanged snapshot share a single
        cleaning round: every child form validator and the formset clean
        hook run only once, and callers joining later cannot change the
        inputs or outcome. If the bound data, initial data, form count or
        field definitions change while a round is running, the next call
        starts a fresh round solely against the new snapshot; the
        superseded round keeps serving its own waiters from its snapshot
        but can never publish over the newer result. If every waiting
        caller is cancelled, the unfinished round is discarded (including
        its temporary errors) and the next call performs a full retry.
        """
        state = self._async_validation
        if state is None:
            if self._can_reuse_validation():
                return self._is_bound and not self._published_is_invalid()
            state = self._start_async_validation()
        elif self._current_validation_fingerprint() != state.fingerprint:
            # Inputs, initial data, form count or field definitions changed
            # while the round was in flight: supersede it and clean solely
            # against the current snapshot. The previous round is detached
            # rather than cancelled so its remaining waiters still get its
            # conclusion.
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
        # formset state.
        return state.result

    def _published_is_invalid(self):
        return bool(self._non_form_errors) or any(self._errors)

    # -- Async round plumbing ------------------------------------------------

    def _validation_inputs(self):
        """The live (data, files, initial) a new round would clean."""
        return (self._data, self._files, self._initial)

    def _snapshot_validation_inputs(self):
        """Detached shallow copies of the live bound inputs."""
        data, files, initial = self._validation_inputs()
        return (
            _snapshot_mapping(data),
            _snapshot_mapping(files),
            None if initial is None else list(initial),
        )

    def _freeze_fingerprint_value(self, value):
        """Return an immutable snapshot of a fingerprint component."""
        if isinstance(value, (list, tuple)):
            return tuple(self._freeze_fingerprint_value(item) for item in value)
        if isinstance(value, set):
            return frozenset(self._freeze_fingerprint_value(item) for item in value)
        if isinstance(value, dict):
            return tuple(
                (key, self._freeze_fingerprint_value(item))
                for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            )
        return value

    def _field_fingerprint(self, field):
        """Structural fingerprint of a (possibly nested) field definition.

        Mirrors the form layer's fingerprinting so validators, choices,
        error messages, coercion callables, nested subfields and the widget
        are all covered; mutable attributes are snapshotted and plain
        callables compare by identity.
        """
        items = []
        for key, value in vars(field).items():
            if (
                key == "fields"
                and isinstance(value, (list, tuple))
                and value
                and all(isinstance(sub, Field) for sub in value)
            ):
                value = tuple(self._field_fingerprint(sub) for sub in value)
            elif key == "widget":
                value = (
                    type(value),
                    self._freeze_fingerprint_value(vars(value)),
                )
            else:
                value = self._freeze_fingerprint_value(value)
            items.append((key, value))
        return tuple(sorted(items, key=lambda item: item[0]))

    def _form_fields_fingerprint(self):
        # The form class's declared fields. Fields added by add_fields()
        # (ORDER/DELETE and, on model formsets, the primary key) are derived
        # deterministically from the other configuration components in this
        # fingerprint and the management snapshot.
        return tuple(
            (name, self._field_fingerprint(field))
            for name, field in self.form.base_fields.items()
        )

    def _formset_configuration_fingerprint(self):
        """Structural fingerprint of the formset's fixed configuration.

        Covers the form class and its field definitions, the number of forms
        it can take and the form_kwargs handed to every constructed form, so
        changes to any of them invalidate a cached round.
        """
        return (
            self.form,
            self._form_fields_fingerprint(),
            self.extra,
            self.can_order,
            self.can_delete,
            self.can_delete_extra,
            self.min_num,
            self.max_num,
            self.absolute_max,
            self.validate_min,
            self.validate_max,
            self.auto_id,
            self.prefix,
            self.error_class,
            self._freeze_fingerprint_value(self.error_messages),
            self._freeze_fingerprint_value(self.form_kwargs),
        )

    def _snapshot_initial_fingerprint(self, initial):
        if initial is None:
            return None
        return tuple(self._freeze_fingerprint_value(item) for item in initial)

    def _build_fingerprint(self, data, files, initial):
        return (
            _mapping_fingerprint(data),
            _mapping_fingerprint(files),
            self._snapshot_initial_fingerprint(initial),
            self._formset_configuration_fingerprint(),
        )

    def _current_validation_fingerprint(self):
        data, files, initial = self._validation_inputs()
        return self._build_fingerprint(data, files, initial)

    def _can_reuse_validation(self):
        """Reuse a completed round when its fingerprint still matches."""
        if self._errors is None or self._validation_fingerprint is None:
            return False
        return self._current_validation_fingerprint() == self._validation_fingerprint

    def _record_validation_fingerprint(self):
        # The synchronous path never consults the fingerprint itself (it
        # always re-runs), but it lets a later ais_valid() reuse the result.
        self._validation_fingerprint = self._current_validation_fingerprint()

    def _start_async_validation(self):
        loop = asyncio.get_running_loop()
        data, files, initial = self._snapshot_validation_inputs()
        state = _AsyncFormSetValidationState(
            epoch=self._validation_epoch,
            is_bound=self._is_bound,
            inputs=(data, files, initial),
            fingerprint=self._build_fingerprint(data, files, initial),
        )
        # The round keeps private staging and private child forms; the last
        # published self._errors / self._non_form_errors stay in place until
        # the new round atomically publishes.
        self._async_rounds.append(state)
        self._async_validation = state
        state.runner_task = loop.create_task(self._arun_async_validation(state))
        return state

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
        state.result = state.is_bound and not (
            state.non_form_errors or any(state.errors)
        )
        if (
            not state.detached
            and self._async_validation is state
            and self._validation_epoch == state.epoch
        ):
            # Atomically publish this round's staging as the formset result.
            self._errors = state.errors
            self._non_form_errors = state.non_form_errors
            self._forms_cache = state.forms
            self._management_form_cache = state.management_form
            # Deletion/ordering bookkeeping derived from the previous child
            # forms must not point at indexes into the new collection; they
            # are rebuilt lazily against the published forms.
            self.__dict__.pop("_deleted_form_indexes", None)
            self.__dict__.pop("_ordering", None)
            self._validation_fingerprint = state.fingerprint
            self._async_validation = None
        # A detached (superseded) round keeps its conclusion only to serve
        # its own waiters via state.result; it never writes the formset
        # state. Likewise a round whose epoch was superseded by a
        # synchronous full_clean() cannot overwrite the newer result.
        self._retire_async_validation(state)

    def _discard_async_validation(self, state):
        state.finalized = True
        state.result = False
        # A detached runner that somehow reaches here must never publish.
        state.detached = True
        self._retire_async_validation(state)
        if self._async_validation is state:
            # The round's temporary collections and child forms were
            # private, so the last published result needs no restoration;
            # clear the cached fingerprint so the next call performs a
            # complete retry.
            self._async_validation = None
            self._validation_fingerprint = None

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

    async def _afull_clean(self, state):
        """
        Async counterpart of full_clean(). Child forms are cleaned in
        declaration order and the formset-wide clean hook only reads the
        completed cleaned_data of valid child forms.
        """
        # Fresh private staging for this round; the previously published
        # collections stay untouched until the round finishes.
        state.errors = []
        state.non_form_errors = self.error_class(
            error_class="nonform", renderer=self.renderer
        )

        if not state.is_bound:  # Stop further processing.
            return

        # The management form is cleaned first, synchronously. The number of
        # child forms still comes from its cleaned_data defaults when it is
        # invalid (TOTAL_FORMS defaults to 0), so this mirrors the
        # synchronous path exactly.
        management_form = self.management_form
        if not management_form.is_valid():
            error = ValidationError(
                self.error_messages["missing_management_form"],
                params={
                    "field_names": ", ".join(
                        management_form.add_prefix(field_name)
                        for field_name in management_form.errors
                    ),
                },
                code="missing_management_form",
            )
            state.non_form_errors.append(error)

        state.total_form_count = self.total_form_count()
        state.initial_form_count = self.initial_form_count()
        state.forms = self._build_forms()
        empty_forms_count = 0
        for i, form in enumerate(state.forms):
            self._async_raise_if_discarded(state)
            # Empty forms are unchanged forms beyond those with initial data.
            if not form.has_changed() and i >= state.initial_form_count:
                empty_forms_count += 1
            # Awaiting the child form runs its full async cleaning pipeline
            # exactly once; _should_delete_form() requires cleaned_data.
            await form.ais_valid()
            self._async_raise_if_discarded(state)
            if self.can_delete and self._should_delete_form(form):
                state.deleted_indexes.append(i)
                continue
            state.errors.append(form.errors)
        state.empty_forms_count = empty_forms_count
        # As on the synchronous path, an invalid management form skips the
        # limit checks and the formset-wide clean hook (the missing
        # INITIAL_FORMS cleaned value prevents the counts from being known).
        if management_form.is_valid():
            await self._acheck_formset_limits(state)

    async def _acheck_formset_limits(self, state):
        self._async_raise_if_discarded(state)
        management_form = state.management_form
        try:
            if (
                self.validate_max
                and state.total_form_count - len(state.deleted_indexes)
                > self.max_num
            ) or management_form.cleaned_data[TOTAL_FORM_COUNT] > self.absolute_max:
                raise ValidationError(
                    self.error_messages["too_many_forms"] % {"num": self.max_num},
                    code="too_many_forms",
                )
            if (
                self.validate_min
                and state.total_form_count
                - len(state.deleted_indexes)
                - state.empty_forms_count
                < self.min_num
            ):
                raise ValidationError(
                    self.error_messages["too_few_forms"] % {"num": self.min_num},
                    code="too_few_forms",
                )
            # Give self.clean() a chance to do cross-form validation; await
            # it when it returns an awaitable. It reads only the fully
            # populated cleaned_data of the child forms.
            result = self.clean()
            if inspect.isawaitable(result):
                await result
                self._async_raise_if_discarded(state)
        except ValidationError as e:
            state.non_form_errors = self.error_class(
                e.error_list,
                error_class="nonform",
                renderer=self.renderer,
            )

    def full_clean(self):
        """
        Clean all of self.data and populate self._errors and
        self._non_form_errors.
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
        # Drop the child forms, deletion/ordering bookkeeping and management
        # form cached for earlier inputs. A bound pass rebuilds them below;
        # an unbound pass returns before constructing anything, as before.
        self._forms_cache = _UNSET
        self.__dict__.pop("_deleted_form_indexes", None)
        self.__dict__.pop("_ordering", None)
        self._management_form_cache = None

        self._errors = []
        self._non_form_errors = self.error_class(
            error_class="nonform", renderer=self.renderer
        )
        empty_forms_count = 0

        if not self.is_bound:  # Stop further processing.
            self._record_validation_fingerprint()
            return

        # Rebuild the management form and the child forms directly rather than
        # going through the in-flight readers' guards on the properties: a
        # synchronous pass owns the live state, and detached async runners
        # keep driving their own private snapshots.
        self._management_form_cache = self._build_management_form()
        self._forms_cache = self._build_forms()

        if not self.management_form.is_valid():
            error = ValidationError(
                self.error_messages["missing_management_form"],
                params={
                    "field_names": ", ".join(
                        self.management_form.add_prefix(field_name)
                        for field_name in self.management_form.errors
                    ),
                },
                code="missing_management_form",
            )
            self._non_form_errors.append(error)

        for i, form in enumerate(self.forms):
            # Empty forms are unchanged forms beyond those with initial data.
            if not form.has_changed() and i >= self.initial_form_count():
                empty_forms_count += 1
            # Accessing errors calls full_clean() if necessary.
            # _should_delete_form() requires cleaned_data.
            form_errors = form.errors
            if self.can_delete and self._should_delete_form(form):
                continue
            self._errors.append(form_errors)
        try:
            if (
                self.validate_max
                and self.total_form_count() - len(self.deleted_forms) > self.max_num
            ) or self.management_form.cleaned_data[
                TOTAL_FORM_COUNT
            ] > self.absolute_max:
                raise ValidationError(
                    self.error_messages["too_many_forms"] % {"num": self.max_num},
                    code="too_many_forms",
                )
            if (
                self.validate_min
                and self.total_form_count()
                - len(self.deleted_forms)
                - empty_forms_count
                < self.min_num
            ):
                raise ValidationError(
                    self.error_messages["too_few_forms"] % {"num": self.min_num},
                    code="too_few_forms",
                )
            # Give self.clean() a chance to do cross-form validation.
            self.clean()
        except ValidationError as e:
            self._non_form_errors = self.error_class(
                e.error_list,
                error_class="nonform",
                renderer=self.renderer,
            )
        self._record_validation_fingerprint()

    def clean(self):
        """
        Hook for doing any extra formset-wide cleaning after Form.clean() has
        been called on every form. Any ValidationError raised by this method
        will not be associated with a particular form; it will be accessible
        via formset.non_form_errors()
        """
        pass

    def has_changed(self):
        """Return True if data in any form differs from initial."""
        return any(form.has_changed() for form in self)

    def add_fields(self, form, index):
        """A hook for adding extra fields on to each form instance."""
        initial_form_count = self.initial_form_count()
        if self.can_order:
            # Only pre-fill the ordering field for initial forms.
            if index is not None and index < initial_form_count:
                form.fields[ORDERING_FIELD_NAME] = IntegerField(
                    label=_("Order"),
                    initial=index + 1,
                    required=False,
                    widget=self.get_ordering_widget(),
                )
            else:
                form.fields[ORDERING_FIELD_NAME] = IntegerField(
                    label=_("Order"),
                    required=False,
                    widget=self.get_ordering_widget(),
                )
        if self.can_delete and (
            self.can_delete_extra or (index is not None and index < initial_form_count)
        ):
            form.fields[DELETION_FIELD_NAME] = BooleanField(
                label=_("Delete"),
                required=False,
                widget=self.get_deletion_widget(),
            )

    def add_prefix(self, index):
        return "%s-%s" % (self.prefix, index)

    def is_multipart(self):
        """
        Return True if the formset needs to be multipart, i.e. it
        has FileInput, or False otherwise.
        """
        if self.forms:
            return self.forms[0].is_multipart()
        else:
            return self.empty_form.is_multipart()

    @property
    def media(self):
        # All the forms on a FormSet are the same, so you only need to
        # interrogate the first form for media.
        if self.forms:
            return self.forms[0].media
        else:
            return self.empty_form.media

    @property
    def template_name(self):
        return self.renderer.formset_template_name

    def get_context(self):
        return {"formset": self}


def formset_factory(
    form,
    formset=BaseFormSet,
    extra=1,
    can_order=False,
    can_delete=False,
    max_num=None,
    validate_max=False,
    min_num=None,
    validate_min=False,
    absolute_max=None,
    can_delete_extra=True,
    renderer=None,
):
    """Return a FormSet for the given form class."""
    if min_num is None:
        min_num = DEFAULT_MIN_NUM
    if max_num is None:
        max_num = DEFAULT_MAX_NUM
    # absolute_max is a hard limit on forms instantiated, to prevent
    # memory-exhaustion attacks. Default to max_num + DEFAULT_MAX_NUM
    # (which is 2 * DEFAULT_MAX_NUM if max_num is None in the first place).
    if absolute_max is None:
        absolute_max = max_num + DEFAULT_MAX_NUM
    if max_num > absolute_max:
        raise ValueError("'absolute_max' must be greater or equal to 'max_num'.")
    attrs = {
        "form": form,
        "extra": extra,
        "can_order": can_order,
        "can_delete": can_delete,
        "can_delete_extra": can_delete_extra,
        "min_num": min_num,
        "max_num": max_num,
        "absolute_max": absolute_max,
        "validate_min": validate_min,
        "validate_max": validate_max,
        "renderer": renderer,
    }
    form_name = form.__name__
    if form_name.endswith("Form"):
        formset_name = form_name + "Set"
    else:
        formset_name = form_name + "FormSet"
    return type(formset_name, (formset,), attrs)


def all_valid(formsets):
    """Validate every formset and return True if all are valid."""
    # List comprehension ensures is_valid() is called for all formsets.
    return all([formset.is_valid() for formset in formsets])
