from django.views.generic import FormView, TemplateView
from django.http import JsonResponse, HttpResponseRedirect, Http404
from django.contrib.auth.views import redirect_to_login
from django.contrib import messages
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.urls import reverse
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.dateparse import parse_datetime

from datetime import datetime, timedelta

from danceschool.core.models import (
    Instructor, StaffMember, Registration, EventRegistration,
    DanceRole, Event, EventOccurrence, EventStaffMember, Customer
)
from danceschool.core.constants import getConstant, REG_VALIDATION_STR
from danceschool.core.utils.timezone import ensure_localtime

from .forms import SlotCreationForm, SlotUpdateForm, SlotBookingForm, PrivateLessonStudentInfoForm
from .models import InstructorAvailabilitySlot, PrivateLessonEvent, PrivateLessonCustomer
from .constants import PRIVATELESSON_VALIDATION_STR


class InstructorAvailabilityView(TemplateView):
    template_name = 'private_lessons/instructor_availability_fullcalendar.html'

    def get(self, request, *args, **kwargs):
        # Only instructors or individuals with permission to change
        # other instructors' availability have permission to see this view.
        thisUser = getattr(request, 'user', None)
        thisStaffMember = getattr(thisUser, 'staffmember', None)
        if (
            (thisStaffMember and thisUser and thisUser.has_perm('private_lessons.edit_own_availability')) or
            (thisUser and thisUser.has_perm('private_lessons.edit_others_availability'))
        ):
            return super().get(request, *args, **kwargs)
        raise Http404()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context.update({
            'instructor': getattr(getattr(self.request, 'user'), 'staffmember'),
            'instructor_list': StaffMember.objects.filter(
                instructor__availableForPrivates=True, instructorprivatelessondetails__isnull=False
            ),
            'creation_form': SlotCreationForm(),
            'update_form': SlotUpdateForm(),
        })

        return context


class AddAvailabilitySlotView(FormView):
    form_class = SlotCreationForm

    def get(self, request, *args, **kwargs):
        return JsonResponse({'valid': False})

    def form_invalid(self, form):
        return JsonResponse(form.errors, status=400)

    def form_valid(self, form):
        '''
        Create slots and return success message.
        '''
        startDate = form.cleaned_data['startDate']
        endDate = form.cleaned_data['endDate']
        startTime = form.cleaned_data['startTime']
        endTime = form.cleaned_data['endTime']
        instructor = form.cleaned_data['instructorId']

        interval_minutes = getConstant('privateLessons__lessonLengthInterval')

        this_date = startDate
        while this_date <= endDate:
            this_time = startTime
            while this_time < endTime:
                InstructorAvailabilitySlot.objects.create(
                    instructor=instructor,
                    startTime=ensure_localtime(datetime.combine(this_date, this_time)),
                    duration=interval_minutes,
                    location=form.cleaned_data.get('location'),
                    room=form.cleaned_data.get('room'),
                    pricingTier=form.cleaned_data.get('pricingTier'),
                )
                this_time = (
                    ensure_localtime(datetime.combine(this_date, this_time)) +
                    timedelta(minutes=interval_minutes)
                ).time()
            this_date += timedelta(days=1)

        return JsonResponse({'valid': True})


class UpdateAvailabilitySlotView(FormView):
    form_class = SlotUpdateForm
    http_method_names = ['post', ]

    def get(self, request, *args, **kwargs):
        return JsonResponse({'valid': False})

    def form_invalid(self, form):
        return JsonResponse(form.errors, status=400)

    def form_valid(self, form):
        '''
        Modify or delete the availability slot as requested and return success message.
        '''
        slotIds = form.cleaned_data['slotIds']
        deleteSlot = form.cleaned_data.get('deleteSlot', False)

        these_slots = InstructorAvailabilitySlot.objects.filter(id__in=slotIds)

        if deleteSlot:
            these_slots.delete()
        else:
            for this_slot in these_slots:
                this_slot.location = form.cleaned_data['updateLocation']
                this_slot.room = form.cleaned_data['updateRoom']
                this_slot.status = form.cleaned_data['updateStatus']
                this_slot.pricingTier = form.cleaned_data.get('updatePricing')
                this_slot.save()

        return JsonResponse({'valid': True})


