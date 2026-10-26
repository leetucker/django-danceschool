from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from cms.models.pluginmodel import CMSPlugin
from cms.models.fields import PageField
from square.core.api_error import ApiError

import logging
import uuid

from danceschool.core.models import PaymentRecord
from .tasks import updateSquareFees
from .api_client import (
    SQUARE_API_ERRORS, api_client, describe_square_error, iso_timestamp_to_localtime,
    record_square_failure, square_api_status,
)


# Define logger for this file
logger = logging.getLogger(__name__)


class SquareAPIMixin:
    '''
    Shared handling of Square API failures for models that read from the
    API.  Display code passes fail_silently=True and falls back to the data
    saved locally; operations that need fresh data let the error propagate.
    '''

    def _api_usable(self, fail_silently):
        '''
        Whether to call the API.  When failing silently, skip the call if
        Square is not configured or failed moments ago (see square_api_status).
        '''
        if not fail_silently:
            return True
        ok, reason = square_api_status()
        if not ok:
            self.api_error = reason
        return ok

    def _api_failed(self, error, fail_silently, what):
        '''
        Handle an exception from the Square client: re-raise it, or record
        why it happened and let the caller fall back to saved data.
        '''
        if not fail_silently:
            raise error
        reason = describe_square_error(error)
        self.api_error = reason
        logger.warning('Square API request for %s failed: %s', what, reason)
        # A failure that will affect every request (bad credentials, rate
        # limiting, an outage, no connection) pauses API calls for a few
        # minutes.  One unknown ID (404) does not.
        status = getattr(error, 'status_code', None)
        if status is None or status in (401, 403, 429) or status >= 500:
            record_square_failure(reason)


