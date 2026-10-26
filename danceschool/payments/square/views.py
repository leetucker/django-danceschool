from django.http import (
    Http404, HttpResponseRedirect, HttpResponseBadRequest, JsonResponse
)
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ObjectDoesNotExist
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone
from django.conf import settings
from django.contrib import messages
from django.utils.html import format_html
from django.views.generic import View, UpdateView


import uuid
from braces.views import PermissionRequiredMixin
from square.core.api_error import ApiError
import logging
from datetime import timedelta
import json
from base64 import b64decode
import binascii
from urllib.parse import unquote
from time import sleep

from danceschool.core.models import Invoice, InvoiceItem
from danceschool.core.constants import getConstant, PAYMENT_VALIDATION_STR
from danceschool.core.helpers import getReturnPage
from danceschool.core.views.cart import clear_reg_cart


from .api_client import api_client
from .forms import CreateInvoiceForm
from .models import SquarePaymentRecord
from .tasks import updateSquareFees


# Define logger for this file
logger = logging.getLogger(__name__)


class SquareCheckoutErrorResponse(JsonResponse):

    def __init__(self, message, **kwargs):
        data = {
            'status': 'error',
            'message': message,
        }
        if kwargs.get('redirect_url'):
            data['redirect_url'] = kwargs.pop('redirect_url', None)

        return super().__init__(data, **kwargs)


