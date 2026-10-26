from django.http import HttpResponseRedirect
from django.core.exceptions import ObjectDoesNotExist
from django.contrib import messages
from django.contrib.messages.views import SuccessMessageMixin
from django.urls import reverse
from django.views.generic import UpdateView, TemplateView
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from braces.views import PermissionRequiredMixin, StaffuserRequiredMixin

from ..models import Invoice, Registration, EventRegistration
from ..forms.adjustments import RefundForm, RegistrationTransferForm
from ..constants import REFUND_VALIDATION_STR
from ..mixins import FinancialContextMixin, AdminSuccessURLMixin
from ..utils.timezone import ensure_localtime

import logging


logger = logging.getLogger(__name__)


def processor_error_detail(response):
    '''
    The error messages in a payment processor's refund response, as one
    string, for the message shown to staff.
    '''
    messages_ = []
    for error in response.get('errors') or []:
        if isinstance(error, dict):
            text = error.get('message') or error.get('detail') or error.get('code')
        else:
            text = str(error)
        if text:
            messages_.append(str(text))
    return '; '.join(messages_).rstrip('.') or str(_('unknown error'))


#################################
# Refund processing and confirmation step views


class RefundConfirmationView(FinancialContextMixin, AdminSuccessURLMixin, PermissionRequiredMixin,
                             StaffuserRequiredMixin, SuccessMessageMixin, TemplateView):
    success_message = _('Refund successfully processed.')
    template_name = 'core/refund_confirmation.html'
    permission_required = 'core.process_refunds'

    def get(self, request, *args, **kwargs):
        self.form_data = request.session.get(REFUND_VALIDATION_STR, {}).get('form_data', {})
        if not self.form_data:
            return HttpResponseRedirect(reverse('refundProcessing', args=(self.form_data.get('id'),)))

        try:
            self.invoice = Invoice.objects.get(id=self.form_data.get('id'))
        except ObjectDoesNotExist:
            return HttpResponseRedirect(reverse('refundProcessing', args=(self.form_data.get('id'),)))

        self.payments = self.invoice.get_payments()

        if request.GET.get('confirmed', '').lower() == 'true' and self.payments:
            return self.process_refund()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        total_refund_amount = self.form_data['total_refund_amount']
        initial_refund_amount = self.form_data['initial_refund_amount']
        amount_to_refund = max(total_refund_amount - initial_refund_amount, 0)

        context.update({
            'form_data': self.form_data,
            'payments': self.payments,
            'total_refund_amount': total_refund_amount,
            'initial_refund_amount': initial_refund_amount,
            'amount_to_refund': amount_to_refund,
        })
        return context

    def refund_failed(self, refund_data, failed, adjustment_amounts):
        '''
        Stop after a failed refund: report it, record it on the invoice, and
        apply only the refunds that already succeeded.
        '''
        logger.error(failed.get('errorMessage'))
        logger.error(failed)

        messages.error(self.request, failed.get('errorMessage'))

        self.invoice.data['refunds'] = refund_data

        total_applied = sum([x.get('refundAmount', 0) for x in refund_data if x.get('status') == 'success'])
        total_fees = sum([x.get('fees', 0) for x in refund_data if x.get('status') == 'success'])

        # Allocate whatever amount was previously successful across the
        # items for which the refund was requested.
        self.invoice.amountPaid -= total_applied

        self.invoice.updateTotals(
            save=True,
            allocateAmounts={
                'adjustments': -1*total_applied,
                'fees': total_fees,
            },
            allocateWeights=adjustment_amounts
        )
        self.request.session.pop(REFUND_VALIDATION_STR, None)
        return HttpResponseRedirect(self.get_success_url())

    def process_refund(self):

        refund_data = self.invoice.data.get('refunds', [])

        total_refund_amount = self.form_data['total_refund_amount']
        initial_refund_amount = self.form_data['initial_refund_amount']
        amount_to_refund = max(total_refund_amount - initial_refund_amount, 0)

        # Identify the items to which refunds should be allocated and update the adjustments line
        # for those items.  Fees are also allocated across the items for which the refund was requested.
        refund_items = self.form_data.items()
        item_refund_data = [(k.split('_')[2], v) for k, v in refund_items if k.startswith('item_refundamount_')]
        adjustment_amounts = {x[0]: float(x[1]) for x in item_refund_data}

        # Keep track of total refund fees as well as how much reamins to be
        # refunded as we iterate through payments to refund them.
        remains_to_refund = amount_to_refund

        for this_payment in self.payments:
            if remains_to_refund <= 0:
                break
            if not this_payment.refundable:
                continue

            this_payment_amount = this_payment.netAmountPaid

            # This dictionary will be updated and then added to refund_data for
            # this invoice whether the refund is successful or not
            this_refund_response_data = {
                'datetime': str(ensure_localtime(timezone.now())),
                'id': this_payment.recordId,
                'methodName': this_payment.methodName,
            }

            if this_payment_amount is None:
                # The payment processor could not say how much was paid (for
                # instance, its API is unavailable).  Do not guess: refund()
                # treats a missing amount as "refund everything remaining".
                reason = getattr(this_payment, 'api_error', None)
                this_refund_response_data.update({
                    'amount': 0,
                    'status': 'error',
                    'errorMessage': str(_(
                        'Could not determine the amount paid by %(method)s payment %(id)s%(reason)s; '
                        'no refund was attempted.'
                    ) % {
                        'method': this_payment.methodName,
                        'id': this_payment.recordId,
                        'reason': ' (%s)' % reason if reason else '',
                    }),
                })
                refund_data.append(this_refund_response_data)
                return self.refund_failed(refund_data, this_refund_response_data, adjustment_amounts)

            this_refund_amount = min(this_payment_amount, remains_to_refund)
            this_refund_response_data['amount'] = this_refund_amount
            this_refund_response = this_payment.refund(this_refund_amount)

            if not this_refund_response:
                # If no response is received, then we must stop because we cannot be sure that a refund has
                # not already been processed.
                this_refund_response_data.update({
                    'status': 'error',
                    'errorMessage': str(_('Error: No response from payment processing app. '
                                          'Check payment processor records for refund status.')),
                })
            elif this_refund_response[0].get('status').lower() == 'success':
                # A successful refund returns {'status': 'success'}
                amount_refunded = this_refund_response[0].get('refundAmount', 0)
                fees = this_refund_response[0].get('fees', 0)

                this_refund_response_data.update({
                    'status': 'success',
                    'refundAmount': amount_refunded,
                    'fees': fees,
                    'response': [dict(this_refund_response[0]), ],
                })

                remains_to_refund -= amount_refunded

            else:
                this_refund_response_data.update({
                    'status': 'error',
                    'errorMessage': str(
                        _('Error from the payment processor: %(detail)s. '
                          'Check payment processor records for refund status.')
                        % {'detail': processor_error_detail(this_refund_response[0])}
                    ),
                    'response': [dict(this_refund_response[0]), ],
                    'id': this_payment.recordId,
                    'methodName': this_payment.methodName,
                    'invoice': str(self.invoice.id),
                    'refundAmount': this_refund_amount,
                })
            refund_data.append(this_refund_response_data)

            if this_refund_response_data.get('status') == 'error':
                return self.refund_failed(refund_data, this_refund_response_data, adjustment_amounts)

        # If there were no errors, then check to ensure that the entire request refund was refunded.
        # If so, then return success, otherwise return indicating that the refund was not completely
        # applied.
        if abs(remains_to_refund) <= 0.01:
            messages.success(self.request, self.success_message)
        else:
            messages.error(
                self.request,
                _('Error, not all of the requested refund was applied. '
                  'Check invoice and payment processor records for details.')
                )

        self.invoice.data['refunds'] = refund_data

        total_applied = sum([x.get('refundAmount', 0) for x in refund_data if x.get('status') == 'success'])
        total_fees = sum([x.get('fees', 0) for x in refund_data if x.get('status') == 'success'])

        self.invoice.amountPaid -= total_applied

        if abs(
            self.invoice.total + (self.invoice.taxes * self.invoice.buyerPaysSalesTax) +
            self.invoice.adjustments - total_applied
        ) < 0.01:
            self.invoice.status = Invoice.PaymentStatus.fullRefund

        # Allocate whatever amount was previously successful across the
        # items for which the refund was requested.
        items = self.invoice.updateTotals(
            save=True,
            allocateAmounts={
                'adjustments': -1*total_applied,
                'fees': total_fees,
            },
            allocateWeights=adjustment_amounts
        )

        # If the refund is a complete refund and is associated with a registration,
        # then cancel the EventRegistration entirely.
        eventregs = EventRegistration.objects.filter(invoiceItem__in=items)
        for this_item in items:
            this_eventreg = eventregs.filter(invoiceItem=this_item).first()

            if (
                abs(
                    this_item.total + this_item.adjustments +
                    (this_item.taxes * self.invoice.buyerPaysSalesTax)
                ) < 0.01 and this_eventreg
            ):
                this_eventreg.cancelled = True
                this_eventreg.save()

        self.request.session.pop(REFUND_VALIDATION_STR, None)
        return HttpResponseRedirect(self.get_success_url())


