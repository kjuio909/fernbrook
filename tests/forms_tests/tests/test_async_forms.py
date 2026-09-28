import asyncio

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.forms import (
    BooleanField,
    CharField,
    ChoiceField,
    ComboField,
    EmailField,
    FileField,
    Form,
    SplitDateTimeField,
)
from django.test import SimpleTestCase


def async_validator(message=None):
    async def validator(value):
        await asyncio.sleep(0)
        if message is not None:
            raise ValidationError(message)

    return validator


def sync_validator(message=None):
    def validator(value):
        if message is not None:
            raise ValidationError(message)

    return validator


class SimplePersonForm(Form):
    first_name = CharField()
    last_name = CharField()


class AsyncIsValidTests(SimpleTestCase):
    async def test_all_sync_valid(self):
        form = SimplePersonForm({"first_name": "John", "last_name": "Lennon"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(
            form.cleaned_data,
            {"first_name": "John", "last_name": "Lennon"},
        )

    async def test_all_sync_invalid_matches_sync(self):
        data = {"first_name": "John"}
        sync_form = SimplePersonForm(data)
        self.assertIs(sync_form.is_valid(), False)
        async_form = SimplePersonForm(data)
        self.assertIs(await async_form.ais_valid(), False)
        self.assertEqual(
            async_form.errors.as_json(), sync_form.errors.as_json()
        )

    async def test_unbound_is_invalid(self):
        form = SimplePersonForm()
        self.assertIs(await form.ais_valid(), False)
        self.assertEqual(dict(form.errors), {})

    async def test_async_validator_awaited(self):
        form = SimplePersonForm({"first_name": "John", "last_name": "Lennon"})
        form.fields["first_name"].validators.append(async_validator())
        self.assertIs(await form.ais_valid(), True)

    async def test_async_validator_error_attribution(self):
        form = SimplePersonForm({"first_name": "John", "last_name": "Lennon"})
        form.fields["last_name"].validators.append(
            async_validator("async bad last name")
        )
        self.assertIs(await form.ais_valid(), False)
        self.assertIn("last_name", form.errors)
        self.assertIn("async bad last name", str(form.errors["last_name"]))
        self.assertNotIn("last_name", form.cleaned_data)

    async def test_mixed_sync_async_validator_order(self):
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
            value = CharField(
                validators=[
                    make_sync("1"),
                    make_async("2"),
                    make_sync("3"),
                ]
            )

        form = F({"value": "x"})
        await form.ais_valid()
        self.assertEqual(
            order,
            [("sync", "1"), ("async", "2"), ("sync", "3")],
        )

    async def test_mixed_validator_errors_aggregated_once(self):
        def sync_fail(value):
            raise ValidationError("alpha")

        class F(Form):
            value = CharField(validators=[sync_fail, async_validator("beta")])

        form = F({"value": "x"})
        self.assertIs(await form.ais_valid(), False)
        messages = str(form.errors["value"])
        self.assertEqual(messages.count("alpha"), 1)
        self.assertEqual(messages.count("beta"), 1)

    async def test_async_clean_field_hook(self):
        class F(Form):
            name = CharField()

            async def clean_name(self):
                await asyncio.sleep(0)
                return self.cleaned_data["name"].upper()

        form = F({"name": "john"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "JOHN")

    async def test_async_clean_field_error(self):
        class F(Form):
            name = CharField()

            async def clean_name(self):
                await asyncio.sleep(0)
                raise ValidationError("hook-bad")

        form = F({"name": "john"})
        self.assertIs(await form.ais_valid(), False)
        self.assertIn("hook-bad", str(form.errors["name"]))
        self.assertNotIn("name", form.cleaned_data)

    async def test_field_cleaning_in_declaration_order(self):
        seen = []
        first_seen_by_second = []

        class F(Form):
            first = CharField()
            second = CharField()

            async def clean_first(self):
                seen.append("first-start")
                await asyncio.sleep(0)
                seen.append("first-end")
                return self.cleaned_data["first"]

            async def clean_second(self):
                seen.append("second-start")
                # self here is the form: the first field must already have
                # finished when the second field is cleaned.
                first_seen_by_second.append("first" in self.cleaned_data)
                await asyncio.sleep(0)
                seen.append("second-end")
                return self.cleaned_data["second"]

        form = F({"first": "1", "second": "2"})
        await form.ais_valid()
        self.assertEqual(
            seen,
            ["first-start", "first-end", "second-start", "second-end"],
        )
        self.assertEqual(first_seen_by_second, [True])

    async def test_async_form_clean_nonfield_error(self):
        class F(Form):
            name = CharField()

            async def clean(self):
                await asyncio.sleep(0)
                raise ValidationError("form-wide")

        form = F({"name": "x"})
        self.assertIs(await form.ais_valid(), False)
        self.assertIn("form-wide", str(form.non_field_errors()))

    async def test_field_and_nonfield_errors_keep_attribution_and_order(self):
        def fail(message):
            def validator(value):
                raise ValidationError(message)

            return validator

        class F(Form):
            first = CharField()
            second = CharField()

            async def clean(self):
                await asyncio.sleep(0)
                raise ValidationError("form-wide")

        class SyncF(Form):
            first = CharField()
            second = CharField()

            def clean(self):
                raise ValidationError("form-wide")

        form = F({"first": "a", "second": "b"})
        form.fields["first"].validators.append(fail("first-bad"))
        form.fields["second"].validators.append(fail("second-bad"))
        self.assertIs(await form.ais_valid(), False)
        # Field errors stay attributed to their fields in declaration order
        # and the form-wide error stays a non-field error.
        self.assertEqual(
            list(form.errors.keys()), ["first", "second", NON_FIELD_ERRORS]
        )
        self.assertIn("first-bad", str(form.errors["first"]))
        self.assertIn("second-bad", str(form.errors["second"]))
        self.assertIn("form-wide", str(form.errors[NON_FIELD_ERRORS]))
        self.assertNotIn("first", form.cleaned_data)
        self.assertNotIn("second", form.cleaned_data)
        # Parity with the synchronous path.
        sync_form = SyncF({"first": "a", "second": "b"})
        sync_form.fields["first"].validators.append(fail("first-bad"))
        sync_form.fields["second"].validators.append(fail("second-bad"))
        sync_form.is_valid()
        self.assertEqual(form.errors.as_json(), sync_form.errors.as_json())

    async def test_async_form_clean_replaces_cleaned_data(self):
        class F(Form):
            name = CharField()

            async def clean(self):
                await asyncio.sleep(0)
                return {"name": "replaced"}

        form = F({"name": "x"})
        await form.ais_valid()
        self.assertEqual(form.cleaned_data, {"name": "replaced"})

    async def test_repeated_calls_run_validators_once(self):
        class F(Form):
            name = CharField()

            def __init__(self, *args, **kwargs):
                self.calls = 0
                super().__init__(*args, **kwargs)

            async def count_validator(self, value):
                self.calls += 1
                await asyncio.sleep(0)

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.count_validator)
        await form.ais_valid()
        await form.ais_valid()
        self.assertEqual(form.calls, 1)

    async def test_async_reuses_completed_sync_validation(self):
        class F(Form):
            name = CharField()

            def __init__(self, *args, **kwargs):
                self.sync_calls = 0
                super().__init__(*args, **kwargs)

            def counting_validator(self, value):
                self.sync_calls += 1

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.counting_validator)
        self.assertIs(form.is_valid(), True)
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.sync_calls, 1)

    async def test_sync_reuses_completed_async_validation(self):
        class F(Form):
            name = CharField()

            def __init__(self, *args, **kwargs):
                self.calls = 0
                super().__init__(*args, **kwargs)

            async def counting_validator(self, value):
                self.calls += 1
                await asyncio.sleep(0)

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.counting_validator)
        self.assertIs(await form.ais_valid(), True)
        self.assertIs(form.is_valid(), True)
        self.assertEqual(form.calls, 1)

    async def test_changed_inputs_trigger_new_round(self):
        class F(Form):
            name = CharField()

            def __init__(self, *args, **kwargs):
                self.calls = 0
                super().__init__(*args, **kwargs)

            async def counting_validator(self, value):
                self.calls += 1
                await asyncio.sleep(0)

        form = F({"name": "a"})
        form.fields["name"].validators.append(form.counting_validator)
        await form.ais_valid()
        self.assertEqual(form.calls, 1)

        # Same inputs: cached, no re-run.
        await form.ais_valid()
        self.assertEqual(form.calls, 1)

        # Replacing the bound data (new object) invalidates the cache and
        # causes a complete fresh round against the new inputs.
        form.data = {"name": "b"}
        await form.ais_valid()
        self.assertEqual(form.calls, 2)
        self.assertEqual(form.cleaned_data["name"], "b")

    async def test_empty_permitted_short_circuit(self):
        form = SimplePersonForm({}, empty_permitted=True, use_required_attribute=False)
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(dict(form.errors), {})


