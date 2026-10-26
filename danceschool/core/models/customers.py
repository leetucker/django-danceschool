from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _
from collections import Counter
import logging

from ..mixins import EmailRecipientMixin

logger = logging.getLogger(__name__)

CUSTOMER_UNIQUE_CONSTRAINT = 'core_customer_unique_name_email'


class CustomerGroup(EmailRecipientMixin, models.Model):
    '''
    A customer group can be used to send emails and to define group-specific
    discounts and vouchers.
    '''
    name = models.CharField(_('Group name'), max_length=100)

    def memberCount(self):
        return self.customer_set.count()

    def get_default_recipients(self):
        ''' Overrides EmailRecipientMixin '''
        return [x.email for x in self.customer_set.all()]

    def __str__(self):
        return self.name

    class Meta:
        ordering = ('name',)
        verbose_name = _('Customer group')
        verbose_name_plural = _('Customer groups')


class CustomerManager(models.Manager):

    def get_or_create_customer(self, first_name, last_name, email, phone=None):
        '''
        Find the customer with this name and email, ignoring case and
        surrounding whitespace, or create one.  Returns (customer, created).

        All registration code should use this rather than get_or_create(), so
        that "jane doe <Jane@Example.com>" finds the existing "Jane Doe
        <jane@example.com>" instead of becoming a second customer.  If several
        existing customers match (case variants from before this matching was
        used), it never fails: it prefers an exact match, then one linked to a
        user account, then the oldest.  A phone number, if given, is saved; a
        missing one never blanks a stored one.
        '''
        first_name = (first_name or '').strip()
        last_name = (last_name or '').strip()
        email = (email or '').strip()

        def find():
            matches = list(self.filter(
                first_name__iexact=first_name, last_name__iexact=last_name, email__iexact=email,
            ).order_by('id'))
            if not matches:
                return None
            return min(matches, key=lambda c: (
                (c.first_name, c.last_name, c.email) != (first_name, last_name, email),
                c.user_id is None,
                c.id,
            ))

        customer = find()
        created = False
        if customer is None:
            try:
                with transaction.atomic(using=self.db):
                    customer = self.create(
                        first_name=first_name, last_name=last_name, email=email, phone=phone or None,
                    )
                created = True
            except IntegrityError:
                # Created by a concurrent request in the meantime.
                customer = find()
                if customer is None:
                    raise
        if not created and phone and customer.phone != phone:
            customer.phone = phone
            customer.save(update_fields=['phone'])
        return customer, created


