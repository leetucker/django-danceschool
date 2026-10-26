import json
from unittest import mock

import httpx
from django.contrib.auth.context_processors import PermWrapper
from django.contrib.messages import get_messages
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.urls import reverse
from square.core.api_error import ApiError

from danceschool.core.constants import REFUND_VALIDATION_STR
from danceschool.core.models import Invoice
from danceschool.core.tests.defaults import DefaultSchoolTestCase

from .api_client import clear_square_failure, square_api_status
from .models import SquarePaymentRecord, SquarePayoutEntry, SquarePayoutRecord


UNAUTHORIZED = {'errors': [{
    'category': 'AUTHENTICATION_ERROR', 'code': 'UNAUTHORIZED',
    'detail': 'This request could not be authorized.',
}]}
NOT_FOUND = {'errors': [{'category': 'INVALID_REQUEST_ERROR', 'code': 'NOT_FOUND', 'detail': 'Not found.'}]}
PAYMENT = {
    'id': 'PAYMENT_CACHED', 'amount_money': {'amount': 2500, 'currency': 'USD'},
    'processing_fee': [{'amount_money': {'amount': 100}}],
}


def fake_client(get=None, refund=None):
    '''
    A stand-in for the Square client.  get and refund are what
    payments.get() and refunds.refund_payment() raise or return (as a
    response whose .dict() is that value).
    '''
    client = mock.Mock()
    for method, result in ((client.payments.get, get), (client.refunds.refund_payment, refund)):
        if isinstance(result, Exception):
            method.side_effect = result
        else:
            method.return_value.dict.return_value = result or {}
    return client