class AsyncFieldTypesTests(SimpleTestCase):
    async def test_combo_field(self):
        class F(Form):
            value = ComboField(fields=[CharField(max_length=10), EmailField()])

        valid = F({"value": "a@b.co"})
        self.assertIs(await valid.ais_valid(), True)
        invalid = F({"value": "not-an-email"})
        self.assertIs(await invalid.ais_valid(), False)
        sync_invalid = F({"value": "not-an-email"})
        sync_invalid.is_valid()
        self.assertEqual(invalid.errors.as_json(), sync_invalid.errors.as_json())

    async def test_split_datetime_field_parity(self):
        data = {"dt_0": "2026-01-02", "dt_1": "03:30:45"}

        class F(Form):
            dt = SplitDateTimeField()

        async_form = F(data)
        sync_form = F(data)
        self.assertIs(await async_form.ais_valid(), True)
        sync_form.is_valid()
        self.assertEqual(async_form.cleaned_data, sync_form.cleaned_data)

    async def test_file_field_parity(self):
        class F(Form):
            upload = FileField(required=False)

        uploaded = SimpleUploadedFile("x.txt", b"hello", content_type="text/plain")
        async_form = F({}, {"upload": uploaded})
        sync_form = F({}, {"upload": uploaded})
        self.assertIs(await async_form.ais_valid(), True)
        sync_form.is_valid()
        self.assertEqual(async_form.cleaned_data, sync_form.cleaned_data)

    async def test_disabled_field_uses_initial(self):
        class F(Form):
            name = CharField(disabled=True)

        async_form = F({"name": "posted"}, initial={"name": "initial"})
        sync_form = F({"name": "posted"}, initial={"name": "initial"})
        self.assertIs(await async_form.ais_valid(), True)
        sync_form.is_valid()
        self.assertEqual(async_form.cleaned_data, sync_form.cleaned_data)
        self.assertEqual(async_form.cleaned_data["name"], "initial")


