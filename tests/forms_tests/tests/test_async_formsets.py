import asyncio

from django.core.exceptions import ValidationError
from django.forms import CharField, Form, formset_factory
from django.forms.formsets import BaseFormSet
from django.test import SimpleTestCase


def async_validator(message=None):
    async def validator(value):
        await asyncio.sleep(0)
        if message is not None:
            raise ValidationError(message)

    return validator


class NameForm(Form):
    name = CharField()


NameFormSet = formset_factory(NameForm, extra=0)


class AsyncBaseFormSet(BaseFormSet):
    # Hand-written formset classes do not go through formset_factory(), so
    # provide the same defaults the factory would set.
    renderer = None
    extra = 1
    can_order = False
    can_delete = False
    can_delete_extra = True
    min_num = 0
    max_num = 1000
    absolute_max = 2000
    validate_min = False
    validate_max = False


def management_data(total=0, initial=0, prefix="form", **extra):
    data = {
        f"{prefix}-TOTAL_FORMS": str(total),
        f"{prefix}-INITIAL_FORMS": str(initial),
    }
    data.update(extra)
    return data


def formset_data(values=(), total=None, initial=0, *, prefix="form", **extra):
    total = len(values) if total is None else total
    data = management_data(total, initial, prefix=prefix)
    for i, value in enumerate(values):
        data[f"{prefix}-{i}-name"] = value
    data.update(extra)
    return data


def non_form_messages(formset):
    return [str(error) for error in formset.non_form_errors()]


def form_errors_as_json(formset):
    return [errors.as_json() for errors in formset.errors]