@override_settings(SQUARE_ACCESS_TOKEN='test-token')
class SquareAPIUnavailableTest(DefaultSchoolTestCase):
    '''
    Without working Square credentials (a test site, or a Square outage),
    pages that show Square payments must still render, using the data saved
    locally, and operations that need Square must report why they failed.
    '''

    def setUp(self):
        clear_square_failure()
        self.addCleanup(clear_square_failure)
        self.invoice = Invoice.objects.create(firstName='Pat', lastName='Payer', total=25, grossTotal=25)
        self.cached = SquarePaymentRecord.objects.create(
            invoice=self.invoice, paymentId='PAYMENT_CACHED', locationId='LOC',
            data={'apiPaymentResponse': PAYMENT},
        )
        self.uncached = SquarePaymentRecord.objects.create(paymentId='PAYMENT_UNCACHED', locationId='LOC')

    def use_client(self, client):
        patcher = mock.patch('danceschool.payments.square.models.api_client', client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def fresh(self, record):
        ''' Reload a record, so that it picks up the patched client. '''
        return SquarePaymentRecord.objects.get(pk=record.pk)

    def test_uncached_amounts_are_none_when_the_api_fails(self):
        self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        record = self.fresh(self.uncached)
        self.assertIsNone(record.netAmountPaid)
        self.assertIn('UNAUTHORIZED', record.api_error)
        self.assertIsNone(record.netFees)
        self.assertIsNone(record.netRevenue)
        self.assertEqual(record.orderLineItems, [])
        ok, reason = square_api_status()
        self.assertFalse(ok)
        self.assertIn('UNAUTHORIZED', reason)

    def test_cached_amounts_need_no_api(self):
        client = self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        record = self.fresh(self.cached)
        self.assertEqual(record.netAmountPaid, 25)
        self.assertEqual(record.netFees, 1)
        self.assertEqual(record.netRevenue, 24)
        client.payments.get.assert_not_called()

    def test_one_failure_pauses_further_calls(self):
        client = self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        other = SquarePaymentRecord.objects.create(paymentId='PAYMENT_OTHER', locationId='LOC')
        self.assertIsNone(self.fresh(self.uncached).netAmountPaid)
        record = self.fresh(other)
        self.assertIsNone(record.netAmountPaid)
        self.assertEqual(client.payments.get.call_count, 1)
        self.assertIn('UNAUTHORIZED', record.api_error)

    def test_unknown_payment_does_not_pause_the_api(self):
        self.use_client(fake_client(get=ApiError(status_code=404, body=NOT_FOUND)))
        record = self.fresh(self.uncached)
        self.assertIsNone(record.netAmountPaid)
        self.assertIn('NOT_FOUND', record.api_error)
        self.assertTrue(square_api_status()[0])

    def test_no_connection(self):
        self.use_client(fake_client(get=httpx.ConnectError('no network')))
        record = self.fresh(self.uncached)
        self.assertIsNone(record.netAmountPaid)
        self.assertIn('ConnectError', record.api_error)
        self.assertFalse(square_api_status()[0])

    @override_settings(SQUARE_ACCESS_TOKEN=None)
    def test_no_credentials_means_no_calls(self):
        client = self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        record = self.fresh(self.uncached)
        self.assertIsNone(record.netAmountPaid)
        self.assertIn('SQUARE_ACCESS_TOKEN', record.api_error)
        client.payments.get.assert_not_called()

    def test_errors_still_raise_for_operations(self):
        self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        with self.assertRaises(ApiError):
            self.fresh(self.uncached).getPayment()

    def test_admin_pages_render_with_a_warning(self):
        self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        payout = SquarePayoutRecord.objects.create(
            payoutId='PAYOUT1', locationId='LOC', creationDate=self.invoice.creationDate,
            modifiedDate=self.invoice.creationDate,
        )
        SquarePayoutEntry.objects.create(entryId='ENTRY1', payout=payout, paymentRecord=self.uncached)
        self.client.force_login(self.superuser)
        # The first page load finds out that the API is unavailable...
        self.assertEqual(self.client.get(reverse('admin:square_squarepaymentrecord_changelist')).status_code, 200)
        # ...and from then on every page says so.
        for url in (
            reverse('admin:square_squarepaymentrecord_changelist'),
            reverse('admin:square_squarepaymentrecord_change', args=(self.uncached.pk,)),
            reverse('admin:square_squarepaymentrecord_change', args=(self.cached.pk,)),
            reverse('admin:square_squarepayoutrecord_changelist'),
            reverse('admin:square_squarepayoutrecord_change', args=(payout.pk,)),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertContains(response, 'Square API unavailable (UNAUTHORIZED')

    def test_refund_reports_api_failure(self):
        self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        response = self.fresh(self.cached).refund(5)
        self.assertEqual(response[0]['status'], 'error')
        self.assertIn('UNAUTHORIZED', response[0]['errors'][0]['message'])
        json.dumps(response)

    def test_refund_processor_errors_are_plain_data(self):
        self.use_client(fake_client(
            get={'payment': PAYMENT}, refund=ApiError(status_code=400, body=NOT_FOUND),
        ))
        with mock.patch('danceschool.payments.square.models.updateSquareFees') as task:
            response = self.fresh(self.cached).refund(5)
        self.assertEqual(response, [{'status': 'error', 'errors': [{'code': 'NOT_FOUND', 'message': 'Not found.'}]}])
        task.schedule.assert_not_called()

    def test_successful_refund_schedules_fee_update(self):
        self.use_client(fake_client(
            get={'payment': PAYMENT}, refund={'refund': {'id': 'REFUND1', 'amount_money': {'amount': 500}}},
        ))
        with mock.patch('danceschool.payments.square.models.updateSquareFees') as task:
            response = self.fresh(self.cached).refund(5)
        self.assertEqual(response[0]['status'], 'success')
        self.assertEqual(response[0]['refundAmount'], 5)
        task.schedule.assert_called_once_with(args=(self.cached.pk,), delay=60)

    def test_create_invoice_needs_payment_data(self):
        self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('viewOrCreatePaymentInvoice', args=(self.uncached.pk,)))
        self.assertRedirects(response, reverse('admin:square_squarepaymentrecord_changelist'))
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('Cannot create an invoice' in m and 'UNAUTHORIZED' in m for m in messages))

    def confirm_refund(self, record):
        record.invoice = self.invoice
        record.save()
        self.client.force_login(self.superuser)
        session = self.client.session
        session[REFUND_VALIDATION_STR] = {'form_data': {
            'id': str(self.invoice.id), 'total_refund_amount': 10, 'initial_refund_amount': 0,
        }}
        session.save()
        return self.client.get(reverse('refundConfirmation') + '?confirmed=true')

    def test_refund_view_does_not_guess_an_unknown_amount(self):
        # Only the uncached payment: its amount cannot be determined.
        self.cached.delete()
        client = self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        response = self.confirm_refund(self.uncached)
        self.assertEqual(response.status_code, 302)
        client.refunds.refund_payment.assert_not_called()
        self.invoice.refresh_from_db()
        failed = self.invoice.data['refunds'][-1]
        self.assertEqual(failed['status'], 'error')
        self.assertIn('no refund was attempted', failed['errorMessage'])
        self.assertIn('UNAUTHORIZED', failed['errorMessage'])
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('no refund was attempted' in m for m in messages))

    def test_refund_view_shows_processor_errors(self):
        self.use_client(fake_client(get=ApiError(status_code=401, body=UNAUTHORIZED)))
        response = self.confirm_refund(self.cached)
        self.assertEqual(response.status_code, 302)
        self.invoice.refresh_from_db()
        failed = self.invoice.data['refunds'][-1]
        self.assertEqual(failed['status'], 'error')
        self.assertIn('Unable to retrieve the Square payment', failed['errorMessage'])
        self.assertIn('UNAUTHORIZED', failed['errorMessage'])


class SquarePointOfSaleTemplateTest(DefaultSchoolTestCase):
    '''
    The point-of-sale button must render based on the session-level payAtDoor
    flag, not on a Registration object (which does not exist for carts that
    contain only merchandise).
    '''

    def _render(self, **extra):
        request = RequestFactory().get('/register/summary/')
        request.user = self.superuser
        context = {
            'request': request,
            'user': self.superuser,
            'perms': PermWrapper(self.superuser),
            'currencyCode': 'USD',
            'instance': None,
            'returnUrl': 'https://example.com/square/pos/',
            'squareApplicationId': 'test-app-id',
        }
        context.update(extra)
        return render_to_string('square/point_of_sale.html', context, request=request)

    def test_button_renders_without_registration(self):
        '''Merchandise-only carts have no Registration but should show the button.'''
        html = self._render(payAtDoor=True, registration=None)
        self.assertIn('square-pos-button', html)

    def test_button_renders_with_registration(self):
        html = self._render(payAtDoor=True, registration=object())
        self.assertIn('square-pos-button', html)

    def test_button_hidden_when_not_pay_at_door(self):
        html = self._render(payAtDoor=False, registration=None)
        self.assertNotIn('square-pos-button', html)