class SquarePaymentRecord(SquareAPIMixin, PaymentRecord):
    '''
    Keeps a local record of Square transactions so that they can be looked up
    using the REST API.
    '''

    paymentId = models.CharField(
        _('Square Payment ID'), max_length=100, unique=True
    )
    orderId = models.CharField(
        _('Square Order ID'), max_length=100, unique=True, null=True,
        db_column='transactionId',
    )
    locationId = models.CharField(_('Square Location ID'), max_length=100)
    payerEmail = models.EmailField(_('Associated email'), null=True, blank=True)

    data = models.JSONField(_('Additional data'), default=dict, blank=True)

    @property
    def methodName(self):
        return 'Square Checkout'

    @property
    def refundable(self):
        return True

    @property
    def recordId(self):
        '''
        Payment methods should override this if they keep their own unique identifiers.
        '''
        return self.paymentId

    # The amounts below are for display: they never raise.  They use the
    # payment data saved locally when there is some, and otherwise ask the
    # API.  If neither is possible they return None (and self.api_error says
    # why), never a misleading 0.

    @property
    def grossAmountPaid(self):
        payment = self.getPayment(fail_silently=True)
        if not payment:
            return None
        return (
            payment.get('amount_money', {}).get('amount', 0) / 100
        )
    grossAmountPaid.fget.short_description = _('Gross amount paid')

    @property
    def netAmountPaid(self):
        payment = self.getPayment(fail_silently=True)
        if not payment:
            return None
        return (
            payment.get('amount_money', {}).get('amount', 0) / 100 -
            payment.get('refunded_money', {}).get('amount', 0) / 100
        )
    netAmountPaid.fget.short_description = _('Net amount paid')

    @property
    def netRefund(self):
        payment = self.getPayment(fail_silently=True)
        if not payment:
            return None
        return (
            payment.get('refunded_money', {}).get('amount', 0) / 100
        )
    netRefund.fget.short_description = _('Net refund amount')

    @property
    def netFees(self):
        payment = self.getPayment(fail_silently=True)
        if not payment:
            return None
        refunds = self.getRefunds(payment=payment, fail_silently=True)

        fees = sum([
            x.get('amount_money', {}).get('amount', 0) / 100
            for x in payment.get('processing_fee', [])
        ])

        for r in refunds:
            fees += sum([
                f.get('amount_money', {}).get('amount', 0) / 100
                for f in r.get('processing_fee', [])
            ])

        return fees
    netFees.fget.short_description = _('Net fees')

    @property
    def netRevenue(self):
        paid, fees = self.netAmountPaid, self.netFees
        if paid is None or fees is None:
            return None
        return paid - fees
    netRevenue.fget.short_description = _('Net revenue')

    @property
    def apiPaymentCreated(self):
        return iso_timestamp_to_localtime(
            self.data.get('apiPaymentResponse', {}).get('created_at')
        )
    apiPaymentCreated.fget.short_description = _('Payment created')

    @property
    def apiPaymentModified(self):
        return iso_timestamp_to_localtime(
            self.data.get('apiPaymentResponse', {}).get('updated_at')
        )
    apiPaymentModified.fget.short_description = _('Payment last updated')

    @property
    def orderLineItems(self):
        ''' Get the line items from the Orders API data. '''
        order_data = self.getOrder(fail_silently=True)
        return order_data.get('line_items', [])

    @property
    def receiptNumber(self):
        return self.data.get('apiPaymentResponse', {}).get('receipt_number')

    @property
    def receiptUrl(self):
        return self.data.get('apiPaymentResponse', {}).get('receipt_url')

    @property
    def paidOut(self):
        return self.getPayoutEntries().exists()
    
    @property
    def paidOutAmount(self):
        return sum([x.amountPaid or 0 for x in self.getPayoutEntries()])

    @property
    def paidOutDate(self):
        return max([x.payoutDate for x in self.getPayoutEntries()])

    def getPayment(
        self, client=None, use_cache=True, update_cache=True, commit=True,
        fail_silently=False,
    ):
        '''
        The Square payment data, from the local cache if allowed, otherwise
        from the API.  With fail_silently=True, an API failure returns the
        cached data (or {}) and sets self.api_error, instead of raising.
        '''
        # No need to proceed if there is no way to get the payment record from
        # the API.
        if not self.paymentId:
            return {}

        cached = self.data.get('apiPaymentResponse', None)
        if use_cache and cached is not None:
            return cached

        if not self._api_usable(fail_silently):
            return cached or {}

        if not client:
            client = self.client

        try:
            response = client.payments.get(self.paymentId).dict().get('payment', {})
        except SQUARE_API_ERRORS as e:
            self._api_failed(e, fail_silently, 'payment %s' % self.paymentId)
            return cached or {}

        if (update_cache is True) and (response != cached):
            self.data['apiPaymentResponse'] = response
            self.data['apiPaymentResponseDate'] = timezone.localtime().isoformat()
            if commit:
                if not self.orderId:
                    self.orderId = response.get('order_id')
                self.save()
        return response

    def getOrder(
        self, client=None,  use_cache=True, update_cache=True, commit=True,
        fail_silently=False,
    ):
        '''
        Order data from the API is not automatically imported or retained
        locally, but it can be requested and used in individual views, and is
        cached when that occurs.  fail_silently is as for getPayment().
        '''
        cached = self.data.get('apiOrderResponse', None)
        if use_cache and cached is not None:
            return cached

        if not self.orderId:
            self.orderId = self.getPayment(
                commit=commit, fail_silently=fail_silently
            ).get('order_id')

        # No need to proceed if there is no way to get the order record from
        # the API.
        if not self.orderId:
            return {}

        if not self._api_usable(fail_silently):
            return cached or {}

        if not client:
            client = self.client

        try:
            response = client.orders.get(self.orderId).dict().get('order', {})
        except SQUARE_API_ERRORS as e:
            self._api_failed(e, fail_silently, 'order %s' % self.orderId)
            return cached or {}

        if (update_cache is True) and (response != cached):
            self.data['apiOrderResponse'] = response
            self.data['apiOrderResponseDate'] = timezone.localtime().isoformat()
            if commit:
                self.save()
        return response

    def getRefunds(
            self, client=None, use_cache=True, update_cache=True, payment=None,
            commit=True, fail_silently=False,
    ):

        cached = self.data.get('apiRefundResponse', None)
        if use_cache and cached is not None:
                return cached

        if not client:
            client = self.client

        if not payment:
            payment = self.getPayment(
                client, use_cache, update_cache, commit, fail_silently=fail_silently
            )

        response = []

        if payment.get('refund_ids', []):
            if not self._api_usable(fail_silently):
                return cached or []
            for y in payment['refund_ids']:
                try:
                    refund_response = client.refunds.get(y).dict().get('refund', {})
                except ApiError:
                    continue
                except SQUARE_API_ERRORS as e:
                    # No connection at all: stop, rather than try each refund.
                    self._api_failed(e, fail_silently, 'refund %s' % y)
                    return cached or []
                else:
                    response.append(refund_response)

        if update_cache and response and (response != cached):
            self.data['apiRefundResponse'] = response
            self.data['apiRefundResponseDate'] = timezone.localtime().isoformat()
            if commit:
                self.save()
        return response

    def getPayoutEntries(self):
        return self.squarepayoutentry_set.all()

    def getPayerEmail(self):
        return self.payerEmail

    def refund(self, amount=None):
        # Start by ensuring that we have the most recent information on the
        # payment and what remains to be refunded.
        try:
            payment = self.getPayment(use_cache=False, commit=False)
        except SQUARE_API_ERRORS as e:
            reason = describe_square_error(e)
            logger.error('Unable to retrieve Square payment %s for refund: %s', self.paymentId, reason)
            return [{
                'status': 'error', 'errors': [{
                    'code': 'square_api_unavailable',
                    'message': _('Unable to retrieve the Square payment: %s') % reason,
                }],
            }]
        if not payment:
            return [{
                'status': 'error', 'errors': [
                    {'code': 'no_payment', 'message': _('Unable to retrieve Square payment from record.')},
                ]
            }]

        # SquarePaymentRecords used to potentially reference multiple tenders,
        # but they are now associated with a single payment that can be refunded
        # up to the allowable amount directly.
        amount_remaining = (
            payment.get('amount_money', {}).get('amount', 0) / 100 -
            payment.get('refunded_money', {}).get('amount', 0) / 100
        )

        if amount:
            amount_to_refund = min(amount, amount_remaining)
        else:
            amount_to_refund = amount_remaining

        refundData = []
        idempotency_key = str(uuid.uuid1())

        body = {
            'idempotency_key': idempotency_key,
            'payment_id': payment.get('id'),
            'amount_money': {
                'amount': int(amount_to_refund * 100),
                'currency': payment.get('amount_money', {}).get('currency')
            }
        }

        try:
            response = self.client.refunds.refund_payment(**body)
            this_refund = response.dict().get('refund', {})

            # Note that fees are often 0 or missing here, but we enqueue the task
            # retrieve and update them afterward.
            refundData.append({
                'status': 'success',
                'refund_id': this_refund.get('id'),
                'refundAmount': float(this_refund.get('amount_money', {}).get('amount', 0)) / 100,
                'fees': float(this_refund.get('app_fee_money', {}).get('amount', 0)) / 100,
            })

            # Once the refund process is complete, fees will be calculated,
            # so schedule a task to get them and update records one minute
            # in the future.
            updateSquareFees.schedule(args=(self.pk, ), delay=60)
        except SQUARE_API_ERRORS as e:
            reason = describe_square_error(e)
            logger.error('Error in providing Square refund: %s' % reason)
            # Plain dicts, so that they can be saved in the invoice's data.
            errors = [
                {'code': getattr(x, 'code', None), 'message': getattr(x, 'detail', None)}
                for x in (getattr(e, 'errors', None) or [])
            ] or [{'code': 'square_api_unavailable', 'message': reason}]
            refundData.append({'status': 'error', 'errors': errors})

        return refundData

    def __init__(self, *args, **kwargs):
        client = kwargs.pop('client', api_client)
        super().__init__(*args, **kwargs)
        self.client = client
        # Why the last API request for this record failed, if it did.
        self.api_error = None

    class Meta:
        permissions = (
            ('handle_pos_payments', _('Has access to point-of-sale payment functionality')),
        )
        verbose_name = _('Square payment record')
        verbose_name_plural = _('Payment records')