class BookPrivateLessonView(FormView):
    template_name = 'private_lessons/private_lesson_fullcalendar.html'
    form_class = SlotBookingForm

    def dispatch(self, request, *args, **kwargs):
        if not getConstant('privateLessons__allowPublicBooking') and not request.user.is_staff:
            return redirect_to_login(request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'instructor_list': Instructor.objects.filter(
                availableForPrivates=True,
                staffMember__instructorprivatelessondetails__isnull=False
            ),
            'defaultLessonLength': getConstant('privateLessons__defaultLessonLength'),
        })
        return context

    def get_form_kwargs(self, **kwargs):
        '''
        Pass the current user to the form to render the payAtDoor field if applicable.
        '''
        kwargs = super().get_form_kwargs(**kwargs)
        kwargs['user'] = self.request.user if hasattr(self.request, 'user') else None
        return kwargs

    def form_valid(self, form):

        slotId = form.cleaned_data.pop('slotId')
        payAtDoor = form.cleaned_data.pop('payAtDoor', False)

        # Check that passed duration is valid.
        try:
            duration = int(form.cleaned_data.pop('duration'))
        except ValueError:
            form.add_error(None, ValidationError(_('Invalid duration.'), code='invalid'))
            return self.form_invalid(form)

        # Include the submission user if the user is authenticated
        if self.request.user.is_authenticated:
            submissionUser = self.request.user
        else:
            submissionUser = None

        try:
            thisSlot = InstructorAvailabilitySlot.objects.get(id=slotId)
        except ObjectDoesNotExist:
            form.add_error(None, ValidationError(_('Invalid slot ID.'), code='invalid'))
            return self.form_invalid(form)

        # Check that passed role is valid
        try:
            role = DanceRole.objects.filter(
                instructorprivatelessondetails__instructor=thisSlot.instructor.staffMember,
            ).get(id=getattr(form.cleaned_data.pop('role'), 'id', None))
        except (ValueError, ObjectDoesNotExist):
            form.add_error(None, ValidationError(_('Invalid dance role.'), code='invalid'))
            return self.form_invalid(form)

        affectedSlots = InstructorAvailabilitySlot.objects.filter(
            instructor=thisSlot.instructor,
            location=thisSlot.location,
            room=thisSlot.room,
            pricingTier=thisSlot.pricingTier,
            startTime__gte=thisSlot.startTime,
            startTime__lt=thisSlot.startTime + timedelta(minutes=duration),
        ).filter(
            Q(status=InstructorAvailabilitySlot.SlotStatus.available) |
            (
                Q(status=InstructorAvailabilitySlot.SlotStatus.tentative) &
                ~Q(eventRegistration__invoiceItem__invoice__expirationDate__gte=timezone.now())
            )
        )

        # If someone cancels, there will already be one or more events associated
        # with these slots.  These need to be deleted, and we also validate to be
        # certain that we are not cancelling any Event which has finalized or in progress
        # registrations attached to it.
        existingEvents = PrivateLessonEvent.objects.filter(
            instructoravailabilityslot__id__in=[x.id for x in affectedSlots]
        )

        if existingEvents.filter(
            Q(eventregistration__isnull=False) |
            Q(eventregistration__invoiceItem__invoice__expirationDate__gte=timezone.now())
        ).exists():
            form.add_error(
                None,
                ValidationError(
                    _(
                        'Some or all of your requested lesson time is currently ' +
                        'in the process of registration. Please select a new ' +
                        'slot or try again later.'
                    ),
                    code='invalid'
                )
            )
            return self.form_invalid(form)
        else:
            existingEvents.delete()

        # Create the lesson record and set related info
        lesson = PrivateLessonEvent.objects.create(
            pricingTier=thisSlot.pricingTier,
            location=thisSlot.location,
            room=thisSlot.room,
            participants=form.cleaned_data.pop('participants'),
            comments=form.cleaned_data.pop('comments'),
            status=Event.RegStatus.hidden,
        )

        lesson_instructor = EventStaffMember.objects.create(
            event=lesson,
            category=getConstant('privateLessons__eventStaffCategoryPrivateLesson'),
            submissionUser=submissionUser,
            staffMember=thisSlot.instructor.staffMember,
        )

        lesson_occurrence = EventOccurrence.objects.create(
            event=lesson,
            startTime=thisSlot.startTime,
            endTime=thisSlot.startTime + timedelta(minutes=duration),
        )
        lesson_instructor.occurrences.add(lesson_occurrence)

        # Ensure that lesson start and end time are saved appropriately for
        # the event.
        lesson.save()

        # The temporary booking expires after a period of inactivity that is specified in preferences.
        expiry = timezone.now() + timedelta(minutes=getConstant('registration__sessionExpiryMinutes'))

        # Slots without pricing tiers can't go through the actual registration process.
        # Instead, they are sent to another view to get contact information.
        if not thisSlot.pricingTier or not getConstant('privateLessons__allowRegistration'):
            affectedSlots.update(
                lessonEvent=lesson,
                status=InstructorAvailabilitySlot.SlotStatus.tentative,
            )
            self.request.session[PRIVATELESSON_VALIDATION_STR] = {
                'lesson': lesson.id,
                'payAtDoor': payAtDoor,
                'expiry': expiry.strftime('%Y-%m-%dT%H:%M:%S%z'),
            }
            return HttpResponseRedirect(reverse('privateLessonStudentInfo'))

        # Slots with pricing tiers require a Registration to be created,
        # and then they are redirected through the registration system.
        else:

            regSession = self.request.session.get(REG_VALIDATION_STR, {})

            # Create a temporary Registration associated with this lesson.
            reg = Registration(
                submissionUser=submissionUser, dateTime=timezone.now(),
                payAtDoor=payAtDoor,
                final=False,
            )

            price = lesson.getBasePrice(payAtDoor=payAtDoor) * affectedSlots.count()

            tr = EventRegistration(
                event=lesson, role=role,
            )

            # Any remaining form data goes into the JSONfield.
            reg.data = form.cleaned_data or {}

            # Now we are ready to save and proceed.
            invoice = reg.link_invoice(
                expirationDate=expiry, grossTotal=price, total=price
            )
            reg.save()
            tr.registration = reg
            tr.save(grossTotal=price, total=price)

            # Override the default public-event tax rate with the private-lesson
            # specific rate. buyerPaysSalesTax is already set on the invoice by
            # link_invoice() and calculateTaxes() reads it from there.
            pl_tax_rate = getConstant('privateLessons__salesTaxRate') or 0
            tr.invoiceItem.taxRate = pl_tax_rate
            tr.invoiceItem.calculateTaxes()
            tr.invoiceItem.save(updateInvoiceTotals=True)

            affectedSlots.update(
                lessonEvent=lesson,
                status=InstructorAvailabilitySlot.SlotStatus.tentative,
                eventRegistration=tr,
            )

            # Load the invoice ID into session data like a regular registration
            # and redirect to Step 2 as usual.
            regSession["invoice_id"] = invoice.id.__str__()
            regSession["invoice_expiry"] = expiry.strftime('%Y-%m-%dT%H:%M:%S%z')
            self.request.session[REG_VALIDATION_STR] = regSession
            return HttpResponseRedirect(reverse('getStudentInfo'))