class AsyncIsValidTests(SimpleTestCase):
    async def test_all_sync_valid(self):
        formset = NameFormSet(formset_data(["John", "Lennon"]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(
            formset.cleaned_data,
            [{"name": "John"}, {"name": "Lennon"}],
        )

    async def test_invalid_matches_sync(self):
        data = formset_data(["John"], total=2, initial=2)
        sync_formset = NameFormSet(data)
        sync_formset.is_valid()
        async_formset = NameFormSet(data)
        self.assertIs(await async_formset.ais_valid(), False)
        self.assertEqual(
            form_errors_as_json(async_formset), form_errors_as_json(sync_formset)
        )
        self.assertEqual(
            non_form_messages(async_formset), non_form_messages(sync_formset)
        )
        self.assertIn("name", async_formset.errors[1])
        self.assertNotIn("name", async_formset.forms[1].cleaned_data)

    async def test_unbound_is_invalid(self):
        formset = NameFormSet(initial=[{"name": "John"}])
        self.assertIs(await formset.ais_valid(), False)
        self.assertEqual(formset.errors, [])
        self.assertEqual(non_form_messages(formset), [])
        with self.assertRaises(AttributeError):
            formset.cleaned_data

    async def test_empty_collection(self):
        formset = NameFormSet(management_data(total=0))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data, [])
        self.assertEqual(formset.total_error_count(), 0)

    async def test_extra_empty_forms_are_permitted(self):
        FormSet = formset_factory(NameForm, extra=1)
        # One rendered extra form submitted completely empty.
        data = formset_data([], total=1)
        sync_formset = FormSet(data)
        sync_formset.is_valid()
        async_formset = FormSet(data)
        self.assertIs(await async_formset.ais_valid(), True)
        self.assertEqual(
            form_errors_as_json(async_formset), form_errors_as_json(sync_formset)
        )
        self.assertEqual(async_formset.cleaned_data, [{}])

    async def test_async_validator_is_awaited(self):
        class F(Form):
            name = CharField(validators=[async_validator()])

        FormSet = formset_factory(F, extra=0)
        formset = FormSet(formset_data(["x"]))
        self.assertIs(await formset.ais_valid(), True)

    async def test_async_validator_error_attribution(self):
        async def validator(value):
            await asyncio.sleep(0)
            if value == "bad":
                raise ValidationError("bad name")

        class F(Form):
            name = CharField(validators=[validator])

        FormSet = formset_factory(F, extra=0)
        formset = FormSet(formset_data(["good", "bad"], total=2))
        self.assertIs(await formset.ais_valid(), False)
        self.assertEqual(dict(formset.errors[0]), {})
        self.assertIn("bad name", str(formset.errors[1]["name"]))
        self.assertNotIn("name", formset.forms[1].cleaned_data)
        self.assertEqual(formset.forms[0].cleaned_data, {"name": "good"})

    async def test_mixed_sync_async_plain_validators(self):
        def plain_return(value):
            return object()

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

        class F(Form):
            name = CharField(
                validators=[
                    make_sync("1"),
                    make_async("2"),
                    make_sync("3"),
                    plain_return,
                ]
            )

        FormSet = formset_factory(F, extra=0)
        formset = FormSet(formset_data(["x"]))
        self.assertIs(await formset.ais_valid(), True)
        # Plain values are passed through, never awaited, and the cleaned
        # value itself is unchanged.
        self.assertEqual(order, [("sync", "1"), ("async", "2"), ("sync", "3")])
        self.assertEqual(formset.forms[0].cleaned_data["name"], "x")

    async def test_child_forms_clean_in_declaration_order(self):
        order = []

        def make_form(i):
            async def clean_name(self):
                value = self.cleaned_data["name"]
                order.append(("start", value))
                await asyncio.sleep(0)
                order.append(("end", value))
                return value

            return type(
                "OrderedForm",
                (Form,),
                {
                    "name": CharField(),
                    "clean_name": clean_name,
                },
            )

        first = make_form(0)
        second = make_form(1)

        class OrderedFormSet(AsyncBaseFormSet):
            form = first

            def _construct_form(self, i, **kwargs):
                if i == 1:
                    self.form = second
                try:
                    return super()._construct_form(i, **kwargs)
                finally:
                    self.form = first

        formset = OrderedFormSet(formset_data(["a", "b"]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(
            order,
            [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")],
        )

    async def test_async_formset_clean_non_form_error(self):
        class AsyncCleanFormSet(AsyncBaseFormSet):
            async def clean(self):
                await asyncio.sleep(0)
                raise ValidationError("formset-wide bad")

        FormSet = formset_factory(NameForm, formset=AsyncCleanFormSet, extra=0)
        formset = FormSet(formset_data(["x"]))
        self.assertIs(await formset.ais_valid(), False)
        self.assertEqual(non_form_messages(formset), ["formset-wide bad"])
        self.assertEqual(dict(formset.errors[0]), {})

    async def test_async_formset_clean_reads_all_cleaned_data(self):
        seen = []

        class AsyncCleanFormSet(AsyncBaseFormSet):
            async def clean(self):
                await asyncio.sleep(0)
                for cleaned in self.cleaned_data:
                    seen.append(cleaned["name"])

        FormSet = formset_factory(NameForm, formset=AsyncCleanFormSet, extra=0)
        formset = FormSet(formset_data(["a", "b"]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(seen, ["a", "b"])

    async def test_sync_formset_clean_still_supported(self):
        class SyncCleanFormSet(AsyncBaseFormSet):
            def clean(self):
                names = [cleaned["name"] for cleaned in self.cleaned_data]
                if len(names) != len(set(names)):
                    raise ValidationError("duplicate names")

        FormSet = formset_factory(NameForm, formset=SyncCleanFormSet, extra=0)
        formset = FormSet(formset_data(["a", "a"]))
        self.assertIs(await formset.ais_valid(), False)
        self.assertEqual(non_form_messages(formset), ["duplicate names"])


class ManagementFormTests(SimpleTestCase):
    async def test_missing_management_fields(self):
        data = {
            "form-TOTAL_FORMS": "",
            "form-INITIAL_FORMS": "",
            "form-0-name": "x",
        }
        sync_formset = NameFormSet(data)
        sync_formset.is_valid()
        async_formset = NameFormSet(data)
        self.assertIs(await async_formset.ais_valid(), False)
        self.assertEqual(
            non_form_messages(async_formset), non_form_messages(sync_formset)
        )
        self.assertIn(
            "ManagementForm data is missing",
            non_form_messages(async_formset)[0],
        )
        self.assertEqual(async_formset.errors, [])

    async def test_invalid_management_does_not_run_child_validators(self):
        calls = 0

        def counting_validator(value):
            nonlocal calls
            calls += 1

        class F(Form):
            name = CharField(validators=[counting_validator])

        FormSet = formset_factory(F, extra=0)
        formset = FormSet(
            management_data(total="", initial="")
        )
        self.assertIs(await formset.ais_valid(), False)
        self.assertEqual(calls, 0)

    async def test_total_present_initial_missing_matches_sync(self):
        data = {"form-TOTAL_FORMS": "1", "form-0-name": "x"}
        sync_formset = NameFormSet(data)
        sync_formset.is_valid()
        async_formset = NameFormSet(data)
        self.assertIs(await async_formset.ais_valid(), False)
        self.assertEqual(
            non_form_messages(async_formset), non_form_messages(sync_formset)
        )
        # The management form's defaults still drive the same number of
        # child forms and error entries.
        self.assertEqual(len(async_formset.forms), len(sync_formset.forms))
        self.assertEqual(
            form_errors_as_json(async_formset), form_errors_as_json(sync_formset)
        )


class DeletionOrderingTests(SimpleTestCase):
    async def test_deleted_invalid_form_is_valid(self):
        FormSet = formset_factory(NameForm, extra=0, can_delete=True)
        data = formset_data(
            [""],
            total=1,
            initial=1,
            **{"form-0-DELETE": "on"},
        )
        sync_formset = FormSet(data)
        sync_formset.is_valid()
        async_formset = FormSet(data)
        self.assertIs(await async_formset.ais_valid(), True)
        self.assertEqual(
            form_errors_as_json(async_formset), form_errors_as_json(sync_formset)
        )
        self.assertEqual(len(async_formset.deleted_forms), 1)
        self.assertEqual(async_formset.deleted_forms[0].cleaned_data["DELETE"], True)
        # Deleted forms are absent from the errors list.
        self.assertEqual(async_formset.errors, [])

    async def test_undeleted_invalid_form_still_fails(self):
        FormSet = formset_factory(NameForm, extra=0, can_delete=True)
        data = formset_data([""], total=1, initial=1)
        formset = FormSet(data)
        self.assertIs(await formset.ais_valid(), False)
        self.assertIn("name", formset.errors[0])
        self.assertEqual(formset.deleted_forms, [])

    async def test_ordering_matches_sync(self):
        FormSet = formset_factory(NameForm, extra=0, can_order=True)
        data = management_data(total=2)
        data.update(
            {
                "form-0-name": "b",
                "form-0-ORDER": "2",
                "form-1-name": "a",
                "form-1-ORDER": "1",
            }
        )
        sync_formset = FormSet(data)
        sync_formset.is_valid()
        async_formset = FormSet(data)
        self.assertIs(await async_formset.ais_valid(), True)
        self.assertEqual(
            [form.cleaned_data["name"] for form in async_formset.ordered_forms],
            ["a", "b"],
        )
        self.assertEqual(
            [
                form.cleaned_data["name"]
                for form in sync_formset.ordered_forms
            ],
            ["a", "b"],
        )
        self.assertEqual(
            form_errors_as_json(async_formset), form_errors_as_json(sync_formset)
        )

    async def test_toggling_deletion_revalidates(self):
        FormSet = formset_factory(NameForm, extra=0, can_delete=True)
        base = formset_data([""], total=1, initial=1)
        formset = FormSet(dict(base))
        self.assertIs(await formset.ais_valid(), False)
        # Deleting the previously failing form changes the inputs and must
        # produce a fresh result.
        formset.data = dict(base, **{"form-0-DELETE": "on"})
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(len(formset.deleted_forms), 1)


class MinMaxTests(SimpleTestCase):
    async def test_too_few_matches_sync(self):
        FormSet = formset_factory(NameForm, extra=0, min_num=2, validate_min=True)
        data = formset_data(["a"])
        sync_formset = FormSet(data)
        sync_formset.is_valid()
        async_formset = FormSet(data)
        self.assertIs(await async_formset.ais_valid(), False)
        self.assertEqual(
            non_form_messages(async_formset), non_form_messages(sync_formset)
        )

    async def test_too_many_matches_sync(self):
        FormSet = formset_factory(
            NameForm, extra=0, max_num=1, validate_max=True
        )
        data = formset_data(["a", "b"])
        sync_formset = FormSet(data)
        sync_formset.is_valid()
        async_formset = FormSet(data)
        self.assertIs(await async_formset.ais_valid(), False)
        self.assertEqual(
            non_form_messages(async_formset), non_form_messages(sync_formset)
        )

    async def test_above_absolute_max_matches_sync(self):
        FormSet = formset_factory(
            NameForm, extra=0, max_num=1, absolute_max=1
        )
        data = formset_data(["a", "b"])
        sync_formset = FormSet(data)
        sync_formset.is_valid()
        async_formset = FormSet(data)
        self.assertIs(await async_formset.ais_valid(), False)
        self.assertEqual(
            non_form_messages(async_formset), non_form_messages(sync_formset)
        )


class AsyncConcurrencyTests(SimpleTestCase):
    def _make_form(self, *, gate=None, started=None, counter=None):
        gate_holder = gate if isinstance(gate, dict) else {"*": gate}

        class F(Form):
            name = CharField()

            async def clean_name(self):
                value = self.cleaned_data["name"]
                if counter is not None:
                    counter[0] += 1
                selected = gate_holder.get(value, gate_holder.get("*"))
                if selected is not None:
                    if started is not None:
                        started.set()
                    await selected.wait()
                return value

        return F

    async def test_concurrent_calls_share_one_round(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        counter = [0]
        form_calls = [0]

        class F(Form):
            name = CharField()

            async def clean_name(self):
                value = self.cleaned_data["name"]
                form_calls[0] += 1
                started.set()
                await gate.wait()
                return value

        class CountingFormSet(AsyncBaseFormSet):
            form = F
            calls = 0

            def clean(self):
                type(self).calls += 1

        formset = CountingFormSet(formset_data(["a", "b"]))
        tasks = [asyncio.create_task(formset.ais_valid()) for _ in range(3)]
        await started.wait()
        await asyncio.sleep(0)
        # Mid-flight reads never expose a half-finished round.
        self.assertEqual(formset.errors, [])
        self.assertEqual(formset.cleaned_data, [])
        self.assertEqual(non_form_messages(formset), [])
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(results, [True, True, True])
        # Each child validator ran once per child form; the formset clean
        # hook ran once for the shared round.
        self.assertEqual(form_calls[0], 2)
        self.assertEqual(CountingFormSet.calls, 1)

    async def test_concurrent_validation_error_shared(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        class F(Form):
            name = CharField()

            async def clean_name(self):
                value = self.cleaned_data["name"]
                started.set()
                await gate.wait()
                if value == "bad":
                    raise ValidationError("shared-bad")
                return value

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["good", "bad"]))
        tasks = [asyncio.create_task(formset.ais_valid()) for _ in range(3)]
        await started.wait()
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(results, [False, False, False])
        self.assertEqual(len(formset.errors), 2)
        self.assertIn("shared-bad", str(formset.errors[1]["name"]))

    async def test_concurrent_ordinary_exception_propagates(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        class F(Form):
            name = CharField()

            async def clean_name(self):
                started.set()
                await gate.wait()
                raise RuntimeError("boom-shared")

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["x"]))
        tasks = [asyncio.create_task(formset.ais_valid()) for _ in range(2)]
        await started.wait()
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(r, RuntimeError) for r in results))
        self.assertTrue(all("boom-shared" in str(r) for r in results))
        # Ordinary exceptions are never cached or published.
        self.assertIsNone(formset._errors)
        self.assertIsNone(formset._non_form_errors)

    async def test_ordinary_exception_in_formset_clean_propagates(self):
        class F(Form):
            name = CharField()

        class BoomFormSet(AsyncBaseFormSet):
            form = F
            calls = 0

            def clean(self):
                type(self).calls += 1
                raise RuntimeError("clean-boom")

        formset = BoomFormSet(formset_data(["x"]))
        with self.assertRaises(RuntimeError):
            await formset.ais_valid()
        self.assertEqual(BoomFormSet.calls, 1)
        self.assertIsNone(formset._errors)
        # Not cached: a retry runs the whole pipeline again.
        with self.assertRaises(RuntimeError):
            await formset.ais_valid()
        self.assertEqual(BoomFormSet.calls, 2)

    async def test_cancel_one_waiter_round_continues(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        F = self._make_form(gate=gate, started=started)

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["x"]))
        keep = asyncio.create_task(formset.ais_valid())
        cancels = asyncio.create_task(formset.ais_valid())
        await started.wait()
        cancels.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancels
        gate.set()
        self.assertIs(await keep, True)

    async def test_all_waiters_cancel_then_retry_reruns(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        counter = [0]
        F = self._make_form(gate=gate, started=started, counter=counter)

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["x"]))
        only = asyncio.create_task(formset.ais_valid())
        await started.wait()
        only.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await only
        await asyncio.sleep(0.05)
        # The abandoned round left no published state or cached failure.
        self.assertIsNone(formset._errors)
        gate.set()
        await asyncio.sleep(0.05)
        calls = counter[0]
        # A subsequent call performs a complete retry.
        self.assertIs(await formset.ais_valid(), True)
        self.assertGreater(counter[0], calls)

    async def test_surviving_waiter_gets_error_after_other_cancels(self):
        gate = asyncio.Event()
        old_started = asyncio.Event()
        F = self._make_form(
            gate={"*": gate},
            started=old_started,
        )

        class FailForm(F):
            async def clean_name(self):
                value = await super().clean_name()
                raise ValidationError("only-other")

        class FormSet(AsyncBaseFormSet):
            form = FailForm

        formset = FormSet(formset_data(["x"]))
        cancels = asyncio.create_task(formset.ais_valid())
        keep = asyncio.create_task(formset.ais_valid())
        await old_started.wait()
        cancels.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancels
        gate.set()
        self.assertIs(await keep, False)
        self.assertIn("only-other", str(formset.errors[0]["name"]))

    async def test_repeated_calls_share_completed_round(self):
        counter = [0]

        class F(Form):
            name = CharField()

            async def clean_name(self):
                counter[0] += 1
                await asyncio.sleep(0)
                return self.cleaned_data["name"]

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["a", "b"]))
        for _ in range(3):
            self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(counter[0], 2)

    async def test_late_joiner_shares_result_without_new_inputs(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        counter = [0]
        F = self._make_form(gate=gate, started=started, counter=counter)

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["x"]))
        first = asyncio.create_task(formset.ais_valid())
        await started.wait()
        # Rebinding the same logical inputs must not start a new round.
        formset.data = formset_data(["x"])
        second = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0)
        gate.set()
        self.assertEqual(await asyncio.gather(first, second), [True, True])
        self.assertEqual(counter[0], 1)