class ProcessSquarePaymentView(View):
    '''
    This view handles the charging of approved Square Checkout payments.

    All Checkout payments must either be associated with a pre-existing Invoice
    or a registration, or they must have an amount and type passed in the post data
    (such as gift certificate payment requests).
    '''

    def post(self, request, *args, **kwargs):
        logger.info('Received request for Square Checkout payment.')

        try:
            data = json.loads(request.body)
        except json.decoder.JSONDecodeError:
            return HttpResponseBadRequest()

        invoice_id = data.get('invoice_id')
        amount = data.get('amount')
        submissionUserId = data.get('user_id')
        transactionType = data.get('transaction_type')
        taxable = data.get('taxable', False)
        sourceUrl = data.get('sourceUrl', reverse('showRegSummary'))
        addSessionInfo = data.get('addSessionInfo', False)
        customerEmail = data.get('customerEmail')
        finalSuccessUrl = data.get('finalSuccessUrl')

        # Send users back to the invoice to confirm the successful payment.
        # If none is specified, then return to the registration page.
        successUrl = data.get(
            'successUrl',
            getReturnPage(request.session.get('SITE_HISTORY', {})).get('url')
        )
        if not successUrl:
            successUrl = reverse('registration')

        # If a specific amount to pay has been passed, then allow payment
        # of that amount.
        if amount:
            try:
                amount = float(amount)
            except ValueError:
                logger.error('Invalid amount passed')
                return SquareCheckoutErrorResponse(
                    format_html(
                        '<p>{}</p><ul><li>{}</li></ul>',
                        str(_('ERROR: Error with Square checkout transaction attempt.')),
                        str(_('Invalid amount passed.'))
                    )
                )

        # Parse if a specific submission user is indicated
        submissionUser = None
        if submissionUserId:
            try:
                submissionUser = User.objects.get(id=int(submissionUserId))
            except (ValueError, ObjectDoesNotExist):
                logger.warning('Invalid user passed, submissionUser will not be recorded.')

        try:
            # Invoice transactions are usually payment on an existing invoice,
            # including registrations.
            if invoice_id:
                this_invoice = Invoice.objects.get(id=invoice_id)
                if this_invoice.status == Invoice.PaymentStatus.preliminary:
                    this_invoice.expirationDate = timezone.now() + timedelta(
                        minutes=getConstant('registration__sessionExpiryMinutes')
                    )
                this_invoice.status = Invoice.PaymentStatus.unpaid
                this_description = _('Invoice Payment: %s' % this_invoice.id)
                if not amount:
                    amount = this_invoice.outstandingBalance
                this_invoice.save()
            # All other transactions require both a transaction type and an amount to be specified
            elif not transactionType or not amount:
                logger.error('Insufficient information passed to createSquarePayment view.')
                return SquareCheckoutErrorResponse(
                    format_html(
                        '<p>{}</p><ul><li>{}</li></ul>',
                        str(_('ERROR: Error with Square checkout transaction attempt.')),
                        str(_('Insufficient information passed to createSquarePayment view.'))
                    )
                )
            else:
                # Gift certificates automatically get a nicer invoice description
                if transactionType == 'Gift Certificate':
                    this_description = _('Gift Certificate Purchase')
                else:
                    this_description = transactionType
                this_invoice = Invoice.create_from_item(
                    float(amount),
                    this_description,
                    submissionUser=submissionUser,
                    calculate_taxes=(taxable is not False),
                    transactionType=transactionType,
                    status=Invoice.PaymentStatus.unpaid,
                )
        except (ValueError, ObjectDoesNotExist) as e:
            logger.error(
                'Invalid invoice/amount information passed to createSquarePayment ' +
                'view: (%s, %s)' % (invoice_id, amount)
            )
            return SquareCheckoutErrorResponse(
                format_html(
                    '<p>{}</p><ul><li>{}</li></ul>',
                    str(_('ERROR: Error with Square checkout transaction attempt.')),
                    str(_(
                        'Invalid invoice/amount information passed to ' +
                        'createSquarePayment view: (%s, %s)' % (
                            invoice_id, amount
                        )
                    ))
                )
            )

        if this_invoice.status == Invoice.PaymentStatus.preliminary:
            this_invoice.status = Invoice.PaymentStatus.unpaid

        this_currency = getConstant('general__currencyCode')
        this_total = min(this_invoice.outstandingBalance, amount)

        client = api_client

        body = {
            'source_id': data.get('sourceId'),
            'location_id': getattr(settings, 'SQUARE_LOCATION_ID', ''),
            'idempotency_key': data.get('idempotency_key', str(uuid.uuid1())),
            'amount_money': {
                'amount': int(100 * this_total),
                'currency': this_currency,
            },
        }

        try:
            response = client.payments.create(**body)
        except ApiError as e:
            logger.error('Error in charging Square transaction: %s' % e.errors)

            this_invoice.status = Invoice.PaymentStatus.error
            this_invoice.save()
            errors_string = ''
            for err in e.errors:
                errors_string += '<li><strong>{}:</strong> {}</li>'.format(
                    err.code, err.detail
                )
            return SquareCheckoutErrorResponse(
                format_html(
                    '<p>{}</p><ul>{}</ul>',
                    str(_('ERROR: Error with Square checkout transaction attempt.')),
                    format_html(errors_string),
                )
            )
        else:
            logger.info('Square charge successfully created.')

        payment = response.dict().get('payment', {})

        paymentRecord = SquarePaymentRecord.objects.create(
            invoice=this_invoice,
            orderId=payment.get('order_id'),
            paymentId=payment.get('id'),
            locationId=payment.get('location_id'),
        )

        # We process the payment now, and enqueue the job to retrieve the
        # transaction again once fees have been calculated by Square
        this_invoice.processPayment(
            amount=this_total,
            fees=0,
            paidOnline=True,
            methodName='Square Checkout',
            methodTxn=payment.get('id'),
            notify=customerEmail,
        )
        clear_reg_cart(request)
        updateSquareFees.schedule(args=(paymentRecord.pk, ), delay=60)

        if addSessionInfo:
            paymentSession = request.session.get(PAYMENT_VALIDATION_STR, {})

            paymentSession.update({
                'invoiceID': str(this_invoice.id),
                'amount': this_total,
                # When finalSuccessUrl is present in the request (gift certificate flow),
                # successUrl in this flow is the customization page — not a safe post-form
                # destination. Use finalSuccessUrl instead, falling back to registration.
                'successUrl': (finalSuccessUrl or reverse('registration')) if 'finalSuccessUrl' in data else successUrl,
            })
            request.session[PAYMENT_VALIDATION_STR] = paymentSession

        return HttpResponseRedirect(successUrl)


