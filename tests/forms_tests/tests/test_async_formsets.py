import asyncio

from django.core.exceptions import ValidationError
from django.forms import (
    BaseFormSet,
    CharField,
    Form,
    IntegerField,
    formset_factory,
)
from django.test import SimpleTestCase

from . import jinja2_tests


def async_validator(message=None):
    async def validator(value):
        await asyncio.sleep(0)
        if message is not None:
            raise ValidationError(message)

    return validator


class PersonForm(Form):
    name = CharField()
    votes = IntegerField(required=False)


PREFIX = "form"


def formset_data(rows, *, total=None, initial=0, prefix=PREFIX, delete=(), order=None):
    """Build bound POST data for a formset from per-row field mappings."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(total if total is not None else len(rows)),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "0",
    }
    for i, row in enumerate(rows):
        for key, value in row.items():
            data[f"{prefix}-{i}-{key}"] = value
    for i in delete:
        data[f"{prefix}-{i}-DELETE"] = "on"
    if order is not None:
        for i, value in enumerate(order):
            data[f"{prefix}-{i}-ORDER"] = value
    return data


class AsyncFormSetParityTests(SimpleTestCase):
    def _pair(self, data, **factory_kwargs):
        FormSet = formset_factory(PersonForm, **factory_kwargs)
        return FormSet(data), FormSet(data)

    def _compare(self, async_formset, sync_formset):
        self.assertEqual(
            [form_errors.as_json() for form_errors in async_formset.errors],
            [form_errors.as_json() for form_errors in sync_formset.errors],
        )
        self.assertEqual(
            async_formset.non_form_errors().as_json(),
            sync_formset.non_form_errors().as_json(),
        )

    async def test_valid_formset(self):
        data = formset_data([{"name": "John", "votes": "1"}, {"name": "Jane"}])
        async_fs, sync_fs = self._pair(data)
        self.assertIs(await async_fs.ais_valid(), True)
        sync_fs.is_valid()
        self._compare(async_fs, sync_fs)
        self.assertEqual(
            async_fs.cleaned_data,
            [
                {"name": "John", "votes": 1},
                {"name": "Jane", "votes": None},
            ],
        )
        self.assertEqual(async_fs.cleaned_data, sync_fs.cleaned_data)

    async def test_invalid_forms_match_sync(self):
        # Missing required name on the second form.
        data = formset_data([{"name": "John"}, {"votes": "2"}])
        async_fs, sync_fs = self._pair(data)
        self.assertIs(await async_fs.ais_valid(), False)
        sync_fs.is_valid()
        self._compare(async_fs, sync_fs)
        self.assertEqual(len(async_fs.errors), 2)
        self.assertIn("name", async_fs.errors[1])
        self.assertNotIn("name", async_fs.forms[1].cleaned_data)
        with self.assertRaises(AttributeError):
            async_fs.cleaned_data

    async def test_unbound_formset(self):
        FormSet = formset_factory(PersonForm, extra=2)
        async_fs, sync_fs = FormSet(), FormSet()
        self.assertIs(await async_fs.ais_valid(), False)
        sync_fs.is_valid()
        self._compare(async_fs, sync_fs)
        self.assertEqual(async_fs.errors, [])
        self.assertEqual(list(async_fs.non_form_errors()), [])
        with self.assertRaises(AttributeError):
            async_fs.cleaned_data

    async def test_empty_collection(self):
        data = formset_data([], total=0)
        async_fs, sync_fs = self._pair(data)
        self.assertIs(await async_fs.ais_valid(), True)
        sync_fs.is_valid()
        self._compare(async_fs, sync_fs)
        self.assertEqual(async_fs.cleaned_data, [])
        self.assertEqual(async_fs.errors, [])

    async def test_missing_management_form_matches_sync(self):
        data = {"form-0-name": "John"}
        async_fs, sync_fs = self._pair(data)
        self.assertIs(await async_fs.ais_valid(), False)
        sync_fs.is_valid()
        self._compare(async_fs, sync_fs)
        codes = [error.code for error in async_fs.non_form_errors().as_data()]
        self.assertEqual(codes, ["missing_management_form"])
        # No child form error is retained and cleaned_data is unavailable.
        self.assertEqual(async_fs.errors, [])
        with self.assertRaises(AttributeError):
            async_fs.cleaned_data

    async def test_management_errors_skip_child_validators(self):
        seen = []

        async def counting(value):
            seen.append(value)
            await asyncio.sleep(0)

        class F(Form):
            name = CharField(validators=[counting])

        FormSet = formset_factory(F)
        # TOTAL_FORMS must be an integer; a non-numeric value makes the
        # management form invalid.
        data = {
            "form-TOTAL_FORMS": "bogus",
            "form-INITIAL_FORMS": "0",
            "form-0-name": "John",
        }
        formset = FormSet(data)
        self.assertIs(await formset.ais_valid(), False)
        self.assertEqual(seen, [])

    async def test_extra_empty_forms_are_valid(self):
        data = formset_data([{"name": "John"}], total=3)
        async_fs, sync_fs = self._pair(data)
        self.assertIs(await async_fs.ais_valid(), True)
        sync_fs.is_valid()
        self._compare(async_fs, sync_fs)
        self.assertEqual(
            [d.get("name") for d in async_fs.cleaned_data], ["John", None, None]
        )
        # Empty extra forms short-circuit with no cleaned fields, like sync.
        self.assertEqual(async_fs.cleaned_data[1:], [{}, {}])

    async def test_formset_wide_clean_nonform_error(self):
        class Base(BaseFormSet):
            def clean(self):
                raise ValidationError("formset-wide bad")

        data = formset_data([{"name": "John"}])
        FormSet = formset_factory(PersonForm, formset=Base)
        async_fs, sync_fs = FormSet(data), FormSet(data)
        self.assertIs(await async_fs.ais_valid(), False)
        sync_fs.is_valid()
        self._compare(async_fs, sync_fs)
        self.assertIn("formset-wide bad", str(async_fs.non_form_errors()))

    async def test_async_formset_wide_clean_awaited(self):
        calls = []

        class Base(BaseFormSet):
            async def clean(self):
                await asyncio.sleep(0)
                calls.append([dict(d) for d in self.cleaned_data])

        data = formset_data([{"name": "John"}, {"name": "Jane"}])
        FormSet = formset_factory(PersonForm, formset=Base)
        formset = FormSet(data)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(
            calls,
            [[{"name": "John", "votes": None}, {"name": "Jane", "votes": None}]],
        )

    async def test_formset_clean_runs_once_per_round(self):
        class Base(BaseFormSet):
            calls = 0

            async def clean(self):
                await asyncio.sleep(0)
                type(self).calls += 1

        data = formset_data([{"name": "John"}])
        FormSet = formset_factory(PersonForm, formset=Base)
        formset = FormSet(data)
        await formset.ais_valid()
        await formset.ais_valid()
        self.assertEqual(FormSet.calls, 1)

    async def test_formset_cleaned_data_unavailable_when_child_invalid(self):
        data = formset_data([{"name": "John"}, {"votes": "1"}])
        formset = formset_factory(PersonForm)(data)
        self.assertIs(await formset.ais_valid(), False)
        with self.assertRaises(AttributeError):
            formset.cleaned_data
        # The second form itself still exposes its (empty) cleaned_data.
        self.assertEqual(formset.forms[0].cleaned_data, {"name": "John", "votes": None})


class AsyncFormSetFeaturesTests(SimpleTestCase):
    async def test_deleted_form_does_not_invalidate(self):
        FormSet = formset_factory(PersonForm, can_delete=True)
        # An initial form missing its required name but marked for deletion
        # plus a valid extra form.
        data = formset_data(
            [{"votes": "1"}, {"name": "Jane"}],
            total=2,
            initial=1,
            delete=(0,),
        )
        formset = FormSet(data)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(len(formset.deleted_forms), 1)
        # Deleted forms are not part of the retained errors.
        self.assertEqual(len(formset.errors), 1)
        self.assertEqual(
            [d.get("name") for d in formset.cleaned_data],
            [None, "Jane"],
        )
        self.assertTrue(formset.cleaned_data[0]["DELETE"])

    async def test_delete_on_unchanged_extra_form_matches_sync(self):
        # Ticking DELETE is itself a change, so -- like the synchronous path
        # -- the form is considered deleted.
        FormSet = formset_factory(PersonForm, can_delete=True)
        data = formset_data([{"name": "John"}, {}], total=2, delete=(1,))
        async_fs, sync_fs = FormSet(data), FormSet(data)
        self.assertIs(await async_fs.ais_valid(), True)
        sync_fs.is_valid()
        self.assertEqual(
            len(async_fs.deleted_forms), len(sync_fs.deleted_forms)
        )
        self.assertEqual(len(async_fs.deleted_forms), 1)

    async def test_deleted_forms_match_sync(self):
        FormSet = formset_factory(PersonForm, can_delete=True, extra=2)
        data = formset_data(
            [{"name": "John"}, {"name": "Jane"}, {"name": "Pat"}],
            total=3,
            initial=2,
            delete=(1,),
        )
        async_fs, sync_fs = FormSet(data), FormSet(data)
        self.assertIs(await async_fs.ais_valid(), True)
        sync_fs.is_valid()
        self.assertEqual(
            [f.cleaned_data for f in async_fs.deleted_forms],
            [f.cleaned_data for f in sync_fs.deleted_forms],
        )

    async def test_ordered_forms_match_sync(self):
        FormSet = formset_factory(PersonForm, can_order=True)
        data = formset_data(
            [{"name": "John"}, {"name": "Jane"}], order=("2", "1")
        )
        async_fs, sync_fs = FormSet(data), FormSet(data)
        self.assertIs(await async_fs.ais_valid(), True)
        sync_fs.is_valid()
        self.assertEqual(
            [f.cleaned_data["name"] for f in async_fs.ordered_forms],
            [f.cleaned_data["name"] for f in sync_fs.ordered_forms],
        )
        self.assertEqual(
            [f.cleaned_data["name"] for f in async_fs.ordered_forms],
            ["Jane", "John"],
        )

    async def test_ordered_forms_unknown_when_disabled(self):
        FormSet = formset_factory(PersonForm)
        formset = FormSet(formset_data([{"name": "John"}]))
        self.assertIs(await formset.ais_valid(), True)
        with self.assertRaises(AttributeError):
            formset.ordered_forms

    async def test_validate_max_error(self):
        FormSet = formset_factory(
            PersonForm, max_num=1, validate_max=True, absolute_max=10
        )
        data = formset_data([{"name": "John"}, {"name": "Jane"}])
        async_fs, sync_fs = FormSet(data), FormSet(data)
        self.assertIs(await async_fs.ais_valid(), False)
        sync_fs.is_valid()
        self.assertEqual(
            [e.code for e in async_fs.non_form_errors().as_data()],
            [e.code for e in sync_fs.non_form_errors().as_data()],
        )
        self.assertEqual(
            [e.code for e in async_fs.non_form_errors().as_data()],
            ["too_many_forms"],
        )

    async def test_validate_min_error_when_initial_form_deleted(self):
        FormSet = formset_factory(
            PersonForm, min_num=1, validate_min=True, can_delete=True, extra=0
        )
        # The only initial form is deleted, dropping the count below min_num.
        data = formset_data([{}], total=1, initial=1, delete=(0,))
        async_fs, sync_fs = FormSet(data), FormSet(data)
        self.assertIs(await async_fs.ais_valid(), False)
        sync_fs.is_valid()
        self.assertEqual(
            [e.code for e in async_fs.non_form_errors().as_data()],
            ["too_few_forms"],
        )

    async def test_deleted_form_counts_toward_max(self):
        FormSet = formset_factory(
            PersonForm, max_num=1, validate_max=True, can_delete=True,
            absolute_max=10,
        )
        data = formset_data(
            [{"name": "John"}, {"name": "Jane"}], total=2, initial=1, delete=(1,)
        )
        formset = FormSet(data)
        self.assertIs(await formset.ais_valid(), True)

    async def test_initial_forms_use_initial_data(self):
        FormSet = formset_factory(PersonForm, extra=0)
        data = formset_data([{"name": "posted"}], total=1, initial=1)
        initial = [{"name": "initial", "votes": 3}]
        async_fs, sync_fs = FormSet(data, initial=initial), FormSet(
            data, initial=initial
        )
        self.assertIs(await async_fs.ais_valid(), True)
        sync_fs.is_valid()
        # Bound posted values win over the formset-level initial data.
        self.assertEqual(async_fs.cleaned_data, sync_fs.cleaned_data)
        self.assertEqual(
            async_fs.cleaned_data, [{"name": "posted", "votes": None}]
        )


class AsyncMixedValidatorsTests(SimpleTestCase):
    async def test_mixed_sync_async_plain_validators(self):
        order = []

        def make_sync(tag):
            def validator(value):
                order.append(("sync", tag))

            return validator

        def make_async(tag):
            async def validator(value):
                await asyncio.sleep(0)
                order.append(("async", tag))

            return validator

        def make_plain(tag):
            def validator(value):
                order.append(("plain", tag))
                return object()  # A plain, non-awaitable result.

            return validator

        class F(Form):
            name = CharField(
                validators=[
                    make_sync("1"),
                    make_async("2"),
                    make_plain("3"),
                    make_async("4"),
                ]
            )

        FormSet = formset_factory(F, extra=1)
        formset = FormSet(formset_data([{"name": "x"}]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(
            order,
            [("sync", "1"), ("async", "2"), ("plain", "3"), ("async", "4")],
        )

    async def test_child_forms_cleaned_in_declaration_order(self):
        seen = []

        class F(Form):
            name = CharField()

            async def clean_name(self):
                seen.append(f"start-{self.prefix}")
                await asyncio.sleep(0)
                seen.append(f"end-{self.prefix}")
                return self.cleaned_data["name"]

        FormSet = formset_factory(F, extra=3)
        formset = FormSet(
            formset_data([{"name": "a"}, {"name": "b"}, {"name": "c"}])
        )
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(
            seen,
            [
                "start-form-0",
                "end-form-0",
                "start-form-1",
                "end-form-1",
                "start-form-2",
                "end-form-2",
            ],
        )

    async def test_async_child_validator_error_attribution(self):
        class F(Form):
            name = CharField(validators=[async_validator("child-bad")])

        FormSet = formset_factory(F, extra=2)
        formset = FormSet(formset_data([{"name": "a"}, {"name": "b"}]))
        self.assertIs(await formset.ais_valid(), False)
        self.assertIn("child-bad", str(formset.errors[0]["name"]))
        self.assertIn("child-bad", str(formset.errors[1]["name"]))
        self.assertNotIn("name", formset.forms[0].cleaned_data)


class AsyncFormSetConcurrencyTests(SimpleTestCase):
    def _make_formset(self, event, *, failure=None, counter=None, exc=None):
        async def gated(value):
            if counter is not None:
                counter.append(value)
            await event.wait()
            if exc is not None:
                raise exc
            if failure is not None:
                raise ValidationError(failure)

        class F(Form):
            name = CharField(validators=[gated])

        FormSet = formset_factory(F, extra=2)
        return FormSet(formset_data([{"name": "a"}, {"name": "b"}]))

    async def test_concurrent_calls_share_one_round(self):
        event = asyncio.Event()
        calls = []
        formset = self._make_formset(event, counter=calls)
        tasks = [asyncio.create_task(formset.ais_valid()) for _ in range(3)]
        await asyncio.sleep(0)
        # Mid-flight reads never expose a half-finished result.
        self.assertEqual(formset.errors, [])
        self.assertEqual(list(formset.non_form_errors()), [])
        event.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(results, [True, True, True])
        # One validator run per child form, not per caller.
        self.assertEqual(calls, ["a", "b"])

    async def test_concurrent_failure_shared(self):
        event = asyncio.Event()
        formset = self._make_formset(event, failure="shared-bad")
        tasks = [asyncio.create_task(formset.ais_valid()) for _ in range(3)]
        await asyncio.sleep(0)
        event.set()
        self.assertEqual(await asyncio.gather(*tasks), [False, False, False])
        self.assertIn("shared-bad", str(formset.errors[0]["name"]))

    async def test_concurrent_ordinary_exception_propagates(self):
        event = asyncio.Event()
        formset = self._make_formset(event, exc=RuntimeError("boom"))
        tasks = [asyncio.create_task(formset.ais_valid()) for _ in range(2)]
        await asyncio.sleep(0)
        event.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(r, RuntimeError) for r in results))
        self.assertTrue(all("boom" in str(r) for r in results))
        # Ordinary exceptions are never cached as validation state.
        self.assertIsNone(formset._errors)

    async def test_ordinary_exception_is_not_cached(self):
        calls = []

        def boom(value):
            calls.append(value)
            raise RuntimeError("kaboom")

        class F(Form):
            name = CharField(validators=[boom])

        formset = formset_factory(F, extra=1)(formset_data([{"name": "a"}]))
        with self.assertRaises(RuntimeError):
            await formset.ais_valid()
        with self.assertRaises(RuntimeError):
            await formset.ais_valid()
        # The ordinary exception did not cache a result: both rounds ran.
        self.assertEqual(calls, ["a", "a"])

    async def test_cancel_one_waiter_round_continues(self):
        event = asyncio.Event()
        formset = self._make_formset(event)
        keep = asyncio.create_task(formset.ais_valid())
        cancels = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0)
        cancels.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancels
        event.set()
        self.assertIs(await keep, True)

    async def test_all_waiters_cancel_then_retry(self):
        event = asyncio.Event()
        calls = []
        formset = self._make_formset(event, counter=calls)
        only = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0)
        only.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await only
        await asyncio.sleep(0.05)
        # The abandoned round left no published state.
        self.assertIsNone(formset._errors)
        self.assertIsNone(formset._non_form_errors)
        event.set()
        await asyncio.sleep(0.05)
        # The retry performs a complete new round; the abandoned runner never
        # reaches the second child form, so only the new round visits it.
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(calls, ["a", "a", "b"])
        self.assertEqual(
            [d["name"] for d in formset.cleaned_data], ["a", "b"]
        )

    async def test_surviving_waiter_gets_failure_after_cancel(self):
        event = asyncio.Event()
        formset = self._make_formset(event, failure="only-other")
        cancels = asyncio.create_task(formset.ais_valid())
        keep = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0)
        cancels.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancels
        event.set()
        self.assertIs(await keep, False)
        self.assertIn("only-other", str(formset.errors[0]["name"]))

    async def test_cancel_restores_last_completed_result(self):
        events = []
        first = asyncio.Event()
        first.set()
        events.append(first)
        calls = []

        async def gated(value):
            calls.append(value)
            await events[0].wait()

        class F(Form):
            name = CharField(validators=[gated])

        formset = formset_factory(F, extra=1)(
            formset_data([{"name": "done"}])
        )
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "done")

        # Block a second round over new inputs and cancel its only waiter.
        second = asyncio.Event()
        events[0] = second
        formset.data = formset_data([{"name": "in-flight"}], prefix=PREFIX)
        only = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0)
        only.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await only
        # The earlier successful result stays observable.
        self.assertEqual(formset.cleaned_data[0]["name"], "done")
        second.set()
        await asyncio.sleep(0.05)
        # The next call retries fully against the current inputs.
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "in-flight")


class AsyncFormSetCacheTests(SimpleTestCase):
    async def test_repeated_calls_run_child_validators_once(self):
        calls = []

        async def counting(value):
            calls.append(value)
            await asyncio.sleep(0)

        class F(Form):
            name = CharField(validators=[counting])

        formset = formset_factory(F)(formset_data([{"name": "a"}]))
        await formset.ais_valid()
        await formset.ais_valid()
        self.assertEqual(calls, ["a"])

    async def test_data_rebound_starts_new_snapshot(self):
        calls = []

        async def counting(value):
            calls.append(value)
            await asyncio.sleep(0)

        class F(Form):
            name = CharField(validators=[counting])

        formset = formset_factory(F, extra=1)(formset_data([{"name": "a"}]))
        self.assertIs(await formset.ais_valid(), True)
        formset.data = formset_data([{"name": "b"}], prefix=PREFIX)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(formset.cleaned_data[0]["name"], "b")

    async def test_data_mutated_in_place_invalidates(self):
        calls = []

        async def counting(value):
            calls.append(value)
            await asyncio.sleep(0)

        class F(Form):
            name = CharField(validators=[counting])

        formset = formset_factory(F, extra=1)(formset_data([{"name": "a"}]))
        await formset.ais_valid()
        formset.data["form-0-name"] = "b"
        await formset.ais_valid()
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(formset.cleaned_data[0]["name"], "b")

    async def test_previous_result_stays_visible_during_round(self):
        event = asyncio.Event()

        async def gated(value):
            await event.wait()

        class F(Form):
            name = CharField()

        FormSet = formset_factory(F, extra=1)
        formset = FormSet(formset_data([{"name": "done"}]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "done")

        formset.data = formset_data([{"name": "in-flight"}], prefix=PREFIX)
        # Attach the gate to the base field so the forms of the new round
        # block when they are built and cleaned.
        F.base_fields["name"].validators.append(gated)
        try:
            task = asyncio.create_task(formset.ais_valid())
            await asyncio.sleep(0.05)
            # While the new round is unfinished, observable state is the last
            # complete result; no half-finished child data leaks.
            self.assertEqual(formset.cleaned_data[0]["name"], "done")
            self.assertEqual(formset.errors, [{}])
            self.assertIs(formset.is_valid(), True)
            event.set()
            self.assertIs(await task, True)
            self.assertEqual(formset.cleaned_data[0]["name"], "in-flight")
        finally:
            F.base_fields["name"].validators.remove(gated)

    async def test_form_count_change_invalidates(self):
        calls = []

        async def counting(value):
            calls.append(value)
            await asyncio.sleep(0)

        class F(Form):
            name = CharField(validators=[counting])

        data = formset_data([{"name": "a"}])
        formset = formset_factory(F, extra=2)(data)
        await formset.ais_valid()
        self.assertEqual(calls, ["a"])
        formset.data["form-TOTAL_FORMS"] = "2"
        formset.data["form-1-name"] = "b"
        await formset.ais_valid()
        self.assertEqual(calls, ["a", "a", "b"])
        self.assertEqual([d["name"] for d in formset.cleaned_data], ["a", "b"])

    async def test_same_error_new_inputs_are_revalidated(self):
        seen = []

        async def always_bad(value):
            seen.append(value)
            await asyncio.sleep(0)
            raise ValidationError("always-bad")

        class F(Form):
            name = CharField(validators=[always_bad])

        formset = formset_factory(F, extra=1)(formset_data([{"name": "a"}]))
        self.assertIs(await formset.ais_valid(), False)
        formset.data = formset_data([{"name": "b"}], prefix=PREFIX)
        self.assertIs(await formset.ais_valid(), False)
        self.assertEqual(seen, ["a", "b"])

    async def test_field_definition_change_invalidates(self):
        calls = []

        async def counting(value):
            calls.append(value)
            await asyncio.sleep(0)

        class F(Form):
            name = CharField()

        FormSet = formset_factory(F, extra=1)
        formset = FormSet(formset_data([{"name": "a"}]))
        await formset.ais_valid()
        F.base_fields["name"].validators.append(counting)
        try:
            await formset.ais_valid()
            self.assertEqual(calls, ["a"])
        finally:
            F.base_fields["name"].validators.remove(counting)

    async def test_initial_mutated_in_place_invalidates(self):
        class F(Form):
            name = CharField(disabled=True)

        FormSet = formset_factory(F, extra=0)
        initial = [{"name": "one"}]
        data = formset_data([{"name": "posted"}], total=1, initial=1)
        formset = FormSet(data, initial=initial)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "one")
        initial[0]["name"] = "two"
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "two")


class AsyncFormSetInterleaveTests(SimpleTestCase):
    def _make_formset(self, *, failure=None):
        started = asyncio.Event()
        gate = asyncio.Event()

        async def maybe_gated(value):
            if holder.block:
                started.set()
                await gate.wait()
                if failure is not None:
                    raise ValidationError(failure)

        class F(Form):
            name = CharField(validators=[maybe_gated])

        holder = type("H", (), {"block": True})()
        FormSet = formset_factory(F, extra=0)
        # INITIAL_FORMS=1 keeps the single form required, so an emptied
        # posting produces a "required" error on the synchronous pass.
        formset = FormSet(
            formset_data([{"name": "old"}], initial=1)
        )
        return formset, holder, started, gate

    async def test_async_reuses_completed_sync_result(self):
        calls = []

        async def counting(value):
            calls.append(value)
            await asyncio.sleep(0)

        class F(Form):
            name = CharField(validators=[counting])

        formset = formset_factory(F, extra=1)(formset_data([{"name": "a"}]))
        self.assertIs(formset.is_valid(), True)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(calls, [])

    async def test_sync_reuses_completed_async_result(self):
        calls = []

        async def counting(value):
            calls.append(value)
            await asyncio.sleep(0)

        class F(Form):
            name = CharField(validators=[counting])

        formset = formset_factory(F, extra=1)(formset_data([{"name": "a"}]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertIs(formset.is_valid(), True)
        self.assertEqual(calls, ["a"])

    async def test_input_change_after_sync_reruns_async(self):
        calls = []

        async def counting(value):
            calls.append(value)
            await asyncio.sleep(0)

        class F(Form):
            name = CharField(validators=[counting])

        formset = formset_factory(F, extra=1)(formset_data([{"name": "a"}]))
        self.assertIs(formset.is_valid(), True)
        formset.data = formset_data([{"name": "b"}], prefix=PREFIX)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "b")
        self.assertEqual(calls, ["b"])

    async def test_late_async_result_does_not_overwrite_sync(self):
        formset, holder, started, gate = self._make_formset()
        task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        # A synchronous pass over new inputs completes while the async round
        # is blocked inside a child validator.
        formset.data = formset_data([{"name": "new"}], prefix=PREFIX)
        holder.block = False
        formset.full_clean()
        self.assertEqual(formset.cleaned_data[0]["name"], "new")
        gate.set()
        self.assertIs(await task, True)
        # The stale async completion served its own waiter but never
        # publishes over the newer synchronous result.
        self.assertEqual(formset.cleaned_data[0]["name"], "new")
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "new")

    async def test_late_async_failure_keeps_sync_errors(self):
        formset, holder, started, gate = self._make_formset(failure="async-old")
        task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        formset.data = formset_data([{}], prefix=PREFIX, initial=1)
        holder.block = False
        formset.full_clean()
        sync_errors = [e.as_json() for e in formset.errors]
        self.assertIn("required", sync_errors[0])
        gate.set()
        self.assertIs(await task, False)
        self.assertEqual(
            [e.as_json() for e in formset.errors], sync_errors
        )

    async def test_cancelling_stale_waiter_keeps_sync_result(self):
        formset, holder, started, gate = self._make_formset()
        task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        holder.block = False
        formset.full_clean()
        self.assertEqual(formset.cleaned_data[0]["name"], "old")
        task.cancel()
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "old")


class AsyncSupersededRoundTests(SimpleTestCase):
    def _make_gated_formset(self, *, stale_failure=False, stale_exc=False):
        old_gate = asyncio.Event()
        new_gate = asyncio.Event()
        gates = [old_gate, new_gate]
        round_index = [0]

        async def gated(value):
            index = round_index[0]
            await gates[index].wait()
            if index == 0:
                if stale_exc:
                    raise RuntimeError("stale-boom")
                if stale_failure:
                    raise ValidationError("stale-bad")

        class F(Form):
            name = CharField(validators=[gated])

        FormSet = formset_factory(F, extra=1)
        formset = FormSet(formset_data([{"name": "old"}]))
        return formset, gates, round_index

    async def test_superseded_round_serves_waiter_but_does_not_publish(self):
        formset, gates, round_index = self._make_gated_formset()
        old_task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0.05)
        formset.data = formset_data([{"name": "new"}], prefix=PREFIX)
        round_index[0] = 1
        new_task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0)
        gates[1].set()
        self.assertIs(await new_task, True)
        self.assertEqual(formset.cleaned_data[0]["name"], "new")
        # The stale round's late success cannot publish its snapshot.
        gates[0].set()
        self.assertIs(await old_task, True)
        self.assertEqual(formset.cleaned_data[0]["name"], "new")

    async def test_late_exception_does_not_corrupt_newer_state(self):
        formset, gates, round_index = self._make_gated_formset(stale_exc=True)
        old_task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0.05)
        formset.data = formset_data([{"name": "new"}], prefix=PREFIX)
        round_index[0] = 1
        new_task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0)
        gates[1].set()
        self.assertIs(await new_task, True)
        self.assertEqual(formset.cleaned_data[0]["name"], "new")
        # The stale round's ordinary exception is delivered to its waiter
        # only and leaves the newer result intact.
        gates[0].set()
        with self.assertRaises(RuntimeError):
            await old_task
        self.assertEqual(formset.cleaned_data[0]["name"], "new")

    async def test_late_validation_error_keeps_newer_success(self):
        formset, gates, round_index = self._make_gated_formset(
            stale_failure=True
        )
        old_task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0.05)
        formset.data = formset_data([{"name": "new"}], prefix=PREFIX)
        round_index[0] = 1
        new_task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0)
        gates[1].set()
        self.assertIs(await new_task, True)
        # The stale round's late ValidationError is delivered to its waiter
        # but cannot replace the newer successful state.
        gates[0].set()
        self.assertIs(await old_task, False)
        self.assertIs(formset.is_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "new")


class AsyncFormSetConfigFreezeTests(SimpleTestCase):
    """A waiting round keeps the configuration it started with.

    The deletion/ordering switches, the extra rows, the number limits and the
    validation switches may be reassigned while an asynchronous round is
    parked in an async validator. The old round must finish on its frozen
    configuration; only a later call adopts the new configuration.
    """

    @staticmethod
    def _gated_formset(event, data, **factory_kwargs):
        async def gated(value):
            await event.wait()

        class F(Form):
            name = CharField(validators=[gated])

        FormSet = formset_factory(F, **factory_kwargs)
        return FormSet(data)

    async def test_deletion_switch_change_keeps_old_round_on_snapshot(self):
        event = asyncio.Event()
        # INITIAL_FORMS=1 makes the row required; it is emptied and marked for
        # deletion, which exempts it only while can_delete is in effect.
        data = formset_data([{}], initial=1, delete=[0])
        formset = self._gated_formset(event, data, can_delete=True, extra=0)
        task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0.05)
        formset.can_delete = False
        event.set()
        # The waiting round froze can_delete=True, so the deleted row is
        # exempt and its "required" error cannot make the round fail.
        self.assertIs(await task, True)
        # The deleted row is omitted from the retained errors entirely.
        self.assertEqual(formset.errors, [])
        # The published child forms were built with the deletion machinery.
        self.assertIn("DELETE", formset.forms[0].fields)
        # A later call starts a fresh round with can_delete=False.
        self.assertIs(await formset.ais_valid(), False)
        self.assertIn("name", formset.errors[0])

    async def test_validate_max_switch_is_frozen(self):
        event = asyncio.Event()
        data = formset_data([{"name": "a"}, {"name": "b"}])
        formset = self._gated_formset(
            event, data, extra=0, max_num=1, validate_max=True, absolute_max=10
        )
        task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0.05)
        formset.validate_max = False
        event.set()
        # The old round froze validate_max=True: two forms exceed max_num=1.
        self.assertIs(await task, False)
        errors = formset.non_form_errors().as_data()
        self.assertEqual(errors[0].code, "too_many_forms")
        # Only the new configuration (validate_max=False) drops the error.
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(list(formset.non_form_errors()), [])

    async def test_max_num_change_is_frozen(self):
        event = asyncio.Event()
        data = formset_data([{"name": "a"}, {"name": "b"}])
        formset = self._gated_formset(
            event, data, extra=0, max_num=5, validate_max=True, absolute_max=10
        )
        task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0.05)
        formset.max_num = 1
        event.set()
        # The old round froze max_num=5, so two forms are within the limit.
        self.assertIs(await task, True)
        self.assertEqual(list(formset.non_form_errors()), [])
        # The tightened limit only governs a fresh round.
        self.assertIs(await formset.ais_valid(), False)
        self.assertEqual(
            formset.non_form_errors().as_data()[0].code, "too_many_forms"
        )

    async def test_ordering_switch_is_frozen(self):
        event = asyncio.Event()
        data = formset_data([{"name": "a"}, {"name": "b"}], order=["2", "1"])
        formset = self._gated_formset(event, data, can_order=True, extra=0)
        task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0.05)
        formset.can_order = False
        event.set()
        self.assertIs(await task, True)
        # The frozen round built its child forms with the ordering field.
        self.assertIn("ORDER", formset.forms[0].fields)
        # A fresh round with can_order=False builds no ordering machinery.
        self.assertIs(await formset.ais_valid(), True)
        self.assertNotIn("ORDER", formset.forms[0].fields)
        with self.assertRaises(AttributeError):
            formset.ordered_forms


# The Jinja2 renderer only affects rendering; run the same parity checks on it
# to make sure the async path carries the renderer through.
@jinja2_tests
class AsyncFormSetJinja2Tests(SimpleTestCase):
    async def test_valid_with_jinja2_renderer(self):
        FormSet = formset_factory(PersonForm, extra=1)
        formset = FormSet(formset_data([{"name": "John"}]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "John")