class AsyncConcurrencyTests(SimpleTestCase):
    async def test_concurrent_calls_share_one_round(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            def __init__(self, *args, **kwargs):
                self.calls = 0
                super().__init__(*args, **kwargs)

            async def gated_validator(self, value):
                self.calls += 1
                await gate.wait()

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.gated_validator)

        tasks = [asyncio.create_task(form.ais_valid()) for _ in range(3)]
        await asyncio.sleep(0)
        # Mid-flight reads from other tasks see no half-finished state.
        self.assertEqual(dict(form.errors), {})
        self.assertEqual(form.cleaned_data, {})
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(results, [True, True, True])
        self.assertEqual(form.calls, 1)

    async def test_concurrent_validation_error_shared(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            async def gated_validator(self, value):
                await gate.wait()
                raise ValidationError("shared-bad")

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.gated_validator)
        tasks = [asyncio.create_task(form.ais_valid()) for _ in range(3)]
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(results, [False, False, False])
        self.assertEqual(len(form.errors.as_data()["name"]), 1)
        self.assertIn("shared-bad", str(form.errors["name"]))

    async def test_concurrent_ordinary_exception_propagates(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            async def boom(self, value):
                await gate.wait()
                raise RuntimeError("boom-shared")

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.boom)
        tasks = [asyncio.create_task(form.ais_valid()) for _ in range(2)]
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(r, RuntimeError) for r in results))
        self.assertTrue(all("boom-shared" in str(r) for r in results))
        self.assertIsNone(form._errors)

    async def test_cancel_one_waiter_round_continues(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            async def gated_validator(self, value):
                await gate.wait()

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.gated_validator)
        keep = asyncio.create_task(form.ais_valid())
        cancels = asyncio.create_task(form.ais_valid())
        await asyncio.sleep(0)
        cancels.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancels
        gate.set()
        self.assertIs(await keep, True)

    async def test_all_waiters_cancel_then_retry_reruns(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            def __init__(self, *args, **kwargs):
                self.calls = 0
                super().__init__(*args, **kwargs)

            async def gated_validator(self, value):
                self.calls += 1
                await gate.wait()

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.gated_validator)
        only = asyncio.create_task(form.ais_valid())
        await asyncio.sleep(0)
        only.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await only
        await asyncio.sleep(0.05)
        # The abandoned round left no errors or cached failure.
        self.assertIsNone(form._errors)
        gate.set()
        await asyncio.sleep(0.05)
        # A subsequent call performs a complete retry.
        calls = form.calls
        self.assertIs(await form.ais_valid(), True)
        self.assertGreater(form.calls, calls)

    async def test_surviving_waiter_gets_error_after_other_cancels(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            async def gated_fail(self, value):
                await gate.wait()
                raise ValidationError("only-other")

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.gated_fail)
        cancels = asyncio.create_task(form.ais_valid())
        keep = asyncio.create_task(form.ais_valid())
        await asyncio.sleep(0)
        cancels.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancels
        gate.set()
        self.assertIs(await keep, False)
        self.assertIn("only-other", str(form.errors["name"]))

    async def test_ordinary_exception_not_cached(self):
        class F(Form):
            name = CharField()

            calls = 0

            def boom(self, value):
                type(self).calls += 1
                raise RuntimeError("kaboom")

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.boom)
        with self.assertRaises(RuntimeError):
            await form.ais_valid()
        with self.assertRaises(RuntimeError):
            await form.ais_valid()
        self.assertEqual(F.calls, 2)