class ProcessPointOfSalePaymentView(View):
    '''
    This view handles the callbacks from point-of-sale transactions.
    Please note that this will only work if you have set up your callback
    URL in Square to point to this view.
    '''

    def getPayment(self, request, serverTransId=None, clientTransId=None):

        logger.debug(f'Received callback with identifiers. Server: {serverTransId} Client: {clientTransId}')

        if not serverTransId and not clientTransId:
            logger.error('An unknown error has occurred with Square point of sale transaction attempt.')
            messages.error(
                self.request,
                _('ERROR: An unknown error has occurred with Square point of sale transaction attempt.'),
                extra_tags='square-error'
            )
            return

        location_id = getattr(settings, 'SQUARE_LOCATION_ID', '')
        client = api_client

        payment = None

        if serverTransId:
            # Added to avoid errors associated with Square API not being up to date.
            sleep(1)
            try:
                response = client.v1transactions.v1retrieve_order(
                    order_id=serverTransId, location_id=location_id
                )
                response_key = 'transaction'
            except ApiError as e:
                try:
                    response = client.orders.get(serverTransId)
                    response_key = 'order'
                except ApiError as e2:
                    response = None
                    response_key = 'error'
                    logger.error('Unable to find Square transaction for %s by server ID: %s' % (
                        serverTransId, e2.errors
                    ))
                    messages.error(
                        request,
                        str(_('ERROR: Unable to find Square transaction for {} by server ID: '.format(serverTransId))) +
                        str(e2.errors),
                        extra_tags='square-error'
                    )
            
            if response_key != 'error':
                payment_list = [x.get('id') for x in response.dict().get(response_key, {}).get('tenders', [])]
                if len(payment_list) == 1:
                    payment = client.payments.get(payment_list[0]).dict().get('payment')
                    logger.debug(f'Successfully retrieved payment based on server transaction identifier {serverTransId}')
                else:
                    logger.error('Returned client transaction ID not found.')
                    messages.error(
                        request, _('ERROR: Returned client transaction ID not found.'),
                        extra_tags='square-error'
                    )

        if clientTransId and not payment:
            # Try to find the payment in the 50 most recent payments
            try:
                response = client.payments.list(location_id=location_id)
            except ApiError as e:
                logger.error('Unable to find Square transaction for %s by client ID: %s' % (
                    location_id, e.errors
                ))
                messages.error(
                    request,
                    str(_('ERROR: Unable to find Square transaction by client ID:' )) +
                    str(e.errors),
                    extra_tags='square-error'
                )
            else:
                payment_list = [x for x in response if x.order_id == clientTransId]
                if len(payment_list) == 1:
                    payment = payment_list[0].dict()
                    logger.debug(f'Successfully retrieved payment based on client transaction identifier {clientTransId}')
                else:
                    logger.error('Returned client transaction ID not found.')
                    messages.error(
                        request, _('ERROR: Returned client transaction ID not found.'),
                        extra_tags='square-error'
                    )

        return payment


    def get(self, request, *args, **kwargs):
        # iOS transactions put all response information in the data key:
        data = json.loads(request.GET.get('data', '{}'))
        if data:
            logger.debug(f'Square Point-of-sale request data: {data}')
            status = data.get('status')
            errorCode = data.get('error_code')
            errorDescription = errorCode

            try:
                stateData = data.get('state', '')
                if stateData:
                    metadata = json.loads(b64decode(unquote(stateData).encode()).decode())
                else:
                    metadata = {}
            except (TypeError, ValueError, binascii.Error):
                logger.error('Invalid metadata passed from Square app.')
                messages.error(
                    request,
                    format_html(
                        '<p>{}</p><ul><li><strong>CODE:</strong> {}</li><li><strong>DESCRIPTION:</strong> {}</li></ul>',
                        str(_('ERROR: Error with Square point of sale transaction attempt.')),
                        str(_('Invalid metadata passed from Square app.')),
                    ),
                    extra_tags='square-error'
                )
                return HttpResponseRedirect(reverse('showRegSummary'))

            # This is the normal transaction identifier, which will be stored in the
            # database as a SquarePaymentRecord
            serverTransId = data.get('transaction_id')

            # This is the only identifier passed for non-card transactions.
            clientTransId = data.get('client_transaction_id')
        else:
            logger.debug(f'Square Point-of-sale request GET: {request.GET}')
            # Android transactions use this GET response syntax
            errorCode = request.GET.get('com.squareup.pos.ERROR_CODE')
            errorDescription = request.GET.get('com.squareup.pos.ERROR_DESCRIPTION')
            status = 'ok' if not errorCode else 'error'

            # This is the normal transaction identifier, which will be stored in the
            # database as a SquarePaymentRecord
            serverTransId = request.GET.get('com.squareup.pos.SERVER_TRANSACTION_ID')

            # This is the only identifier passed for non-card transactions.
            clientTransId = request.GET.get('com.squareup.pos.CLIENT_TRANSACTION_ID')

            # Load the metadata, which includes the registration or invoice ids
            try:
                stateData = request.GET.get('com.squareup.pos.REQUEST_METADATA', '')
                if stateData:
                    metadata = json.loads(b64decode(unquote(stateData).encode()).decode())
                else:
                    metadata = {}

            except (TypeError, ValueError, binascii.Error):
                logger.error('Invalid metadata passed from Square app.')
                messages.error(
                    request,
                    format_html(
                        '<p>{}</p><ul><li><strong>CODE:</strong> {}</li><li><strong>DESCRIPTION:</strong> {}</li></ul>',
                        str(_('ERROR: Error with Square point of sale transaction attempt.')),
                        str(_('Invalid metadata passed from Square app.')),
                    ),
                    extra_tags='square-error'
                )
                return HttpResponseRedirect(reverse('showRegSummary'))

        # Other things that can be passed in the metadata
        sourceUrl = metadata.get('sourceUrl', reverse('showRegSummary'))
        submissionUserId = metadata.get('userId', getattr(getattr(request, 'user', None), 'id', None))
        transactionType = metadata.get('transaction_type')
        taxable = metadata.get('taxable', False)
        addSessionInfo = metadata.get('addSessionInfo', False)
        customerEmail = metadata.get('customerEmail')

        # Send users back to the invoice to confirm the successful payment.
        # If none is specified, then return to the registration page.
        successUrl = (
            getReturnPage(request.session.get('SITE_HISTORY', {})).get('url') or
            metadata.get('successUrl') or
            reverse('registration')
        )

        if errorCode or status != 'ok':
            # Return the user to their original page with the error message displayed.
            logger.error(
                'Error with Square point of sale transaction attempt.  ' +
                'CODE: %s; DESCRIPTION: %s' % (errorCode, errorDescription)
            )
            messages.error(
                request,
                format_html(
                    '<p>{}</p><ul><li><strong>CODE:</strong> {}</li><li><strong>DESCRIPTION:</strong> {}</li></ul>',
                    str(_('ERROR: Error with Square point of sale transaction attempt.')), errorCode, errorDescription
                ),
                extra_tags='square-error'
            )
            return HttpResponseRedirect(sourceUrl)

        # Use the Square API to get the payment based on the passed identifiers.
        payment = self.getPayment(request, serverTransId, clientTransId)
        if not payment:
            return HttpResponseRedirect(sourceUrl)

        # Get total information from the transaction for handling invoice.
        this_total = (
            payment.get('amount_money', {}).get('amount', 0) / 100 -
            payment.get('refunded_money', {}).get('amount', 0) / 100
        )

        # Parse if a specific submission user is indicated
        submissionUser = None
        if submissionUserId:
            try:
                submissionUser = User.objects.get(id=int(submissionUserId))
            except (ValueError, ObjectDoesNotExist):
                logger.warning('Invalid user passed, submissionUser will not be recorded.')

        if 'invoice' in metadata.keys():
            try:
                this_invoice = Invoice.objects.get(id=metadata.get('invoice'))
                this_description = _('Invoice Payment: %s' % this_invoice.id)

                if this_invoice.status == Invoice.PaymentStatus.preliminary:
                    this_invoice.expirationDate = timezone.now() + timedelta(
                        minutes=getConstant('registration__sessionExpiryMinutes')
                    )
                this_invoice.status = Invoice.PaymentStatus.unpaid
                this_invoice.save()

            except (ValueError, TypeError, ObjectDoesNotExist):
                logger.error('Invalid invoice ID passed: %s' % metadata.get('invoice'))
                messages.error(
                    request,
                    str(_('ERROR: Invalid invoice ID passed')) + ': %s' % metadata.get('invoice'),
                    extra_tags='square-error'
                )
                return HttpResponseRedirect(sourceUrl)
        else:
            # Gift certificates automatically get a nicer invoice description
            if transactionType == 'Gift Certificate':
                this_description = _('Gift Certificate Purchase')
            else:
                this_description = transactionType
            this_invoice = Invoice.create_from_item(
                this_total,
                this_description,
                submissionUser=submissionUser,
                calculate_taxes=(taxable is not False),
                transactionType=transactionType,
                status=Invoice.PaymentStatus.unpaid,
            )

        paymentRecord, created = SquarePaymentRecord.objects.get_or_create(
            paymentId=payment.get('id'), orderId=payment.get('order_id'),
            locationId=payment.get('location_id'),
            defaults={'invoice': this_invoice, }
        )
        if created:
            # We process the payment now, and enqueue the job to retrieve the
            # transaction again once fees have been calculated by Square
            this_invoice.processPayment(
                amount=this_total,
                fees=0,
                paidOnline=True,
                methodName='Square Point of Sale',
                methodTxn=payment.get('id'),
                notify=customerEmail,
            )
            clear_reg_cart(request)
        updateSquareFees.schedule(args=(paymentRecord.pk, ), delay=60)

        if addSessionInfo:
            paymentSession = request.session.get(PAYMENT_VALIDATION_STR, {})

            paymentSession.update({
                'invoiceID': str(this_invoice.id),
                'amount': this_total,
                'successUrl': successUrl,
            })
            request.session[PAYMENT_VALIDATION_STR] = paymentSession

        return HttpResponseRedirect(successUrl)