class PrivateLessonStudentInfoView(FormView):
    '''
    For private lessons booked and paid for using the traditional
    online registration system, that system collects all information
    needed for booking.  However, for lessons booked without the option
    of online payment, we still need to collect the customer's name and
    email adress before finalizing booking.  This view handles the
    collection of that information.
    '''
    template_name = 'private_lessons/get_student_info.html'
    form_class = PrivateLessonStudentInfoForm

    def dispatch(self, request, *args, **kwargs):
        '''
        Handle the session data passed by the prior view.
        '''

        lessonSession = request.session.get(PRIVATELESSON_VALIDATION_STR, {})

        try:
            self.lesson = PrivateLessonEvent.objects.get(id=lessonSession.get('lesson'))
        except (ValueError, ObjectDoesNotExist):
            messages.error(request, _('Invalid lesson identifier passed to sign-up form.'))
            return HttpResponseRedirect(reverse('bookPrivateLesson'))

        expiry = parse_datetime(lessonSession.get('expiry', ''), )
        if not expiry or expiry < timezone.now():
            messages.info(request, _('Your registration session has expired. Please try again.'))
            return HttpResponseRedirect(reverse('bookPrivateLesson'))

        self.payAtDoor = lessonSession.get('payAtDoor', False)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'lesson': self.lesson,
            'teachers': [x.staffMember.fullName for x in self.lesson.eventstaffmember_set.all()],
        })
        return context

    def get_form_kwargs(self, **kwargs):
        ''' Pass along the request data to the form '''
        kwargs = super().get_form_kwargs(**kwargs)
        kwargs['request'] = self.request
        kwargs['payAtDoor'] = self.payAtDoor
        return kwargs

    def form_valid(self, form):
        first_name = form.cleaned_data.get('firstName')
        last_name = form.cleaned_data.get('lastName')
        email = form.cleaned_data.get('email')
        phone = form.cleaned_data.get('phone')

        customer, created = Customer.objects.get_or_create_customer(
            first_name, last_name, email, phone=phone
        )
        # Ensure that this customer is affiliated with this lesson.
        PrivateLessonCustomer.objects.get_or_create(
            customer=customer,
            lesson=self.lesson,
        )

        self.lesson.finalizeBooking()
        messages.success(self.request, _('Your private lesson has been scheduled successfully.'))
        self.request.session.pop(PRIVATELESSON_VALIDATION_STR, {})
        return HttpResponseRedirect(reverse('submissionRedirect'))
