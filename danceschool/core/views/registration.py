import re

from django.urls import reverse
from django.core.exceptions import ObjectDoesNotExist
from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.views.generic import FormView, TemplateView
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.utils.dateparse import parse_datetime

import logging
from allauth.account.forms import LoginForm, SignupForm
from datetime import timedelta

from ..models import (
    Event, Series, PublicEvent, Invoice, Customer, CashPaymentRecord,
    Registration
)
from ..forms.registration import (
    RegistrationContactForm, MultiRegCustomerNameForm, PartnerRequiredForm
)
from ..constants import getConstant, REG_VALIDATION_STR
from ..signals import (
    post_student_info, apply_discount, apply_price_adjustments, check_voucher
)
from ..mixins import (
    FinancialContextMixin, EventOrderMixin, SiteHistoryMixin,
    RegistrationAdjustmentsMixin
)

# Define logger for this file
logger = logging.getLogger(__name__)


def clear_reg_cart(request):
    '''
    Remove only the 'cart' key from the registration session, leaving all
    other session state (invoice_id, payAtDoor, link_authorized, etc.) intact.
    '''
    reg_session = request.session.get(REG_VALIDATION_STR)
    if reg_session and 'cart' in reg_session:
        del reg_session['cart']
        request.session.modified = True


class RegistrationOfflineView(TemplateView):
    '''
    If registration is offline, just say so.
    '''
    template_name = 'core/registration/registration_offline.html'