class ViewOrCreateInvoiceView(PermissionRequiredMixin, UpdateView):
    '''
    This view allows for the creation of an invoice for a SquarePaymentRecord
    if one does not already exist.
    '''
    model = SquarePaymentRecord
    form_class = CreateInvoiceForm
    permission_required = 'core.add_invoice'
    template_name = 'square/create_invoice.html'

    def dispatch(self, request, *args, **kwargs):
        '''
        Get the payment record being requested. If it already has an invoice,
        then just redirect to view the invoice.
        '''
        self.object = self.get_object()
        if self.object.invoice:
            change_url = reverse('viewInvoice', args=(self.object.invoice.id, ))
            return HttpResponseRedirect(
                f'{change_url}?v={self.object.invoice.validationString}'
            )
        return super().dispatch(request, *args, **kwargs)

    def payment_unavailable(self, request):
        '''
        The invoice is built from the Square payment data.  If there is none
        saved and the API cannot provide it, say why and go back to the list.
        '''
        record = self.get_object()
        if record.getPayment(fail_silently=True):
            return None
        messages.error(request, _(
            'Cannot create an invoice for this payment: its details are not saved locally and '
            'could not be retrieved from Square (%(reason)s).'
        ) % {'reason': record.api_error or _('no payment data')})
        return HttpResponseRedirect(reverse('admin:square_squarepaymentrecord_changelist'))

    def get(self, request, *args, **kwargs):
        return self.payment_unavailable(request) or super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.payment_unavailable(request) or super().post(request, *args, **kwargs)

    def form_valid(self, form):
        '''
        Create the invoice and return to the SquarePaymentRecord changelist view.
        '''
        cleaned_data = form.cleaned_data

        new_invoice = Invoice.objects.create(
            email=self.object.payerEmail,
            comments=cleaned_data.get('comments'),
            submissionUser=self.request.user,
            buyerPaysSalesTax=getConstant('registration__buyerPaysSalesTax'),
            status=Invoice.PaymentStatus.preliminary
        )

        line_items = self.object.orderLineItems
        if not line_items:
            # If there are no order line items, just use the payment totals to
            # create a single item.
            InvoiceItem.objects.create(
                invoice=new_invoice,
                description=_('Square payment'),
                grossTotal=self.object.grossAmountPaid,
                total=self.object.grossAmountPaid,
                adjustments=-1*self.object.netRefund,
                fees=self.object.netFees,
            )
        else:
            # This is used to allocate fees across line items.
            line_item_total = sum([
                item.get('total_money',{}).get('amount', 0) / 100
                for item in line_items
            ])

            for item in line_items:
                quantity = int(item.get('quantity', 1))
                for n in range(quantity):
                    this_total = item.get('total_money',{}).get('amount', 0) / (100*quantity)

                    ii = InvoiceItem.objects.create(
                        invoice=new_invoice,
                        description=item.get('name', item.get('item_type', _('Square payment'))),
                        grossTotal=item.get('gross_sales_money',{}).get('amount', 0) / (100*quantity),
                        total=this_total,
                        taxes=item.get('total_tax_money',{}).get('amount', 0) / (100*quantity),
                        adjustments=-1*self.object.netRefund*(this_total / line_item_total),
                        fees=self.object.netFees*(this_total / line_item_total)
                    )

                    # Update the revenue item that has been created alongside
                    # the invoice item.
                    if hasattr(ii, 'revenueitem'):
                        ii.revenueitem.update(
                            category=getConstant('financial__doorPaymentRevenueCat'),
                            description=ii.description,
                            event=cleaned_data.get('event'),
                            paymentMethod='Square App',
                            receivedDate=self.object.apiPaymentCreated,
                        )
        # Now that invoice items have been created, update the invoice totals to
        # match the sum of item totals.
        new_invoice.status = Invoice.PaymentStatus.paid
        new_invoice.save()
        new_invoice.updateTotals()
        
        self.object.invoice = new_invoice
        self.object.save()

        return HttpResponseRedirect(
            reverse('admin:square_squarepaymentrecord_changelist')
        )