class SquarePayoutRecord(SquareAPIMixin, models.Model):
    '''
    Keeps a local record of Square payouts so that they can be looked up
    using the REST API and so that square payouts can be corresponded to payment
    records.
    '''

    payoutId = models.CharField(
        _('Square Payout ID'), max_length=100, unique=True, null=False,
        primary_key=True, editable=False
    )
    locationId = models.CharField(
        _('Square Location ID'), max_length=100, editable=False
    )
    creationDate = models.DateTimeField(_('Payout creation date'))
    modifiedDate = models.DateTimeField(_('Payout modified date'))

    data = models.JSONField(_('Additional data'), default=dict, blank=True)

    # For display: these never raise, and return None if there is no payout
    # data (see SquarePaymentRecord).

    @property
    def amountPaid(self):
        payout = self.getPayout(fail_silently=True)
        if not payout:
            return None
        return (payout.get('amount_money') or {}).get('amount', 0) / 100
    amountPaid.fget.short_description = _('Total payout amount')

    @property
    def netRevenue(self):
        return self.amountPaid

    @property
    def payoutDate(self):
        return iso_timestamp_to_localtime(
            self.getPayout(fail_silently=True).get('arrival_date', '')
        )
    payoutDate.fget.short_description = _('Payout deposit date')

    def getPayout(
        self, client=None, use_cache=True, update_cache=True, commit=True,
        fail_silently=False,
    ):
        # No need to proceed if there is no way to get the payout record from
        # the API.
        if not self.payoutId:
            return {}

        cached = self.data.get('apiPayoutResponse', None)
        if use_cache and cached is not None:
            return cached

        if not self._api_usable(fail_silently):
            return cached or {}

        if not client:
            client = self.client

        try:
            response = client.payouts.get(self.payoutId).dict().get('payout', {})
        except SQUARE_API_ERRORS as e:
            self._api_failed(e, fail_silently, 'payout %s' % self.payoutId)
            return cached or {}

        if (update_cache is True) and (response != cached):
            self.data['apiPayoutResponse'] = response
            self.data['apiPayoutResponseDate'] = timezone.localtime().isoformat()
            if commit:
                self.save()
        return response

    def getPayoutEntries(
        self, client=None, use_cache=True, update_cache=True, commit=True
    ):
        # No need to proceed if there is no way to get the payout record from
        # the API.
        if not self.payoutId:
            return {}

        cached = self.data.get('apiEntriesResponse', None)
        if use_cache and cached is not None:
            return cached

        if not client:
            client = self.client

        response = client.payouts.list_entries(self.payoutId).dict().get('items', [])

        if (update_cache is True) and (response != cached):
            self.data['apiEntriesResponse'] = response
            self.data['apiEntriesResponseDate'] = timezone.localtime().isoformat()
            if commit:
                self.save()
        return response

    def updatePayoutEntries(self, **kwargs):
        '''
        Ensure that all payout entries exist in the database, and that any
        entries that are not in the API data are deleted, so that the database
        info matches the API info.
        '''

        entries = self.getPayoutEntries(**kwargs)
        for entry in entries:
            SquarePayoutEntry.objects.update_or_create(
                entryId=entry.get('id'),
                payout=self,
                defaults={
                    'paymentRecord': SquarePaymentRecord.objects.filter(
                        paymentId=entry.get('type_charge_details', {}).get('payment_id')
                    ).first(),
                    'data': {
                        'apiEntryResponse':entry,
                        'apiEntryResponseDate': timezone.localtime().isoformat()
                    }
                }
            )
        SquarePayoutEntry.objects.filter(payout=self).exclude(
            entryId__in=[x.get('id') for x in entries]
        ).delete()

    def __init__(self, *args, **kwargs):
        client = kwargs.pop('client', api_client)
        super().__init__(*args, **kwargs)
        self.client = client
        # Why the last API request for this record failed, if it did.
        self.api_error = None

    class Meta:
        verbose_name = _('Square payout record')
        verbose_name_plural = _('Payout records')