class AsyncCacheInvalidationTests(SimpleTestCase):
    async def test_data_mutated_in_place_invalidates_cache(self):
        formset = NameFormSet(formset_data(["John"]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "John")

        formset.data["form-0-name"] = "Ringo"
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "Ringo")

    async def test_initial_mutated_in_place_invalidates_cache(self):
        initial = [{"name": "one"}]

        class F(Form):
            name = CharField(disabled=True)

        FormSet = formset_factory(F, extra=1)
        formset = FormSet(formset_data(["posted"], initial=1), initial=initial)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "one")

        initial[0]["name"] = "two"
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data[0]["name"], "two")

    async def test_same_error_new_inputs_are_revalidated(self):
        seen = []

        async def recording_validator(value):
            await asyncio.sleep(0)
            seen.append(value)
            raise ValidationError("always-bad")

        class F(Form):
            name = CharField(validators=[recording_validator])

        FormSet = formset_factory(F, extra=0)
        formset = FormSet(formset_data(["a"]))
        self.assertIs(await formset.ais_valid(), False)
        formset.data = formset_data(["b"])
        self.assertIs(await formset.ais_valid(), False)
        # The fresh inputs were cleaned even though the resulting error text
        # is identical.
        self.assertEqual(seen, ["a", "b"])

    async def test_form_count_change_invalidates_cache(self):
        formset = NameFormSet(formset_data(["a", "b"]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(len(formset.forms), 2)

        formset.data = formset_data(["a", "b", "c"], total=3)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(len(formset.forms), 3)
        self.assertEqual(
            formset.cleaned_data,
            [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        )

    async def test_field_definition_change_invalidates_cache(self):
        counter = [0]

        async def counting_validator(value):
            counter[0] += 1
            await asyncio.sleep(0)

        formset = NameFormSet(formset_data(["x"]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(counter[0], 0)

        NameForm.base_fields["name"].validators.append(counting_validator)
        try:
            self.assertIs(await formset.ais_valid(), True)
            self.assertEqual(counter[0], 1)
        finally:
            NameForm.base_fields["name"].validators.remove(counting_validator)

    async def test_running_round_uses_round_inputs(self):
        gates = {"old": asyncio.Event(), "new": asyncio.Event()}
        started = asyncio.Event()

        class F(Form):
            name = CharField()

            async def clean_name(self):
                value = self.cleaned_data["name"]
                started.set()
                await gates[value].wait()
                return value

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["old"]))
        task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        # Inputs rebound while the round is in flight cannot change it.
        formset.data = formset_data(["new"])
        gates["old"].set()
        self.assertIs(await task, True)
        self.assertEqual(formset.cleaned_data, [{"name": "old"}])
        # The next call validates against the current inputs.
        new_task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        gates["new"].set()
        self.assertIs(await new_task, True)
        self.assertEqual(formset.cleaned_data, [{"name": "new"}])

    async def test_changed_inputs_supersede_running_round(self):
        old_gate = asyncio.Event()
        new_gate = asyncio.Event()
        old_started = asyncio.Event()
        new_started = asyncio.Event()
        seen = []

        class F(Form):
            name = CharField()

            async def clean_name(self):
                value = self.cleaned_data["name"]
                seen.append(value)
                if value == "old":
                    old_started.set()
                    await old_gate.wait()
                else:
                    new_started.set()
                    await new_gate.wait()
                return value

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["old"]))
        old_task = asyncio.create_task(formset.ais_valid())
        await old_started.wait()
        self.assertEqual(seen, ["old"])

        formset.data = formset_data(["new"])
        new_task = asyncio.create_task(formset.ais_valid())
        await new_started.wait()
        # The new round already started cleaning the new snapshot even
        # though the old round has not unblocked.
        self.assertEqual(seen, ["old", "new"])
        new_gate.set()
        self.assertIs(await new_task, True)
        self.assertEqual(formset.cleaned_data, [{"name": "new"}])
        # The stale round's waiter still gets its own snapshot's result.
        old_gate.set()
        self.assertIs(await old_task, True)
        # The published result corresponds to the newest completed round.
        self.assertEqual(formset.cleaned_data, [{"name": "new"}])

    async def test_stale_failure_does_not_delete_newer_result(self):
        old_gate = asyncio.Event()
        new_gate = asyncio.Event()
        old_started = asyncio.Event()
        new_started = asyncio.Event()

        class F(Form):
            name = CharField()

            async def clean_name(self):
                value = self.cleaned_data["name"]
                if value == "old":
                    old_started.set()
                    await old_gate.wait()
                    raise ValidationError("old-bad")
                new_started.set()
                await new_gate.wait()
                return value

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["old"]))
        old_task = asyncio.create_task(formset.ais_valid())
        await old_started.wait()
        formset.data = formset_data(["new"])
        new_task = asyncio.create_task(formset.ais_valid())
        await new_started.wait()
        new_gate.set()
        self.assertIs(await new_task, True)
        self.assertEqual(formset.cleaned_data, [{"name": "new"}])
        old_gate.set()
        self.assertIs(await old_task, False)
        # A stale failure must never delete the newer successful result.
        self.assertEqual(formset.cleaned_data, [{"name": "new"}])
        self.assertEqual(formset.errors, [{}])

    async def test_previous_result_stays_visible_during_round(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        phase = {"block": False}

        class F(Form):
            name = CharField()

            async def clean_name(self):
                value = self.cleaned_data["name"]
                if phase["block"]:
                    started.set()
                    await gate.wait()
                return value

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["done"]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data, [{"name": "done"}])

        phase["block"] = True
        formset.data = formset_data(["in-flight"])
        task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        # While the new round is unfinished, external reads keep observing
        # the last complete result instead of a half-finished one.
        self.assertEqual(formset.cleaned_data, [{"name": "done"}])
        self.assertEqual(formset.errors, [{}])
        self.assertIs(formset.is_valid(), True)
        gate.set()
        self.assertIs(await task, True)
        self.assertEqual(formset.cleaned_data, [{"name": "in-flight"}])

    async def test_running_round_uses_round_field_definitions(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        counted = [0]

        async def gated_validator(value):
            started.set()
            await gate.wait()

        async def counting_validator(value):
            counted[0] += 1

        class F(Form):
            name = CharField()

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["x"]))
        # A first round without any appended validators completes normally.
        self.assertIs(await formset.ais_valid(), True)
        # The gated validator is appended for the next round.
        F.base_fields["name"].validators.append(gated_validator)
        task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        # A validator appended while the round is running must not
        # participate in that round.
        F.base_fields["name"].validators.append(counting_validator)
        gate.set()
        self.assertIs(await task, True)
        self.assertEqual(counted[0], 0)
        # It takes effect on the next round.
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(counted[0], 1)
        F.base_fields["name"].validators.remove(gated_validator)
        F.base_fields["name"].validators.remove(counting_validator)


