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
from django.utils.functional import cached_property
from django.utils.translation import gettext as _

from .renderers import get_default_renderer

__all__ = ("BaseForm", "Form")

# Marker for "cleaned_data was never populated" (accessing it raises
# AttributeError, as on a form that has never been validated).
_UNSET = object()


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

    def __init__(self, *, previous_errors, previous_cleaned_data, inputs):
        self.runner_task = None
        # Number of ais_valid() callers currently awaiting the runner task.
        self.waiters = 0
        # Set once the round has either completed or been abandoned.
        self.finalized = False
        # State to restore if the round is abandoned because every waiter
        # was cancelled.
        self.previous_errors = previous_errors
        self.previous_cleaned_data = previous_cleaned_data
        # Identity snapshot of (data, files, initial) the round is cleaning.
        self.inputs = inputs


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
        self.data = MultiValueDict() if data is None else data
        self.files = MultiValueDict() if files is None else files
        self.auto_id = auto_id
        if prefix is not None:
            self.prefix = prefix
        self.initial = initial or {}
        self.error_class = error_class
        # Translators: This is the default suffix added to form field labels
        self.label_suffix = label_suffix if label_suffix is not None else _(":")
        self.empty_permitted = empty_permitted
        self._errors = None  # Stores the errors after clean() has been called.
        # cleaned_data is only populated during a (a)full_clean(); before
        # that it raises AttributeError like a plain unset attribute.
        self._cleaned_data = _UNSET
        # State for an in-progress asynchronous validation round (ais_valid()).
        # None when no async round is running.
        self._async_validation = None
        # Snapshot of the inputs used by the last completed validation round,
        # and whether the last round was abandoned (all waiters cancelled).
        self._validated_inputs = None
        self._async_abandoned = False

        # The base_fields class attribute is the *class-wide* definition of
        # fields. Because a particular *instance* of the class might want to
        # alter self.fields, we create self.fields here by copying base_fields.
        # Instances should always modify self.fields; they should not modify
        # self.base_fields.
        self.fields = copy.deepcopy(self.base_fields)
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

    def __getitem__(self, name):
        """Return a BoundField with the given name."""
        try:
            field = self.fields[name]
        except KeyError:
            raise KeyError(
                "Key '%s' not found in '%s'. Choices are: %s."
                % (
                    name,
                    self.__class__.__name__,
                    ", ".join(sorted(self.fields)),
                )
            )
        if name not in self._bound_fields_cache:
            self._bound_fields_cache[name] = field.get_bound_field(self, name)
        return self._bound_fields_cache[name]

    @property
    def errors(self):
        """Return an ErrorDict for the data provided for the form."""
        # While an asynchronous validation round is in progress, only the
        # task running the round itself may observe the (possibly partial)
        # error collection. Every other caller gets a detached, empty
        # ErrorDict instead of a half-finished or stale result.
        state = self._async_validation
        if state is not None and asyncio.current_task() is not state.runner_task:
            return ErrorDict(renderer=self.renderer)
        if self._errors is None:
            self.full_clean()
        return self._errors

    @property
    def cleaned_data(self):
        state = self._async_validation
        if state is not None and asyncio.current_task() is not state.runner_task:
            # Never expose partially populated data to code outside the
            # task running the asynchronous validation round.
            return {}
        if self._cleaned_data is _UNSET:
            raise AttributeError(
                "'%s' object has no attribute 'cleaned_data'"
                % self.__class__.__name__
            )
        return self._cleaned_data

    @cleaned_data.setter
    def cleaned_data(self, value):
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

        Concurrent calls on the same form instance share a single cleaning
        round: side-effectful validators run only once, and callers joining
        later cannot change the inputs or outcome. If every waiting caller
        is cancelled, the unfinished round is discarded (including its
        temporary errors) and the next call performs a full retry.
        """
        state = self._async_validation
        if state is None:
            if self._can_reuse_validation():
                return self.is_bound and not self._errors
            state = self._start_async_validation()
        state.waiters += 1
        try:
            await asyncio.shield(state.runner_task)
        except asyncio.CancelledError:
            state.waiters -= 1
            if state.waiters == 0 and not state.finalized:
                # Every waiter has given up: abandon the unfinished round
                # synchronously so that a follow-up call starts a fresh one
                # even if the runner task has not processed its cancellation
                # yet.
                self._abandon_async_validation(state)
                state.runner_task.cancel()
            raise
        except BaseException:
            state.waiters -= 1
            raise
        state.waiters -= 1
        return self.is_bound and not self._errors

    def _can_reuse_validation(self):
        """Reuse a completed round when inputs are unchanged and the last
        round was not abandoned by a wholesale cancellation."""
        if self._async_abandoned or self._errors is None:
            return False
        snapshot = self._validated_inputs
        return snapshot is not None and all(
            current is original
            for current, original in zip(
                (self.data, self.files, self.initial),
                snapshot,
                strict=True,
            )
        )

    def _start_async_validation(self):
        loop = asyncio.get_running_loop()
        state = _AsyncValidationState(
            previous_errors=self._errors,
            previous_cleaned_data=self._cleaned_data,
            inputs=(self.data, self.files, self.initial),
        )
        self._async_validation = state
        self._async_abandoned = False
        # The runner installs fresh staging collections as its first step.
        # Until then this task does not await anything, so the previous
        # collections cannot be observed mid-transition by another task.
        state.runner_task = loop.create_task(self._arun_async_validation(state))
        return state

    async def _arun_async_validation(self, state):
        # The round may already have been abandoned synchronously (every
        # waiter cancelled) before this task got its first step.
        if state.finalized:
            return
        completed = False
        try:
            await self._afull_clean(state)
            completed = True
        finally:
            # Finalization also runs if this task is cancelled before or
            # while the cleaning coroutine runs. A round abandoned directly
            # by the last waiter (or superseded by a newer round) is left
            # untouched.
            if self._async_validation is state and not state.finalized:
                if completed:
                    self._finish_async_validation(state)
                else:
                    self._abandon_async_validation(state)

    def _finish_async_validation(self, state):
        state.finalized = True
        self._validated_inputs = state.inputs
        self._async_abandoned = False
        self._async_validation = None

    def _abandon_async_validation(self, state):
        state.finalized = True
        # Drop the temporary, possibly partial collections and roll back to
        # the last completed state. No failure is cached.
        self._errors = state.previous_errors
        self._cleaned_data = state.previous_cleaned_data
        self._async_abandoned = True
        self._async_validation = None

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
                    self._errors[field] = self.error_class(
                        error_class="nonfield", renderer=self.renderer
                    )
                else:
                    self._errors[field] = self.error_class(
                        renderer=self.renderer,
                        field_id=self[field].auto_id,
                    )
            self._errors[field].extend(error_list)
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
        self._errors = ErrorDict(renderer=self.renderer)
        if not self.is_bound:  # Stop further processing.
            self._record_validation_inputs()
            return
        self.cleaned_data = {}
        # If the form is permitted to be empty, and none of the form data has
        # changed from the initial data, short circuit any validation.
        if self.empty_permitted and not self.has_changed():
            self._record_validation_inputs()
            return

        self._clean_fields()
        self._clean_form()
        self._post_clean()
        self._record_validation_inputs()

    def _record_validation_inputs(self):
        self._validated_inputs = (self.data, self.files, self.initial)
        self._async_abandoned = False

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

    def _async_raise_if_detached(self, state):
        """Stop a runner whose round was abandoned or superseded.

        A runner can keep running after cancellation if user code (a
        validator or a clean hook) swallows CancelledError. Such a detached
        runner must not write into the restored or newer form state, so it
        is re-cancelled at the next pipeline boundary.
        """
        if state.finalized or self._async_validation is not state:
            raise asyncio.CancelledError()

    async def _aclean_fields(self, state):
        for name, bf in self._bound_items():
            self._async_raise_if_detached(state)
            field = bf.field
            try:
                value = await field._aclean_bound_field(bf)
                # The value is written to cleaned_data only after any
                # awaitable field cleaning has resolved, so partial results
                # never persist.
                self._async_raise_if_detached(state)
                self.cleaned_data[name] = value
                if hasattr(self, "clean_%s" % name):
                    hook = getattr(self, "clean_%s" % name)
                    value = hook()
                    if inspect.isawaitable(value):
                        value = await value
                        self._async_raise_if_detached(state)
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
        self._async_raise_if_detached(state)
        try:
            cleaned_data = self.clean()
            if inspect.isawaitable(cleaned_data):
                cleaned_data = await cleaned_data
                self._async_raise_if_detached(state)
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
            self._async_raise_if_detached(state)

    async def _afull_clean(self, state):
        """
        Async counterpart of full_clean(). Field cleaning runs in
        declaration order and the form-wide clean hook only reads field
        results that have already completed.
        """
        self._errors = ErrorDict(renderer=self.renderer)
        if not self.is_bound:  # Stop further processing.
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
        self._async_raise_if_detached(state)
        result = self.add_error(field, error)
        if inspect.isawaitable(result):
            await result
            self._async_raise_if_detached(state)

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

    @cached_property
    def changed_data(self):
        return [name for name, bf in self._bound_items() if bf._has_changed()]

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