class RefundProcessingView(FinancialContextMixin, PermissionRequiredMixin, StaffuserRequiredMixin, UpdateView):
    template_name = 'core/process_refund.html'
    form_class = RefundForm
    permission_required = 'core.process_refunds'
    model = Invoice

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'invoice': self.object,
            'payments': self.get_payments(),
        })
        if getattr(self.object, 'registration', None):
            context['registration'] = self.object.registration

        return context

    def form_valid(self, form):
        # Avoid JSON serialization issues by passing the Invoice ID, not the object itself
        clean_data = form.cleaned_data
        clean_data['id'] = str(clean_data.get('id').id)

        self.request.session[REFUND_VALIDATION_STR] = {
            'form_data': clean_data,
            'invoice': str(self.object.id),
        }
        return HttpResponseRedirect(reverse('refundConfirmation'))

    def get_payments(self):
        if not getattr(self, 'payments', None):
            self.payments = self.object.get_payments()
        return self.payments


class RegistrationTransferProcessingView(
    FinancialContextMixin, AdminSuccessURLMixin, PermissionRequiredMixin,
    StaffuserRequiredMixin, UpdateView
):
    template_name = 'core/process_transfer.html'
    form_class = RegistrationTransferForm
    permission_required = 'core.process_refunds'
    model = Registration

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'registration': self.object,
            'invoice': self.object.invoice,
        })
        return context

    def form_valid(self, form):
        clean_data = form.cleaned_data

        for er in self.object.eventregistration_set.all():
            old_customer = er.customer
            new_customer = clean_data.get('new_customer_%s' % er.id)

            old_event = er.event
            new_event = clean_data.get('new_event_%s' % er.id)

            new_role = clean_data.get('new_role_%s' % er.id)
            old_role = er.role

            customer_changed = (old_customer != new_customer) and new_customer
            event_changed = (old_event != new_event) and new_event
            role_changed = old_role != new_role

            # Only proceed if the event or role has changed.
            if not customer_changed and not event_changed and not role_changed:
                continue

            er.event = new_event

            if customer_changed:
                er.customer = new_customer

            availableRoles = new_event.availableRoles

            if new_role and new_role in availableRoles:
                er.role = new_role
            elif old_role and old_role not in availableRoles:
                er.role = None
            er.save()

            if event_changed or customer_changed:
                # Delete any event check-ins associated with the old event.
                er.eventcheckin_set.filter(event=old_event).delete()

            if event_changed:
                # Link any revenue items in the financial app to the correct event.
                linked_revenue = getattr(er.invoiceItem, 'revenueitem', None)
                if linked_revenue:
                    linked_revenue.event = new_event
                    linked_revenue.save()

        return HttpResponseRedirect(self.get_success_url())