class SquarePayoutEntry(models.Model):
    '''
    This class defines the relationship between payments and payouts so that
    we can perform accounting to ensure that all Square revenues are properly
    recognized.
    '''

    entryId = models.CharField(
        _('Square Payout Entry ID'), max_length=100, unique=True, null=False,
        primary_key=True, editable=False
    )
    payout = models.ForeignKey(SquarePayoutRecord, on_delete=models.CASCADE)
    paymentRecord = models.ForeignKey(
        SquarePaymentRecord, null=True, on_delete=models.SET_NULL
    )
    
    data = models.JSONField(_('Additional data'), default=dict, blank=True)

    @property
    def apiEntry(self):
        return self.data.get('apiEntryResponse', {})

    @property
    def amountPaid(self):
        money = self.apiEntry.get('net_amount_money')
        if not money:
            return None
        return money.get('amount', 0) / 100

    @property
    def netRevenue(self):
        return self.amountPaid

    @property
    def payoutDate(self):
        return iso_timestamp_to_localtime(
            self.apiEntry.get('effective_at', '')
        )

    class Meta:
        verbose_name = _('Square payout entry')
        verbose_name_plural = _('Payout entries')


class SquareCheckoutFormModel(CMSPlugin):
    ''' This model holds options for instances of the SquarePaymentFormPlugin '''

    successPage = PageField(
        verbose_name=_('Success Page'),
        help_text=_(
            'When the user returns to the site after a successful ' +
            'transaction, send them to this page.'
        ),
        related_name='successPageForSquare', null=True, blank=True
    )
    defaultAmount = models.FloatField(
        verbose_name=_('Default amount'),
        help_text=_('The initial value for gift certificate forms.'),
        default=0
    )

    def get_short_description(self):
        return self.plugin_type or self.id