class PublicRegisterView(
    FinancialContextMixin, EventOrderMixin, SiteHistoryMixin, TemplateView
):
    '''
    Public-facing registration page backed by a CMS alias placeholder
    ('public_register_content').  Unlike PointOfSaleRegisterView, this requires no
    special permissions and respects the registration__registrationEnabled
    site setting.  Staff with core.accept_door_payments can still access
    the page when registration is disabled, and may toggle a door-registration
    checkbox (ephemeral, client-side only) to enable payAtDoor mode.

    Referral/voucher codes are supported via the ?referral= query parameter or
    the registration/referral/<voucher_id>/ URL pattern.  Valid codes are stored
    directly in the cart session data as discount_code; invalid codes produce a
    warning message.  Marketing IDs (?id= or registration/id/<marketing_id>/) are
    stored in session data for later use.
    '''
    template_name = 'core/public_register.html'

    def get_allEvents(self):
        if not hasattr(self, 'allEvents'):
            self.allEvents = Event.objects.filter(
                Q(instance_of=PublicEvent) |
                Q(instance_of=Series)
            ).annotate(
                **self.get_annotations()
            ).exclude(
                Q(status=Event.RegStatus.hidden) |
                Q(status=Event.RegStatus.regHidden) |
                Q(status=Event.RegStatus.linkOnly)
            ).order_by(*self.get_ordering()).distinct()
        return self.allEvents

    def get(self, request, *args, **kwargs):
        voucher_id = kwargs.pop('voucher_id', None)
        marketing_id = kwargs.pop('marketing_id', None)

        # GET parameters are also usable, but URL kwargs take precedence.
        if not voucher_id:
            voucher_id = request.GET.get('referral', None)
        if not marketing_id:
            marketing_id = request.GET.get('id', None)

        # Ignore IDs that contain disallowed characters.
        pattern = re.compile(r'^[a-zA-Z\-_0-9]+$')
        if voucher_id and not pattern.match(voucher_id):
            voucher_id = None
        if marketing_id and not pattern.match(marketing_id):
            marketing_id = None

        if marketing_id:
            reg_session = request.session.setdefault(REG_VALIDATION_STR, {})
            reg_session['marketing_id'] = marketing_id
            request.session.modified = True

        if voucher_id:
            responses = check_voucher.send(
                sender=self.__class__,
                voucherId=voucher_id,
                cart_items=[],
                customer=None,
                validateCustomer=False,
                invoice=None,
                payAtDoor=False,
            )
            results = [r[1] for r in responses if len(r) > 1 and r[1]]
            result = results[0] if results else {}

            if result.get('status') == 'valid':
                reg_session = request.session.setdefault(REG_VALIDATION_STR, {})
                reg_session.setdefault('cart', {})['discount_code'] = voucher_id
                request.session.modified = True
            else:
                errors = result.get('errors', [])
                error_detail = errors[0].get('message', '') if errors else ''
                messages.warning(
                    request,
                    _(
                        'The voucher code "%(code)s" is not valid.%(detail)s'
                    ) % {
                        'code': voucher_id,
                        'detail': ' ' + str(error_detail) if error_detail else '',
                    }
                )

        return super().get(request, *args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        if (
            not getConstant('registration__registrationEnabled') and
            not request.user.has_perm('core.accept_door_payments')
        ):
            return redirect('registrationOffline')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = {
            'allEvents': self.get_allEvents(),
            'showDescriptionRule': getConstant('registration__showDescriptionRule') or 'all',
            'registrationEnabled': getConstant('registration__registrationEnabled'),
        }
        context.update(kwargs)
        self.set_return_page('registration', pageName=_('Registration'))
        return super().get_context_data(**context)


class RegistrationSummaryView(
    FinancialContextMixin, RegistrationAdjustmentsMixin, SiteHistoryMixin, TemplateView
):
    template_name = 'core/registration_summary.html'

    # Ensures that customer-specific discounts are applied at this stage
    customers_final = True

    def dispatch(self, request, *args, **kwargs):
        ''' Always check that the temporary registration has not expired '''
        regSession = self.request.session.get(REG_VALIDATION_STR, {})

        if not regSession:
            return HttpResponseRedirect(reverse('registration'))

        try:
            invoice = Invoice.objects.get(
                id=self.request.session[REG_VALIDATION_STR].get('invoice_id')
            )
        except ObjectDoesNotExist:
            messages.error(request, _('Invalid invoice identifier passed to summary view.'))
            return HttpResponseRedirect(reverse('registration'))

        expiry = parse_datetime(
            self.request.session[REG_VALIDATION_STR].get('invoice_expiry', ''),
        )
        if not expiry or expiry < timezone.now():
            clear_reg_cart(request)
            messages.info(request, _('Your registration session has expired. Please try again.'))
            return HttpResponseRedirect(reverse('registration'))

        reg = Registration.objects.filter(invoice=invoice).first()

        # If OK, pass the registration and proceed
        kwargs.update({
            'reg': reg,
            'invoice': invoice,
        })
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        reg = kwargs.get('reg')
        invoice = kwargs.get('invoice')

        discount_codes = None
        total_discount_amount = 0
        addons = []

        voucherId = invoice.data.get('discount_code') or invoice.data.get('gift')

        if reg:
            discount_codes, total_discount_amount, voucherId = self.getDiscounts(
                invoice, registration=reg, voucher_code=voucherId
            )
            addons = self.getAddons(invoice, reg)

            for discount in discount_codes:
                apply_discount.send(
                    sender=RegistrationSummaryView,
                    discount=discount.code,
                    discount_amount=discount.discount_amount,
                    registration=reg,
                )

        # The return value to this signal should contain any adjustments that
        # need to be made to the price (e.g., from vouchers if the voucher app
        # is installed)
        adjustment_responses = apply_price_adjustments.send(
            sender=RegistrationSummaryView,
            invoice=invoice,
            registration=reg,
            prior_adjustment=-1*total_discount_amount,
        )

        combined_response = {
            'total_pretax': 0,
            'total_posttax': 0,
            'items': [],
        }

        for response in adjustment_responses:
            combined_response['total_pretax'] += response[1].get('total_pretax', 0)
            combined_response['total_posttax'] += response[1].get('total_posttax', 0)
            combined_response['items'] += response[1].get('items', [])

        # The updateTotals method allocates the total adjustment across the
        # invoice items, and also recalculates the taxes for each item.
        invoice.updateTotals(
            allocateAmounts={
                'total': -1*(combined_response['total_pretax'] + total_discount_amount),
                'adjustments': -1*(combined_response['total_posttax']),
            },
            save=True,
        )

        # Update the session key to keep track of this registration
        regSession = request.session[REG_VALIDATION_STR]
        regSession["temp_invoice_id"] = invoice.id.__str__()
        if reg:
            regSession["temp_reg_id"] = reg.id
            regSession['addons'] = addons
            regSession['total_discount_amount'] = total_discount_amount
            if discount_codes:
                regSession['discount_codes'] = [
                    (x.code.name, x.code.pk, x.discount_amount) for x in discount_codes
                ]

        regSession['vouchers'] = combined_response
        request.session[REG_VALIDATION_STR] = regSession

        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ''' Pass the initial kwargs, then update with the needed registration info. '''
        context_data = super().get_context_data(**kwargs)

        regSession = self.request.session[REG_VALIDATION_STR]
        invoice_id = regSession["temp_invoice_id"]
        invoice = Invoice.objects.get(id=invoice_id)

        reg_id = regSession.get("temp_reg_id",None)
        if reg_id:
            reg = Registration.objects.get(id=reg_id)
        else:
            reg = None

        discount_codes = regSession.get('discount_codes', None)
        discount_amount = regSession.get('total_discount_amount', 0)
        vouchers = regSession.get('vouchers', {})
        addons = regSession.get('addons', [])

        zeroBalance = (invoice.outstandingBalance == 0)
        isComplete = (zeroBalance or regSession.get('direct_payment', False) is True)

        if isComplete:
            # Include the submission user if the user is authenticated
            if self.request.user.is_authenticated:
                submissionUser = self.request.user
            else:
                submissionUser = None

            if zeroBalance:
                invoice.processPayment(
                    amount=0, fees=0, submissionUser=submissionUser,
                    forceFinalize=True
                )
            else:
                amountPaid = invoice.total
                paymentMethod = regSession.get('direct_payment_method', 'Cash')

                this_cash_payment = CashPaymentRecord.objects.create(
                    invoice=invoice, amount=amountPaid,
                    status=CashPaymentRecord.PaymentStatus.collected,
                    paymentMethod=paymentMethod,
                    payerEmail=invoice.email,
                    submissionUser=submissionUser,
                    collectedByUser=submissionUser,
                )
                invoice.processPayment(
                    amount=amountPaid, fees=0, paidOnline=False,
                    methodName=paymentMethod, submissionUser=submissionUser,
                    collectedByUser=submissionUser,
                    methodTxn='CASHPAYMENT_%s' % this_cash_payment.recordId,
                    forceFinalize=True,
                )
            if reg:
                # Ensures that the registration has a status that reflects the
                # payment that has been processed
                reg.refresh_from_db()

        context_data.update({
            'returnPage': self.get_return_page().get(
                'url', reverse('registration')
            ),
            'registration': reg,
            'invoice': invoice,
            "addonItems": addons,
            "discount_codes": discount_codes,
            "discount_code_amount": discount_amount,
            "vouchers": vouchers,
            "total_discount_amount": discount_amount + vouchers.get('total_pretax', 0),
            "total_adjustment_amount": vouchers.get('total_posttax', 0),
            "currencyCode": getConstant('general__currencyCode'),
            'payAtDoor': regSession.get('payAtDoor', False),
            'is_complete': isComplete,
            'zero_balance': zeroBalance,
        })

        return context_data


class PartnerRequiredView(RegistrationAdjustmentsMixin, FormView):
    '''
    When one or more events in a customer's registration have a partner
    required, this page is used to collect partner name information for each
    registrant to that event.
    '''
    form_class = PartnerRequiredForm
    template_name = 'core/partner_required_form.html'

    # Ensures that customer-specific discounts are applied at this stage
    customers_final = True

    def dispatch(self, request, *args, **kwargs):
        '''
        Require session data to be set to proceed, otherwise go back to step 1.
        Because they have the same expiration date, this also implies that the
        Registration object is not yet expired.
        '''
        if REG_VALIDATION_STR not in request.session:
            return HttpResponseRedirect(reverse('registration'))

        try:
            self.invoice = Invoice.objects.get(
                id=self.request.session[REG_VALIDATION_STR].get('invoice_id')
            )
        except ObjectDoesNotExist:
            messages.error(request, _('Invalid invoice identifier passed to sign-up form.'))
            return HttpResponseRedirect(reverse('registration'))

        expiry = parse_datetime(
            self.request.session[REG_VALIDATION_STR].get('invoice_expiry', ''),
        )
        if not expiry or expiry < timezone.now():
            clear_reg_cart(request)
            messages.info(request, _('Your registration session has expired. Please try again.'))
            return HttpResponseRedirect(reverse('registration'))

        if self.invoice.status != Invoice.PaymentStatus.preliminary:
            messages.error(request, _('The current invoice has already been submitted. Please try again.'))
            return HttpResponseRedirect(reverse('registration'))

        self.registration = Registration.objects.filter(
            invoice=self.invoice
        ).prefetch_related(
            'eventregistration_set', 'eventregistration_set__event',
            'eventregistration_set__customer'
        ).first()

        if not self.registration:
            messages.error(request, _('Invalid registration passed to additional customer name form.'))
            return HttpResponseRedirect(reverse('registration'))

        self.partnerRequiredRegs = self.registration.eventregistration_set.filter(
            event__partnerRequired=True
        )

        if not self.partnerRequiredRegs:
            return HttpResponseRedirect(self.get_success_url())

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context_data = super().get_context_data(**kwargs)
        reg = self.registration

        payAtDoor = self.request.session[REG_VALIDATION_STR].get('payAtDoor', False)

        # Get a voucher ID to check from the current contents of the form
        voucherId = self.invoice.data.get('discount_code') or self.invoice.data.get('gift')

        discount_codes, total_discount_amount, voucherId = self.getDiscounts(
            self.invoice, registration=reg, voucher_code=voucherId
        )
        addons = self.getAddons(self.invoice, reg)

        # Update totals (without saving anything), so that taxes are
        # recalculated and the invoice handler knows how much to apply.
        items_queryset = self.invoice.updateTotals(
            save=False, allocateAmounts={'total': -1*total_discount_amount}
        )

        if voucherId:
            context_data['voucher'] = self.getVoucher(voucherId, self.invoice)

            # Recalculate taxes, but do not save the invoice
            # with the updates, since the voucher will only be applied later.
            if context_data['voucher'].get('beforeTax'):
                allocateAmounts = {'total': -1*context_data['voucher'].get('voucherAmount', 0)}
            else:
                allocateAmounts = {'adjustments': -1*context_data['voucher'].get('voucherAmount', 0)}
            items_queryset = self.invoice.updateTotals(
                save=False, allocateAmounts=allocateAmounts,
                prior_queryset=items_queryset
            )

        context_data.update({
            'invoice': self.invoice,
            'currencySymbol': getConstant('general__currencySymbol'),
            'reg': reg,
            'payAtDoor': payAtDoor,
            'addonItems': addons,
            'discount_codes': discount_codes,
            'discount_code_amount': total_discount_amount,
        })

        return context_data

    def get_form_kwargs(self, **kwargs):
        ''' Pass along the request data to the form '''
        kwargs = super().get_form_kwargs(**kwargs)
        kwargs['partnerRequiredRegs'] = self.partnerRequiredRegs
        return kwargs

    def get_success_url(self):
        return reverse('showRegSummary')

    def form_valid(self, form):
        '''
        Even if this form is valid, the handlers for this form may have added messages
        to the request.  In that case, then the page should be handled as if the form
        were invalid.  Otherwise, update the session data with the form data and then
        move to the next view
        '''

        # The session expires after a period of inactivity that is specified in preferences.
        expiry = timezone.now() + timedelta(minutes=getConstant('registration__sessionExpiryMinutes'))
        self.request.session[REG_VALIDATION_STR]["invoice_expiry"] = \
            expiry.strftime('%Y-%m-%dT%H:%M:%S%z')
        self.request.session.modified = True

        for er in self.partnerRequiredRegs:
            partner_data = {
                'firstName': form.cleaned_data.pop('er_%s_partner_firstName' % er.id),
                'lastName': form.cleaned_data.pop('er_%s_partner_lastName' % er.id)
            }

            customerId = form.cleaned_data.pop('er_%s_partner_customerId' % er.id, None)
            if not Customer.objects.filter(id=customerId).exists():
                customerId = None

            if customerId:
                partner_data['customerId'] = customerId

            er.data['partner'] = partner_data
            er.save()

        return HttpResponseRedirect(self.get_success_url())  # Redirect after POST


class MultiRegCustomerNameView(RegistrationAdjustmentsMixin, FormView):
    '''
    This page collects additional name and email information needed when there
    are multiple EventRegistrations associated with an Invoice.  For each

    '''
    form_class = MultiRegCustomerNameForm
    template_name = 'core/multireg_customer_name_form.html'

    def dispatch(self, request, *args, **kwargs):
        '''
        Require session data to be set to proceed, otherwise go back to step 1.
        Because they have the same expiration date, this also implies that the
        Registration object is not yet expired.
        '''
        if REG_VALIDATION_STR not in request.session:
            return HttpResponseRedirect(reverse('registration'))

        try:
            self.invoice = Invoice.objects.get(
                id=self.request.session[REG_VALIDATION_STR].get('invoice_id')
            )
        except ObjectDoesNotExist:
            messages.error(request, _('Invalid invoice identifier passed to sign-up form.'))
            return HttpResponseRedirect(reverse('registration'))

        expiry = parse_datetime(
            self.request.session[REG_VALIDATION_STR].get('invoice_expiry', ''),
        )
        if not expiry or expiry < timezone.now():
            clear_reg_cart(request)
            messages.info(request, _('Your registration session has expired. Please try again.'))
            return HttpResponseRedirect(reverse('registration'))

        if self.invoice.status != Invoice.PaymentStatus.preliminary:
            messages.error(request, _('The current invoice has already been submitted. Please try again.'))
            return HttpResponseRedirect(reverse('registration'))

        self.registration = Registration.objects.filter(
            invoice=self.invoice
        ).prefetch_related('eventregistration_set').first()

        self.multiReg = (
            self.registration and
            self.registration.eventregistration_set.filter(
                invoiceItem__parent_item__isnull=True
            ).count() > 1
        )

        if not self.registration or not self.multiReg:
            messages.error(request, _('Invalid registration passed to additional customer name form.'))
            return HttpResponseRedirect(reverse('registration'))

        self.partnerRequired = self.registration.eventregistration_set.filter(
            event__partnerRequired=True
        ).exists()

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context_data = super().get_context_data(**kwargs)
        reg = self.registration

        payAtDoor = self.request.session[REG_VALIDATION_STR].get('payAtDoor', False)

        # Get a voucher ID to check from the current contents of the form
        voucherId = self.invoice.data.get('discount_code') or self.invoice.data.get('gift')

        discount_codes, total_discount_amount, voucherId = self.getDiscounts(
            self.invoice, registration=reg, voucher_code=voucherId
        )
        addons = self.getAddons(self.invoice, reg)

        # Update totals (without saving anything), so that taxes are
        # recalculated and the invoice handler knows how much to apply.
        items_queryset = self.invoice.updateTotals(
            save=False, allocateAmounts={'total': -1*total_discount_amount}
        )

        if voucherId:
            context_data['voucher'] = self.getVoucher(voucherId, self.invoice)

            # Recalculate taxes, but do not save the invoice
            # with the updates, since the voucher will only be applied later.
            if context_data['voucher'].get('beforeTax'):
                allocateAmounts = {'total': -1*context_data['voucher'].get('voucherAmount', 0)}
            else:
                allocateAmounts = {'adjustments': -1*context_data['voucher'].get('voucherAmount', 0)}
            items_queryset = self.invoice.updateTotals(
                save=False, allocateAmounts=allocateAmounts,
                prior_queryset=items_queryset
            )

        context_data.update({
            'invoice': self.invoice,
            'currencySymbol': getConstant('general__currencySymbol'),
            'reg': reg,
            'payAtDoor': payAtDoor,
            'addonItems': addons,
            'discount_codes': discount_codes,
            'discount_code_amount': total_discount_amount,
        })

        return context_data

    def get_form_kwargs(self, **kwargs):
        ''' Pass along the request data to the form '''
        kwargs = super().get_form_kwargs(**kwargs)
        kwargs['request'] = self.request
        kwargs['registration'] = self.registration
        kwargs['invoice'] = self.invoice
        kwargs['multiReg'] = self.multiReg
        return kwargs

    def get_success_url(self):
        if self.partnerRequired:
            return reverse('partnerRequiredForm')
        return reverse('showRegSummary')

    def form_valid(self, form):
        '''
        Even if this form is valid, the handlers for this form may have added messages
        to the request.  In that case, then the page should be handled as if the form
        were invalid.  Otherwise, update the session data with the form data and then
        move to the next view
        '''

        # The session expires after a period of inactivity that is specified in preferences.
        expiry = timezone.now() + timedelta(minutes=getConstant('registration__sessionExpiryMinutes'))
        self.request.session[REG_VALIDATION_STR]["invoice_expiry"] = \
            expiry.strftime('%Y-%m-%dT%H:%M:%S%z')
        self.request.session.modified = True

        for er in self.registration.eventregistration_set.all():
            firstName = form.cleaned_data.pop('er_%s_firstName' % er.id)
            lastName = form.cleaned_data.pop('er_%s_lastName' % er.id)
            email = form.cleaned_data.pop('er_%s_email' % er.id)
            phone = form.cleaned_data.pop('er_%s_phone' % er.id, None)
            student = form.cleaned_data.pop('er_%s_student' % er.id, False)

            customer, created = Customer.objects.get_or_create_customer(
                firstName, lastName, email, phone=phone
            )
            er.customer = customer
            er.student = student
            er.data.update(form.cleaned_data)
            er.save()

        # This signal allows vouchers to be applied temporarily, and it can
        # be used for other tasks.  It is sent here because it has not been
        # sent in the StudentInfoView if we got here.
        post_student_info.send(
            sender=StudentInfoView, invoice=self.invoice,
            registration=self.registration
        )
        return HttpResponseRedirect(self.get_success_url())  # Redirect after POST


class StudentInfoView(RegistrationAdjustmentsMixin, FormView):
    '''
    This page displays a preliminary total of what is being signed up for, and it also
    collects customer information, either by having the user sign in in an Ajax view, or by
    manually entering the information.  When the form is submitted, the view just passes
    everything into the session data and continues on to the next step.  To add additional
    fields to this form, or to modify existing fields, just override the form class to
    a form that adds/modifies whatever fields you would like.
    '''
    form_class = RegistrationContactForm
    template_name = 'core/student_info_form.html'

    def dispatch(self, request, *args, **kwargs):
        '''
        Require session data to be set to proceed, otherwise go back to step 1.
        Because they have the same expiration date, this also implies that the
        Registration object is not yet expired.
        '''
        if REG_VALIDATION_STR not in request.session:
            return HttpResponseRedirect(reverse('registration'))

        try:
            self.invoice = Invoice.objects.get(
                id=self.request.session[REG_VALIDATION_STR].get('invoice_id')
            )
        except ObjectDoesNotExist:
            messages.error(request, _('Invalid invoice identifier passed to sign-up form.'))
            return HttpResponseRedirect(reverse('registration'))

        expiry = parse_datetime(
            self.request.session[REG_VALIDATION_STR].get('invoice_expiry', ''),
        )
        if not expiry or expiry < timezone.now():
            clear_reg_cart(request)
            messages.info(request, _('Your registration session has expired. Please try again.'))
            return HttpResponseRedirect(reverse('registration'))

        if self.invoice.status != Invoice.PaymentStatus.preliminary:
            messages.error(request, _('The current invoice has already been submitted. Please try again.'))
            return HttpResponseRedirect(reverse('registration'))

        self.registration = Registration.objects.filter(
            invoice=self.invoice
        ).prefetch_related('eventregistration_set').first()

        multiRegRule = getConstant('registration__multiRegNameFormRule')

        self.multiReg = (
            self.registration and
            self.registration.eventregistration_set.filter(
                invoiceItem__parent_item__isnull=True
            ).count() > 1 and
            (
                multiRegRule == 'Y' or
                (multiRegRule == 'O' and not self.registration.payAtDoor)
            )
        )

        self.partnerRequired = (
            self.registration and
            self.registration.eventregistration_set.filter(
                event__partnerRequired=True
            ).exists()
        )

        # Ensure that session data is always updated when this view is called
        # so that passed voucher_ids are cleared.
        request.session.modified = True

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context_data = super().get_context_data(**kwargs)
        reg = self.registration

        payAtDoor = self.request.session[REG_VALIDATION_STR].get('payAtDoor', False)

        # Get a voucher ID to check from the current contents of the form
        voucherId = getattr(context_data['form'].fields.get('gift'), 'initial', None)

        discount_codes, total_discount_amount, voucherId = self.getDiscounts(
            self.invoice, registration=reg, voucher_code=voucherId
        )
        addons = self.getAddons(self.invoice, reg)

        # Update totals (without saving anything), so that taxes are
        # recalculated and the invoice handler knows how much to apply.
        items_queryset = self.invoice.updateTotals(
            save=False, allocateAmounts={'total': -1*total_discount_amount}
        )

        if voucherId:
            context_data['voucher'] = self.getVoucher(voucherId, self.invoice)

            # Recalculate taxes, but do not save the invoice
            # with the updates, since the voucher will only be applied later.
            if context_data['voucher'].get('beforeTax'):
                allocateAmounts = {'total': -1*context_data['voucher'].get('voucherAmount', 0)}
            else:
                allocateAmounts = {'adjustments': -1*context_data['voucher'].get('voucherAmount', 0)}
            items_queryset = self.invoice.updateTotals(
                save=False, allocateAmounts=allocateAmounts,
                prior_queryset=items_queryset
            )

        if (
            payAtDoor or
            self.request.user.is_authenticated or not
            getConstant('registration__allowAjaxSignin')
        ):
            context_data['show_ajax_form'] = False
        else:
            # Add a login form and a signup form
            context_data.update({
                'show_ajax_form': True,
                'login_form': LoginForm(),
                'signup_form': SignupForm(),
            })

        context_data.update({
            'invoice': self.invoice,
            'currencySymbol': getConstant('general__currencySymbol'),
            'reg': reg,
            'payAtDoor': payAtDoor,
            'addonItems': addons,
            'discount_codes': discount_codes,
            'discount_code_amount': total_discount_amount,
            'is_multiple_registration': self.multiReg,
        })

        return context_data

    def get_initial(self):
        ''' The initial value of the student field can be populated from session data. '''
        return {'student': self.request.session[REG_VALIDATION_STR].get('student', False)}

    def get_form_kwargs(self, **kwargs):
        ''' Pass along the request data to the form '''
        kwargs = super().get_form_kwargs(**kwargs)
        kwargs['request'] = self.request
        kwargs['registration'] = self.registration
        kwargs['invoice'] = self.invoice
        kwargs['multiReg'] = self.multiReg
        from django.urls import NoReverseMatch
        try:
            kwargs['add_more_url'] = reverse('registration')
        except NoReverseMatch:
            kwargs['add_more_url'] = None
        return kwargs

    def get_success_url(self):
        if self.multiReg:
            return reverse('multiRegNameInfo')
        elif self.partnerRequired:
            return reverse('partnerRequiredForm')
        return reverse('showRegSummary')

    def form_valid(self, form):
        '''
        Even if this form is valid, the handlers for this form may have added messages
        to the request.  In that case, then the page should be handled as if the form
        were invalid.  Otherwise, update the session data with the form data and then
        move to the next view
        '''

        # The session expires after a period of inactivity that is specified in preferences.
        expiry = timezone.now() + timedelta(minutes=getConstant('registration__sessionExpiryMinutes'))
        self.request.session[REG_VALIDATION_STR]["invoice_expiry"] = \
            expiry.strftime('%Y-%m-%dT%H:%M:%S%z')
        self.request.session.modified = True

        # Notice that student and phone are not popped so that they go into the
        # Invoice data.
        firstName = form.cleaned_data.pop('firstName')
        lastName = form.cleaned_data.pop('lastName')
        email = form.cleaned_data.pop('email')
        phone = form.cleaned_data.get('phone', None)
        student = form.cleaned_data.get('student', False)

        reg = self.registration
        if reg:

            # Update the expiration date for this registration, and pass in the data from
            # this form.
            reg.comments = form.cleaned_data.pop('comments', None)
            reg.howHeardAboutUs = form.cleaned_data.pop('howHeardAboutUs', None)

            if not self.multiReg:
                customer, created = Customer.objects.get_or_create_customer(
                    firstName, lastName, email, phone=phone
                )
                reg.eventregistration_set.update(
                    customer=customer, student=student
                )

            invoice = reg.link_invoice(
                expirationDate=expiry, firstName=firstName, lastName=lastName,
                email=email, save=False
            )

            # Anything else in the form goes to the Invoice data.
            invoice.data.update(form.cleaned_data)
            invoice.save()
            reg.save()
        else:
            invoice = self.invoice

            if invoice.status == Invoice.PaymentStatus.preliminary:
                invoice.expirationDate = expiry

            invoice.firstName = firstName
            invoice.lastName = lastName
            invoice.email = email
            invoice.data.update(form.cleaned_data)
            invoice.save()

        # This signal allows vouchers to be applied temporarily, and it can
        # be used for other tasks.  We only send it here if we are not collecting
        # additional name information in the next step.
        if not self.multiReg:
            post_student_info.send(
                sender=StudentInfoView, invoice=invoice, registration=reg
            )
        return HttpResponseRedirect(self.get_success_url())  # Redirect after POST
