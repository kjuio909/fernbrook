"""
Form classes
"""

import asyncio
import copy
import datetime
from contextvars import ContextVar
from functools import partial
from inspect import isawaitable

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.forms.fields import Field, _reject_awaitable_in_sync
from django.forms.utils import ErrorDict, ErrorList, RenderableFormMixin
from django.forms.widgets import Media, MediaDefiningClass
from django.utils.datastructures import MultiValueDict
from django.utils.functional import cached_property
from django.utils.translation import gettext as _

from .renderers import get_default_renderer

__all__ = ("BaseForm", "Form")


# The _AsyncValidation of the asynchronous cleaning round currently running
# in this task. It only identifies the cleaning task (so the public
# errors/cleaned_data attributes gate *other* tasks away from the live,
# not-yet-committed round state). The round state itself lives on the form
# instance (self._errors/self.cleaned_data), exactly as in the synchronous
# flow, so existing cleaning code -- including direct self._errors access in
# ModelForm._post_clean() -- works unchanged.
_async_clean_state = ContextVar("baseform_async_clean_state", default=None)


class _AsyncValidation:
    """
    Bookkeeping for a single shared asynchronous round of full_clean().

    Concurrent callers share one instance so validators with side effects
    run only once. While the round runs it writes the live state onto the
    form; tasks other than the cleaning task are gated to empty views. When
    the round is abandoned (every caller cancels) or raises an ordinary
    exception, the previous committed state is restored, so the unfinished
    result is never exposed or cached.
    """

    __slots__ = (
        "form",
        "signature",
        "task",
        "settled",
        "waiters",
        "finished",
        "abandoned",
        "previous",
    )

    def __init__(self, form, signature):
        self.form = form
        # Input snapshots this round started with. They are swapped onto the
        # form while the round runs, so a task joining later (or a mutation
        # of form.data) cannot rewrite the inputs or the result.
        self.signature = signature
        self.task = None
        # Set once the cleaning task has fully finished tearing itself down
        # (including restoring the form inputs/state), so callers can wait
        # for an abandoned round to settle before retrying.
        self.settled = asyncio.Event()
        self.waiters = 0
        self.finished = False
        self.abandoned = False
        # The committed (errors, cleaned_data, signature) to restore if the
        # round is abandoned or fails.
        self.previous = None