class Customer(EmailRecipientMixin, models.Model):
    '''
    Not all customers choose to log in when they sign up for classes, and
    sometimes Users register their spouses, friends, or other customers.
    However, we still need to keep track of those customers' registrations.
    So, Customer objects are unique for each combination of name and email
    address, even though Users are unique by email address only.  Customers
    also store name and email information separately from the User object.
    Use Customer.objects.get_or_create_customer() to find or create one.
    '''
    objects = CustomerManager()

    user = models.OneToOneField(
        User, null=True, blank=True, verbose_name=_('User account'),
        on_delete=models.SET_NULL
    )

    first_name = models.CharField(_('First name'), max_length=30)
    last_name = models.CharField(_('Last name'), max_length=30)
    email = models.EmailField(_('Email address'))
    phone = models.CharField(_('Telephone'), max_length=20, null=True, blank=True)

    groups = models.ManyToManyField(
        CustomerGroup,
        verbose_name=_('Customer groups'), blank=True,
        help_text=_(
            'Customer groups may be used for group-specific discounts and ' +
            'vouchers, as well as for email purposes.'
        )
    )

    data = models.JSONField(_('Additional data'), default=dict, blank=True)

    @property
    def fullName(self):
        return ' '.join([self.first_name or '', self.last_name or ''])
    fullName.fget.short_description = _('Name')

    @property
    def numEventRegistrations(self):
        from .event_registration import EventRegistration
        return EventRegistration.objects.filter(
            customer=self, dropIn=False, cancelled=False,
            registration__final=True
        ).count()
    numEventRegistrations.fget.short_description = _('# Events/series registered')

    @property
    def numClassSeries(self):
        from .event_registration import EventRegistration
        return EventRegistration.objects.filter(
            customer=self, event__series__isnull=False,
            dropIn=False, cancelled=False, registration__final=True
        ).count()
    numClassSeries.fget.short_description = _('# Series registered')

    @property
    def numPublicEvents(self):
        from .event_registration import EventRegistration
        return EventRegistration.objects.filter(
            customer=self, event__publicevent__isnull=False,
            dropIn=False, cancelled=False,
            registration__final=True
        ).count()
    numPublicEvents.fget.short_description = _('# Public events registered')

    @property
    def numDropIns(self):
        from .event_registration import EventRegistration
        return EventRegistration.objects.filter(
            customer=self, dropIn=True, cancelled=False,
            registration__final=True
        ).count()
    numPublicEvents.fget.short_description = _('# Drop-ins registered')

    @property
    def firstSeries(self):
        from .event_registration import EventRegistration
        return EventRegistration.objects.filter(
            customer=self, event__series__isnull=False,
            dropIn=False, cancelled=False, registration__final=True
        ).order_by('event__startTime').first().event
    firstSeries.fget.short_description = _('Customer\'s first series')

    @property
    def firstSeriesDate(self):
        from .event_registration import EventRegistration
        return EventRegistration.objects.filter(
            customer=self, event__series__isnull=False,
            dropIn=False, cancelled=False, registration__final=True
        ).order_by('event__startTime').first().event.startTime
    firstSeriesDate.fget.short_description = _('Customer\'s first series date')

    @property
    def lastSeries(self):
        from .event_registration import EventRegistration
        return EventRegistration.objects.filter(
            customer=self, event__series__isnull=False,
            dropIn=False, cancelled=False, registration__final=True
        ).order_by('-event__startTime').first().event
    lastSeries.fget.short_description = _('Customer\'s most recent series')

    @property
    def lastSeriesDate(self):
        from .event_registration import EventRegistration
        return EventRegistration.objects.filter(
            customer=self, event__series__isnull=False,
            dropIn=False, cancelled=False, registration__final=True
        ).order_by('-event__startTime').first().event.startTime
    lastSeriesDate.fget.short_description = _('Customer\'s most recent series date')

    def getSeriesRegistered(self, q_filter=Q(), distinct=True, counter=False, **kwargs):
        '''
        Return a list that indicates each series the person has registered for
        and how many registrations they have for that series (because of couples).
        This can be filtered by any keyword arguments passed (e.g. year and month).
        '''
        from .event_types import Series
        series_set = Series.objects.filter(
            q_filter, eventregistration__customer=self,
            eventregistration__registration__final=True,
            **kwargs
        )

        if not distinct:
            return series_set
        elif distinct and not counter:
            return series_set.distinct()
        elif 'year' in kwargs or 'month' in kwargs:
            return [
                str(x[1]) + 'x: ' + x[0].classDescription.title for x in
                Counter(series_set).items()
            ]
        else:
            return [str(x[1]) + 'x: ' + x[0].__str__() for x in Counter(series_set).items()]

    def get_default_recipients(self):
        ''' Overrides EmailRecipientMixin '''
        return [self.email, ]

    def get_email_context(self, **kwargs):
        ''' Overrides EmailRecipientMixin '''
        context = super().get_email_context(**kwargs)
        context.update({
            'first_name': self.first_name,
            'last_name': self.last_name,
            'email': self.email,
            'fullName': self.fullName,
            'phone': self.phone,
        })
        return context

    def __str__(self):
        return '%s: %s' % (self.fullName, self.email)

    class Meta:
        constraints = [
            # Enforced in the database by migration 0060, or by
            # merge_duplicate_customers once any duplicates are merged.
            models.UniqueConstraint(
                fields=('last_name', 'first_name', 'email'), name=CUSTOMER_UNIQUE_CONSTRAINT,
            ),
        ]
        ordering = ('last_name', 'first_name')
        permissions = (
            (
                'can_autocomplete_users',
                _('Able to use customer and User autocomplete features (in various admin forms)')
            ),
            ('view_other_user_profiles', _('Able to view other Customer and User profile pages')),
        )
        verbose_name = _('Customer')
        verbose_name_plural = _('Customers')