class AsyncCancellationStateTests(SimpleTestCase):
    async def test_cancel_restores_last_completed_result(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        phase = {"block": False}

        class F(Form):
            name = CharField()

            async def clean_name(self):
                value = self.cleaned_data["name"]
                if phase["block"]:
                    started.set()
                    await gate.wait()
                return value

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["done"]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data, [{"name": "done"}])

        phase["block"] = True
        formset.data = formset_data(["blocked"])
        only = asyncio.create_task(formset.ais_valid())
        await started.wait()
        only.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await only
        # The earlier successful result stays observable.
        self.assertEqual(formset.cleaned_data, [{"name": "done"}])
        gate.set()
        await asyncio.sleep(0.05)
        # The next call retries fully against the current inputs.
        phase["block"] = False
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data, [{"name": "blocked"}])


class AsyncSyncInterleaveTests(SimpleTestCase):
    def _make_formset(self, *, fail=False):
        started = asyncio.Event()
        gate = asyncio.Event()

        class F(Form):
            block = True
            name = CharField()

            def clean_name(self):
                value = self.cleaned_data["name"]
                if F.block:

                    async def wait():
                        started.set()
                        await gate.wait()
                        if fail:
                            raise ValidationError("async-old-bad")

                    return wait()
                return value

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["old"]))
        return formset, F, started, gate

    async def test_async_reuses_completed_sync_validation(self):
        counter = [0]

        def counting(value):
            counter[0] += 1

        class F(Form):
            name = CharField(validators=[counting])

        class FormSet(AsyncBaseFormSet):
            form = F

        formset = FormSet(formset_data(["x"]))
        self.assertIs(formset.is_valid(), True)
        self.assertEqual(counter[0], 1)
        # Same snapshot: the async call reuses the synchronous result and no
        # child form validator runs again.
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(counter[0], 1)

    async def test_sync_reuses_completed_async_validation(self):
        formset = NameFormSet(formset_data(["x"]))
        self.assertIs(await formset.ais_valid(), True)
        self.assertIs(formset.is_valid(), True)
        self.assertEqual(formset.cleaned_data, [{"name": "x"}])

    async def test_changed_inputs_rerun_after_sync(self):
        formset = NameFormSet(formset_data(["old"]))
        self.assertIs(formset.is_valid(), True)
        formset.data = formset_data(["new"])
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data, [{"name": "new"}])

    async def test_late_async_result_does_not_overwrite_sync_result(self):
        formset, F, started, gate = self._make_formset()
        task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        # The inputs change and a synchronous validation completes while the
        # async round is blocked inside its child hook.
        formset.data = formset_data(["new"])
        F.block = False
        formset.full_clean()
        self.assertEqual(formset.cleaned_data, [{"name": "new"}])

        gate.set()
        self.assertIs(await task, True)
        # The late async completion served its own snapshot to its waiter but
        # must never publish over the newer synchronous result.
        self.assertEqual(formset.cleaned_data, [{"name": "new"}])
        self.assertEqual(formset.errors, [{}])
        # The synchronous result is reusable without another round.
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data, [{"name": "new"}])

    async def test_late_async_failure_does_not_delete_sync_errors(self):
        formset, F, started, gate = self._make_formset(fail=True)
        task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        # New inputs fail synchronously: the form is an initial form, so its
        # empty required value raises a required error.
        formset.data = management_data(total=1, initial=1)
        formset.data["form-0-name"] = ""
        F.block = False
        formset.full_clean()
        sync_errors = form_errors_as_json(formset)
        self.assertIn("required", sync_errors[0])

        gate.set()
        self.assertIs(await task, False)
        # The stale async failure must not replace the synchronous errors.
        self.assertEqual(form_errors_as_json(formset), sync_errors)

    async def test_cancelling_stale_waiter_keeps_sync_result(self):
        formset, F, started, gate = self._make_formset()
        task = asyncio.create_task(formset.ais_valid())
        await started.wait()
        # A synchronous pass over the same inputs becomes the newest result.
        F.block = False
        formset.full_clean()
        self.assertEqual(formset.cleaned_data, [{"name": "old"}])
        # Abandoning the stale waiter must not discard the synchronous result
        # or its cached fingerprint.
        task.cancel()
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data, [{"name": "old"}])