def _validation_task_done(state, task):
    # Backstop for a task cancelled before it could run its own teardown:
    # detach it from the form and release anyone waiting to retry.
    if not task.cancelled():
        task.exception()
    form = state.form
    if form._async_validation is state:
        form._async_validation = None
    state.settled.set()


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
        self._cleaned_data = None  # Stores the cleaned data after clean().
        # Signature of the inputs (data/files/initial) represented by
        # ``_errors``/``_cleaned_data`` so repeated validation calls with
        # unchanged inputs can reuse the completed result.
        self._validation_signature = None
        # The shared _AsyncValidation of the asynchronous round currently in
        # progress, or None when no round is running.
        self._async_validation = None

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

    def _async_state(self):
        """
        Return the _AsyncValidation owned by the cleaning task running in
        this task for this form, or None.
        """
        state = _async_clean_state.get()
        if state is not None and state.form is self:
            return state
        return None

    @property
    def errors(self):
        """Return an ErrorDict for the data provided for the form."""
        if self._async_state() is None and self._async_validation is not None:
            # A round is running in another task: never expose its partial
            # errors nor the previous round's residue.
            return ErrorDict(renderer=self.renderer)
        if self._errors is None:
            self.full_clean()
        return self._errors

    @property
    def cleaned_data(self):
        if self._async_state() is None and self._async_validation is not None:
            # Cleaning is ongoing in another task: do not expose partial data
            # or the previously committed round.
            return {}
        if self._cleaned_data is None:
            raise AttributeError("cleaned_data")
        return self._cleaned_data

    @cleaned_data.setter
    def cleaned_data(self, value):
        self._cleaned_data = value

    def is_valid(self):
        """Return True if the form has no errors, or False otherwise."""
        return self.is_bound and not self.errors

    async def ais_valid(self):
        """
        Asynchronous counterpart of is_valid(): run the asynchronous
        validation (awaiting any awaitable results returned by field
        cleaning, field validators, clean_<field>() and clean()) and return
        True if the form has no errors, or False otherwise.
        """
        await self.afull_clean()
        return self.is_bound and not self._errors

    def _validation_inputs_signature(self):
        """
        Snapshot of the inputs validation depends on, used to reuse a
        completed round when the data/files/initial did not change.
        """
        return (
            self.is_bound,
            self.data.copy(),
            self.files.copy(),
            copy.copy(self.initial),
            self.empty_permitted,
        )

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

        errors = self.errors
        for field, error_list in error.items():
            if field not in errors:
                if field != NON_FIELD_ERRORS and field not in self.fields:
                    raise ValueError(
                        "'%s' has no field named '%s'."
                        % (self.__class__.__name__, field)
                    )
                if field == NON_FIELD_ERRORS:
                    errors[field] = self.error_class(
                        error_class="nonfield", renderer=self.renderer
                    )
                else:
                    errors[field] = self.error_class(
                        renderer=self.renderer,
                        field_id=self[field].auto_id,
                    )
            errors[field].extend(error_list)
            # Access cleaned_data only through the (possibly asynchronous)
            # attribute; keep it out of a local variable so sensitive values
            # are never captured in a traceback frame.
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
        self._validation_signature = self._validation_inputs_signature()
        if not self.is_bound:  # Stop further processing.
            return
        self.cleaned_data = {}
        # If the form is permitted to be empty, and none of the form data has
        # changed from the initial data, short circuit any validation.
        if self.empty_permitted and not self.has_changed():
            return

        try:
            self._clean_fields()
            self._clean_form()
            self._post_clean()
        except Exception:
            # An ordinary exception propagates as-is (it is not turned into a
            # form error), and the partial round must not become a reusable
            # cached result for a subsequent validation call. Drop only the
            # signature so nothing is reused; the partial ``_errors`` stays
            # accessible, preserving the synchronous behaviour.
            self._validation_signature = None
            raise

    async def afull_clean(self):
        """
        Asynchronous counterpart of full_clean(): put self.data through the
        asynchronous cleaning steps (awaiting awaitable results returned by
        field cleaning, field validators, clean_<field>() and clean()) and
        populate self._errors and self.cleaned_data with the result.

        Concurrent calls for the same inputs share a single round, so
        validators with side effects run at most once per round.
        """
        signature = self._validation_inputs_signature()
        # Reuse a completed (synchronous or asynchronous) round when the
        # inputs did not change.
        if (
            self._async_validation is None
            and self._errors is not None
            and self._validation_signature == signature
        ):
            return
        state = self._async_validation
        if state is not None:
            if state.abandoned or state.signature != signature:
                # The round in progress was given up by all of its waiters,
                # or it cleans different inputs. It cannot be rewritten; wait
                # for it to settle and then run a fresh round for the
                # current inputs.
                await state.settled.wait()
                await self.afull_clean()
                return
        else:
            state = _AsyncValidation(self, signature)
            self._async_validation = state
            state.task = asyncio.ensure_future(self._arun_validation(state))
            state.task.add_done_callback(partial(_validation_task_done, state))
        await self._await_validation(state)

    async def _await_validation(self, state):
        state.waiters += 1
        try:
            # shield(): a cancelled waiter abandons only its own wait; the
            # shared cleaning task keeps running while another waiter is
            # still waiting.
            await asyncio.shield(state.task)
        finally:
            state.waiters -= 1
            if (
                state.waiters == 0
                and not state.finished
                and not state.task.done()
            ):
                # Every waiter has left while the round was still running:
                # abandon it. The unfinished result and temporary errors are
                # discarded, so the next call can clean from scratch.
                state.abandoned = True
                state.task.cancel()

    async def _arun_validation(self, state):
        """The single shared cleaning task for one afull_clean() round."""
        token = _async_clean_state.set(state)
        # The round writes its live state onto the instance, just like the
        # synchronous flow. Direct self._errors/self.cleaned_data access in
        # cleaning hooks (e.g. ModelForm._post_clean()) therefore works.
        state.previous = (
            self._errors,
            self._cleaned_data,
            self._validation_signature,
        )
        saved_inputs = self._swap_validation_inputs(state.signature)
        # Bound fields cache their initial value; rebuild them so the round
        # reads the inputs it started with (and a retry reads new inputs).
        saved_bound_fields = self._bound_fields_cache
        self._bound_fields_cache = {}
        # changed_data is derived from the inputs and cached; drop it for the
        # round so it is recomputed against the round's own inputs.
        self.__dict__.pop("changed_data", None)
        try:
            await self._afull_clean()
        except BaseException:
            # Discard the unfinished result on cancellation and on ordinary
            # exceptions alike: restore the previous committed state so no
            # partial errors/data are exposed or reused. Ordinary exceptions
            # propagate to every waiter without becoming form errors.
            self._restore_committed_state(state)
            raise
        else:
            if state.abandoned:
                # The round was given up while it was still running (it only
                # got here because cleaning swallowed the cancellation). Its
                # result must not be committed.
                self._restore_committed_state(state)
                return
            state.finished = True
            self._validation_signature = state.signature
        finally:
            self._restore_validation_inputs(saved_inputs)
            self._bound_fields_cache = saved_bound_fields
            # Drop the round-computed cache; it is lazily recomputed against
            # the restored (current) inputs on the next access.
            self.__dict__.pop("changed_data", None)
            _async_clean_state.reset(token)
            if self._async_validation is state:
                self._async_validation = None
            state.settled.set()

    def _restore_committed_state(self, state):
        errors, cleaned_data, signature = state.previous
        self._errors = errors
        self._cleaned_data = cleaned_data
        self._validation_signature = signature

    def _swap_validation_inputs(self, signature):
        is_bound, data, files, initial, empty_permitted = signature
        saved = (
            self.is_bound,
            self.data,
            self.files,
            self.initial,
            self.empty_permitted,
        )
        self.is_bound = is_bound
        self.data = data
        self.files = files
        self.initial = initial
        self.empty_permitted = empty_permitted
        return saved

    def _restore_validation_inputs(self, saved):
        (
            self.is_bound,
            self.data,
            self.files,
            self.initial,
            self.empty_permitted,
        ) = saved

    async def _afull_clean(self):
        self._errors = ErrorDict(renderer=self.renderer)
        if not self.is_bound:  # Stop further processing.
            return
        self.cleaned_data = {}
        # If the form is permitted to be empty, and none of the form data has
        # changed from the initial data, short circuit any validation.
        if self.empty_permitted and not self.has_changed():
            return

        await self._aclean_fields()
        await self._aclean_form()
        await self._apost_clean()

    async def _aclean_fields(self):
        for name, bf in self._bound_items():
            field = bf.field
            try:
                self.cleaned_data[name] = await field._aclean_bound_field(bf)
                if hasattr(self, "clean_%s" % name):
                    value = getattr(self, "clean_%s" % name)()
                    if isawaitable(value):
                        value = await value
                    self.cleaned_data[name] = value
            except ValidationError as e:
                self.add_error(name, e)

    async def _aclean_form(self):
        try:
            cleaned_data = self.clean()
            if isawaitable(cleaned_data):
                cleaned_data = await cleaned_data
        except ValidationError as e:
            self.add_error(None, e)
        else:
            if cleaned_data is not None:
                self.cleaned_data = cleaned_data

    async def _apost_clean(self):
        """
        Asynchronous counterpart of _post_clean(). By default the
        synchronous hook runs; an awaitable result it returns is awaited.
        Used for model validation in model forms.
        """
        result = self._post_clean()
        if isawaitable(result):
            await result

    def _clean_fields(self):
        for name, bf in self._bound_items():
            field = bf.field
            try:
                self.cleaned_data[name] = field._clean_bound_field(bf)
                if hasattr(self, "clean_%s" % name):
                    value = getattr(self, "clean_%s" % name)()
                    if isawaitable(value):
                        _reject_awaitable_in_sync(
                            value, "%s.clean_%s()" % (type(self).__name__, name)
                        )
                    self.cleaned_data[name] = value
            except ValidationError as e:
                self.add_error(name, e)

    def _clean_form(self):
        try:
            cleaned_data = self.clean()
        except ValidationError as e:
            self.add_error(None, e)
        else:
            if isawaitable(cleaned_data):
                _reject_awaitable_in_sync(
                    cleaned_data, "%s.clean()" % type(self).__name__
                )
            if cleaned_data is not None:
                self.cleaned_data = cleaned_data

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
