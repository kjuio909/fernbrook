import asyncio

from django.core.exceptions import NON_FIELD_ERRORS, AsynchronousOnlyOperation
from django.forms import CharField, Form, IntegerField, ValidationError
from django.test import SimpleTestCase


def async_validator_factory(calls, message=None, fail_value=None, delay=0):
    async def validator(value):
        calls.append(value)
        if delay:
            await asyncio.sleep(delay)
        if fail_value is not None and value == fail_value and message is not None:
            raise ValidationError(message)

    return validator


class AsyncFormValidationTests(SimpleTestCase):
    async def test_valid_form(self):
        calls = []

        class Person(Form):
            name = CharField(validators=[async_validator_factory(calls)])

        form = Person({"name": "Jacobs"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data, {"name": "Jacobs"})
        self.assertEqual(form.errors, {})
        self.assertEqual(calls, ["Jacobs"])

    async def test_field_error(self):
        calls = []

        class Person(Form):
            name = CharField(
                validators=[
                    async_validator_factory(
                        calls, message="must be good", fail_value="bad", delay=0
                    )
                ]
            )

        form = Person({"name": "bad"})
        self.assertIs(await form.ais_valid(), False)
        self.assertEqual(form.errors["name"], ["must be good"])
        self.assertNotIn("name", form.cleaned_data)
        self.assertEqual(calls, ["bad"])

    async def test_async_clean_field(self):
        class Person(Form):
            name = CharField()

            async def clean_name(self):
                await asyncio.sleep(0)
                return self.cleaned_data["name"].upper()

        form = Person({"name": "jacobs"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "JACOBS")

    async def test_async_clean_field_error_attribution(self):
        class Person(Form):
            name = CharField()

            async def clean_name(self):
                await asyncio.sleep(0)
                raise ValidationError("clean_name failed")

        form = Person({"name": "jacobs"})
        self.assertIs(await form.ais_valid(), False)
        self.assertEqual(form.errors["name"], ["clean_name failed"])

    async def test_async_form_wide_clean_error(self):
        class Person(Form):
            name = CharField()

            async def clean(self):
                await asyncio.sleep(0)
                raise ValidationError("form wide failed")

        form = Person({"name": "jacobs"})
        self.assertIs(await form.ais_valid(), False)
        self.assertEqual(form.non_field_errors(), ["form wide failed"])
        self.assertEqual(
            form.errors[NON_FIELD_ERRORS], ["form wide failed"]
        )

    async def test_field_and_non_field_errors_coexist(self):
        async def field_validator(value):
            await asyncio.sleep(0)
            raise ValidationError("field failed")

        class Person(Form):
            name = CharField(validators=[field_validator])

            async def clean(self):
                raise ValidationError("form failed")

        form = Person({"name": "jacobs"})
        self.assertIs(await form.ais_valid(), False)
        self.assertEqual(form.errors["name"], ["field failed"])
        self.assertEqual(form.non_field_errors(), ["form failed"])

    async def test_fields_cleaned_in_declaration_order(self):
        order = []

        def make_validator(label):
            async def validator(value):
                order.append(label)
                await asyncio.sleep(0)

            return validator

        class Person(Form):
            first = CharField(validators=[make_validator("first")])
            second = CharField(validators=[make_validator("second")])

        form = Person({"first": "a", "second": "b"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(
            list(form.cleaned_data), ["first", "second"]
        )

    async def test_form_clean_sees_all_completed_fields(self):
        seen = []

        class Person(Form):
            first = CharField()
            second = CharField()

            async def clean(self):
                seen.append(dict(self.cleaned_data))
                return self.cleaned_data

        form = Person({"first": "a", "second": "b"})
        await form.ais_valid()
        self.assertEqual(seen, [{"first": "a", "second": "b"}])

    async def test_sync_validators_run_on_async_path(self):
        calls = []

        def sync_validator(value):
            calls.append(value)
            if value == "bad":
                raise ValidationError("sync bad")

        class Person(Form):
            name = CharField(validators=[sync_validator])

        form = Person({"name": "bad"})
        self.assertIs(await form.ais_valid(), False)
        self.assertEqual(form.errors["name"], ["sync bad"])
        self.assertEqual(calls, ["bad"])

    async def test_mixed_sync_and_async_validators_order(self):
        order = []

        def sync_v(value):
            order.append("sync")

        async def async_v(value):
            order.append("async")
            await asyncio.sleep(0)

        class Person(Form):
            name = CharField(validators=[sync_v, async_v])

        form = Person({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(order, ["sync", "async"])

    async def test_unbound_form_is_invalid(self):
        class Person(Form):
            name = CharField()

        form = Person()
        self.assertIs(await form.ais_valid(), False)
        self.assertEqual(form.errors, {})

    async def test_plain_value_branch_not_awaited_twice(self):
        class Person(Form):
            name = CharField()

            def clean_name(self):
                return self.cleaned_data["name"] + "!"

        form = Person({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "x!")

    async def test_validation_error_from_awaitable_not_recorded_until_done(self):
        async def slow_bad(value):
            await asyncio.sleep(0.02)
            raise ValidationError("late bad")

        class Person(Form):
            name = CharField(validators=[slow_bad])

        form = Person({"name": "x"})
        task = asyncio.ensure_future(form.afull_clean())
        await asyncio.sleep(0.005)
        self.assertEqual(dict(form.errors), {})
        await task
        self.assertEqual(form.errors["name"], ["late bad"])


class AsyncFormCachingTests(SimpleTestCase):
    async def test_repeated_call_reuses_result(self):
        calls = []

        class Person(Form):
            name = CharField(validators=[async_validator_factory(calls)])

        form = Person({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(calls, ["x"])

    async def test_sync_then_async_reuses_result(self):
        calls = []

        def sync_validator(value):
            calls.append(value)

        class Person(Form):
            name = CharField(validators=[sync_validator])

        form = Person({"name": "x"})
        self.assertIs(form.is_valid(), True)
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(calls, ["x"])

    async def test_async_then_sync_reuses_result(self):
        calls = []

        def sync_validator(value):
            calls.append(value)

        class Person(Form):
            name = CharField(validators=[sync_validator])

        form = Person({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        self.assertIs(form.is_valid(), True)
        self.assertEqual(calls, ["x"])

    async def test_changed_input_triggers_new_round(self):
        calls = []

        class Person(Form):
            name = CharField(validators=[async_validator_factory(calls)])

        form = Person({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        form.data = form.data.copy()
        form.data["name"] = "y"
        form._bound_fields_cache = {}
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(calls, ["x", "y"])
        self.assertEqual(form.cleaned_data, {"name": "y"})

    async def test_validators_not_run_again_after_completion(self):
        calls = []

        async def validator(value):
            calls.append(value)
            raise ValidationError("bad")

        class Person(Form):
            name = CharField(validators=[validator])

        form = Person({"name": "x"})
        self.assertIs(await form.ais_valid(), False)
        self.assertIs(await form.ais_valid(), False)
        self.assertEqual(calls, ["x"])
        self.assertEqual(form.errors["name"], ["bad"])


class AsyncFormConcurrencyTests(SimpleTestCase):
    async def test_concurrent_waiters_share_one_round(self):
        calls = []

        class Person(Form):
            name = CharField(
                validators=[async_validator_factory(calls, delay=0.02)]
            )

        form = Person({"name": "x"})
        results = await asyncio.gather(
            form.ais_valid(), form.ais_valid(), form.ais_valid()
        )
        self.assertEqual(results, [True, True, True])
        self.assertEqual(calls, ["x"])

    async def test_validation_error_shared_by_all_waiters(self):
        calls = []

        async def validator(value):
            calls.append(value)
            await asyncio.sleep(0.02)
            raise ValidationError("bad")

        class Person(Form):
            name = CharField(validators=[validator])

        form = Person({"name": "x"})
        results = await asyncio.gather(
            form.ais_valid(), form.ais_valid()
        )
        self.assertEqual(results, [False, False])
        self.assertEqual(calls, ["x"])
        self.assertEqual(form.errors["name"], ["bad"])

    async def test_one_cancelling_waiter_does_not_stop_round(self):
        calls = []

        class Person(Form):
            name = CharField(
                validators=[async_validator_factory(calls, delay=0.03)]
            )

        form = Person({"name": "x"})

        async def cancelling_waiter():
            try:
                await asyncio.wait_for(form.afull_clean(), timeout=0.005)
            except asyncio.TimeoutError:
                return "cancelled"

        t1 = asyncio.ensure_future(cancelling_waiter())
        t2 = asyncio.ensure_future(form.ais_valid())
        self.assertEqual(await t1, "cancelled")
        self.assertIs(await t2, True)
        self.assertEqual(calls, ["x"])

    async def test_all_waiters_cancelling_abandons_round(self):
        calls = []

        class Person(Form):
            name = CharField(
                validators=[async_validator_factory(calls, delay=0.05)]
            )

        form = Person({"name": "x"})
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(form.afull_clean(), form.afull_clean()),
                timeout=0.005,
            )
        # Wait for the abandoned round to settle, then retry from scratch.
        await asyncio.sleep(0.02)
        self.assertIsNone(form._errors)
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(len(calls), 2)

    async def test_abandoned_round_retried_with_current_inputs(self):
        calls = []

        class Person(Form):
            name = CharField(
                validators=[async_validator_factory(calls, delay=0.05)]
            )

        form = Person({"name": "first"})
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(form.afull_clean(), timeout=0.005)
        await asyncio.sleep(0.02)
        form.data = form.data.copy()
        form.data["name"] = "second"
        form._bound_fields_cache = {}
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data, {"name": "second"})
        self.assertEqual(calls[-1], "second")

    async def test_mid_flight_external_reads_are_empty(self):
        class Person(Form):
            name = CharField(
                validators=[async_validator_factory([], delay=0.03)]
            )

        form = Person({"name": "x"})
        task = asyncio.ensure_future(form.afull_clean())
        await asyncio.sleep(0.01)
        try:
            self.assertEqual(dict(form.errors), {})
            self.assertEqual(form.cleaned_data, {})
        finally:
            await task
        self.assertEqual(form.cleaned_data, {"name": "x"})

    async def test_mid_flight_reads_hide_previous_round(self):
        class Person(Form):
            name = CharField(
                validators=[async_validator_factory([], delay=0.03)]
            )

        form = Person({"name": "one"})
        self.assertIs(await form.ais_valid(), True)
        form.data = form.data.copy()
        form.data["name"] = "two"
        form._bound_fields_cache = {}
        task = asyncio.ensure_future(form.afull_clean())
        await asyncio.sleep(0.01)
        try:
            self.assertEqual(form.cleaned_data, {})
            self.assertEqual(dict(form.errors), {})
        finally:
            await task
        self.assertEqual(form.cleaned_data, {"name": "two"})

    async def test_ordinary_exception_propagates_to_all_waiters(self):
        class BoomError(Exception):
            pass

        async def validator(value):
            await asyncio.sleep(0.02)
            raise BoomError("boom")

        class Person(Form):
            name = CharField(validators=[validator])

        form = Person({"name": "x"})
        results = await asyncio.gather(
            form.ais_valid(), form.ais_valid(), return_exceptions=True
        )
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], BoomError)
        self.assertIsInstance(results[1], BoomError)

    async def test_ordinary_exception_is_not_cached_as_form_error(self):
        class BoomError(Exception):
            pass

        async def validator(value):
            raise BoomError("boom")

        class Person(Form):
            name = CharField(validators=[validator])

        form = Person({"name": "x"})
        with self.assertRaises(BoomError):
            await form.ais_valid()
        # Not recorded as a form error.
        self.assertNotIn("name", form.errors if form._errors else {})
        with self.assertRaises(BoomError):
            await form.ais_valid()


class AsyncFormSyncCompatibilityTests(SimpleTestCase):
    def test_sync_is_valid_unchanged(self):
        class Person(Form):
            name = CharField()

        form = Person({"name": "x"})
        self.assertIs(form.is_valid(), True)
        self.assertEqual(form.cleaned_data, {"name": "x"})

    def test_async_validator_in_sync_path_raises(self):
        async def validator(value):
            await asyncio.sleep(0)

        class Person(Form):
            name = CharField(validators=[validator])

        form = Person({"name": "x"})
        with self.assertRaises(AsynchronousOnlyOperation):
            form.is_valid()

    def test_async_clean_field_in_sync_path_raises(self):
        class Person(Form):
            name = CharField()

            async def clean_name(self):
                return self.cleaned_data["name"]

        form = Person({"name": "x"})
        with self.assertRaises(AsynchronousOnlyOperation):
            form.is_valid()

    def test_async_form_clean_in_sync_path_raises(self):
        class Person(Form):
            name = CharField()

            async def clean(self):
                return self.cleaned_data

        form = Person({"name": "x"})
        with self.assertRaises(AsynchronousOnlyOperation):
            form.is_valid()

    async def test_integer_field_async_path(self):
        class Numbers(Form):
            n = IntegerField()

        form = Numbers({"n": "42"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["n"], 42)

        form = Numbers({"n": "notanint"})
        self.assertIs(await form.ais_valid(), False)
        self.assertIn("n", form.errors)

    async def test_disabled_field_uses_initial(self):
        class Person(Form):
            name = CharField(disabled=True)

        form = Person({}, initial={"name": "initial-name"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data["name"], "initial-name")

    async def test_empty_permitted_short_circuits_when_unchanged(self):
        class Person(Form):
            name = CharField()

        form = Person(
            {},
            empty_permitted=True,
            use_required_attribute=False,
        )
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data, {})

    async def test_awaitable_branch_errors_held_until_done(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_validator(value):
            started.set()
            await release.wait()
            raise ValidationError("late")

        def sync_validator(value):
            raise ValidationError("early")

        class Person(Form):
            name = CharField(validators=[slow_validator, sync_validator])

        form = Person({"name": "x"})
        task = asyncio.ensure_future(form.afull_clean())
        await started.wait()
        # The awaitable branch has not completed, and the synchronous
        # validator after it has not run yet; nothing is committed.
        self.assertEqual(dict(form.errors), {})
        release.set()
        await task
        self.assertEqual(form.errors["name"], ["late", "early"])

    async def test_post_clean_direct_state_access(self):
        # ModelForm._post_clean() reads self._errors and self.cleaned_data
        # directly (bypassing the attributes); the asynchronous round must
        # operate on that same live state.
        class Person(Form):
            name = CharField()

            def _post_clean(self):
                # Read state directly, like BaseModelForm._post_clean() does
                # (self._errors / self.cleaned_data, bypassing properties).
                assert isinstance(self._errors, dict)
                self._post_clean_seen = self.cleaned_data.get("name")

        form = Person({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(form.cleaned_data, {"name": "x"})
        self.assertEqual(getattr(form, "_post_clean_seen", None), "x")

    async def test_awaitable_post_clean_is_awaited(self):
        class Person(Form):
            name = CharField()

            async def _post_clean(self):
                # An awaitable result from the post-clean hook is awaited
                # before the round is committed.
                self._post_clean_seen = self.cleaned_data.get("name")

        form = Person({"name": "x"})
        self.assertIs(await form.ais_valid(), True)
        self.assertEqual(getattr(form, "_post_clean_seen", None), "x")