class AsyncResultReuseTests(SimpleTestCase):
    async def test_failure_result_reused_without_rerun(self):
        counter = [0]

        async def counting(value):
            counter[0] += 1
            await asyncio.sleep(0)
            raise ValidationError("always-bad")

        class F(Form):
            name = CharField(validators=[counting])

        FormSet = formset_factory(F, extra=0)
        formset = FormSet(formset_data(["x"]))
        self.assertIs(await formset.ais_valid(), False)
        self.assertIs(await formset.ais_valid(), False)
        # The failure is shared from the completed round; the validator ran
        # once.
        self.assertEqual(counter[0], 1)
        self.assertIn("always-bad", str(formset.errors[0]["name"]))

    async def test_unbound_repeated_call_does_not_build_forms(self):
        built = [0]

        class BuildCountingFormSet(NameFormSet):
            def _build_forms(self):
                built[0] += 1
                return super()._build_forms()

        formset = BuildCountingFormSet(initial=[{"name": "John"}])
        self.assertIs(await formset.ais_valid(), False)
        self.assertIs(await formset.ais_valid(), False)
        # Unbound rounds construct no child forms, and the second call
        # reuses the completed first round.
        self.assertEqual(built[0], 0)

    async def test_round_uses_initial_snapshot(self):
        observed = []

        class F(Form):
            name = CharField(disabled=True)

            def clean_name(self):
                observed.append(self.initial.get("name"))
                return self.cleaned_data["name"]

        initial = [{"name": "one"}]
        FormSet = formset_factory(F, extra=1)
        formset = FormSet(formset_data(["posted"], initial=1), initial=initial)
        task = asyncio.create_task(formset.ais_valid())
        await asyncio.sleep(0)
        # Mutating the initial list while the round is in flight cannot
        # change the round's snapshot.
        initial[0] = {"name": "two"}
        self.assertIs(await task, True)
        self.assertEqual(observed, ["one"])
        self.assertEqual(formset.cleaned_data, [{"name": "one"}])
        # The next call uses the new initial.
        self.assertIs(await formset.ais_valid(), True)
        self.assertEqual(formset.cleaned_data, [{"name": "two"}])

    async def test_formset_clean_sees_only_complete_cleaned_data(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        seen = []

        class GatedFormSet(AsyncBaseFormSet):
            form = NameForm

            async def clean(self):
                entered.set()
                await release.wait()
                # No partially populated child data: every child has finished
                # before the formset-wide hook is awaited.
                seen.append([dict(d) for d in self.cleaned_data])

        formset = GatedFormSet(formset_data(["a", "b"]))
        task = asyncio.create_task(formset.ais_valid())
        await entered.wait()
        # The hook is suspended before the children's data is read; once it
        # resumes both children have completed and their data is complete.
        self.assertEqual(seen, [])
        release.set()
        self.assertIs(await task, True)
        self.assertEqual(
            seen,
            [[{"name": "a"}, {"name": "b"}]],
        )