class AsyncChoiceFieldTests(SimpleTestCase):
    async def test_choice_field_async_valid(self):
        class F(Form):
            pick = ChoiceField(choices=[("a", "A"), ("b", "B")])

        form = F({"pick": "a"})
        self.assertIs(await form.ais_valid(), True)

        invalid = F({"pick": "z"})
        self.assertIs(await invalid.ais_valid(), False)
        self.assertIn("pick", invalid.errors)


class AsyncBooleanFieldTests(SimpleTestCase):
    async def test_boolean_field_required(self):
        class F(Form):
            agreed = BooleanField()

        form = F({})
        self.assertIs(await form.ais_valid(), False)
        self.assertIn("agreed", form.errors)


class AsyncCacheInvalidationTests(SimpleTestCase):
    async def test_data_mutated_in_place_invalidates_cache(self):
        form = SimplePersonForm({"first_name": "John", "last_name": "Lennon"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["first_name"], "John")

        form.data["first_name"] = "Ringo"
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["first_name"], "Ringo")

    async def test_initial_mutated_in_place_invalidates_cache(self):
        class F(Form):
            name = CharField(disabled=True)

        initial = {"name": "one"}
        form = F({"name": "posted"}, initial=initial)
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "one")

        initial["name"] = "two"
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "two")

    async def test_same_error_new_inputs_are_revalidated(self):
        seen = []

        async def recording_validator(value):
            await asyncio.sleep(0)
            seen.append(value)
            raise ValidationError("always-bad")

        class F(Form):
            name = CharField()

        form = F({"name": "a"})
        form.fields["name"].validators.append(recording_validator)
        self.assertIs(await form.ais_valid(), False)
        form.data = {"name": "b"}
        self.assertIs(await form.ais_valid(), False)
        # The fresh inputs were cleaned even though the resulting error text
        # is identical.
        self.assertEqual(seen, ["a", "b"])

    async def test_field_definition_change_invalidates_cache(self):
        class F(Form):
            name = CharField()

            def __init__(self, *args, **kwargs):
                self.calls = 0
                super().__init__(*args, **kwargs)

            async def counting_validator(self, value):
                self.calls += 1
                await asyncio.sleep(0)

        form = F({"name": "x"})
        form.fields["name"].validators.append(form.counting_validator)
        await form.ais_valid()
        self.assertEqual(form.calls, 1)

        form.fields["extra"] = CharField(required=False)
        await form.ais_valid()
        self.assertEqual(form.calls, 2)

    async def test_validator_added_after_completion_runs(self):
        class F(Form):
            name = CharField()

            def __init__(self, *args, **kwargs):
                self.calls = 0
                super().__init__(*args, **kwargs)

            async def counting_validator(self, value):
                self.calls += 1
                await asyncio.sleep(0)

        form = F({"name": "x"})
        await form.ais_valid()
        form.fields["name"].validators.append(form.counting_validator)
        await form.ais_valid()
        self.assertEqual(form.calls, 1)

    async def test_choices_mutated_in_place_invalidates_cache(self):
        class F(Form):
            pick = ChoiceField(choices=[("a", "A")])

        form = F({"pick": "b"})
        self.assertIs(await form.ais_valid(), False)
        form.fields["pick"].choices = [("a", "A"), ("b", "B")]
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["pick"], "b")

    async def test_required_toggle_invalidates_cache(self):
        class F(Form):
            name = CharField(required=False)

        form = F({})
        self.assertIs(await form.ais_valid(), True)
        form.fields["name"].required = True
        self.assertIs(await form.ais_valid(), False)
        self.assertIn("name", form.errors)

    async def test_running_round_uses_round_inputs(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            async def gated_validator(self, value):
                await gate.wait()

        form = F({"name": "original"})
        form.fields["name"].validators.append(form.gated_validator)
        task = asyncio.create_task(form.ais_valid())
        await asyncio.sleep(0)
        # Inputs rebound while the round is in flight cannot change it.
        form.data = {"name": "rebound"}
        gate.set()
        self.assertIs(await task, True)
        self.assertEqual(form.cleaned_data["name"], "original")
        # The next call validates against the current inputs.
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "rebound")

    async def test_running_round_uses_round_field_definitions(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            def __init__(self, *args, **kwargs):
                self.counted = 0
                super().__init__(*args, **kwargs)

            async def gated_validator(self, value):
                await gate.wait()

            async def counting_validator(self, value):
                self.counted += 1

        form = F({"name": "x", "extra": "y"})
        form.fields["name"].validators.append(form.gated_validator)
        task = asyncio.create_task(form.ais_valid())
        await asyncio.sleep(0)
        # A field added and a validator appended while the round is running
        # must not participate in that round.
        form.fields["extra"] = CharField()
        form.fields["name"].validators.append(form.counting_validator)
        gate.set()
        self.assertIs(await task, True)
        self.assertEqual(set(form.cleaned_data), {"name"})
        self.assertEqual(form.counted, 0)
        # Both changes take effect on the next round.
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(set(form.cleaned_data), {"name", "extra"})
        self.assertEqual(form.counted, 1)


class AsyncPlainReturnValueTests(SimpleTestCase):
    async def test_validator_returning_plain_value(self):
        def plain_return(value):
            return 42

        class F(Form):
            name = CharField(validators=[plain_return])

        form = F({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "x")

    async def test_clean_field_hook_returning_plain_value(self):
        class F(Form):
            name = CharField()

            def clean_name(self):
                return self.cleaned_data["name"].upper() + "!"

        form = F({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "X!")

    async def test_form_clean_returning_plain_dict(self):
        class F(Form):
            name = CharField()

            def clean(self):
                return {"name": "sync-cleaned"}

        form = F({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data, {"name": "sync-cleaned"})


class AsyncCancellationStateTests(SimpleTestCase):
    async def test_mid_round_reads_keep_last_completed_result(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            async def gated_validator(self, value):
                await gate.wait()

        form = F({"name": "done"})
        form.fields["name"].validators.append(async_validator())
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "done")

        # A new round against changed inputs stays unfinished at the gate.
        form.data = {"name": "pending"}
        form.fields["name"].validators.append(form.gated_validator)
        task = asyncio.create_task(form.ais_valid())
        await asyncio.sleep(0)
        # Until the new round completes, external readers (including the
        # synchronous entry point and BoundField.errors) keep observing the
        # last completed result rather than an empty or partial one.
        self.assertEqual(form.cleaned_data["name"], "done")
        self.assertNotIn("name", form.errors)
        self.assertEqual(list(form["name"].errors), [])
        self.assertIs(form.is_valid(), True)
        gate.set()
        self.assertIs(await task, True)
        # The new complete result atomically replaces the old one.
        self.assertEqual(form.cleaned_data["name"], "pending")

    async def test_cancel_restores_last_completed_result(self):
        gate = asyncio.Event()

        class F(Form):
            name = CharField()

            async def gated_validator(self, value):
                await gate.wait()

        form = F({"name": "done"})
        form.fields["name"].validators.append(async_validator())
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "done")

        form.fields["name"].validators.append(form.gated_validator)
        only = asyncio.create_task(form.ais_valid())
        await asyncio.sleep(0)
        only.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await only
        # The earlier successful result stays observable.
        self.assertEqual(form.cleaned_data["name"], "done")
        gate.set()
        await asyncio.sleep(0.05)
        # The next call retries fully against the current inputs.
        self.assertIs(await form.ais_valid(), True)

