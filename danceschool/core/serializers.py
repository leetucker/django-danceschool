from rest_framework import serializers
from django.utils.translation import gettext_lazy as _
from .constants import REG_VALIDATION_STR
from .models import Event, EventRole


class EventRoleSerializer(serializers.ModelSerializer):
    model_class = serializers.CharField(default='EventRole', read_only=True)
    description = serializers.CharField(source='role.name', read_only=True)
    price = serializers.SerializerMethodField()
    count_registrations = serializers.SerializerMethodField()
    quantity_available = serializers.SerializerMethodField()

    def get_price(self, obj):
        payAtDoor = bool(self.context.get('payAtDoor', False))
        return obj.price(payAtDoor=payAtDoor)

    def get_count_registrations(self, obj):
        includeTemporaryRegs = bool(self.context.get('includeTemporaryRegs', False))
        return obj.numRegistered(includeTemporaryRegs)

    def get_quantity_available(self, obj):
        includeTemporaryRegs = bool(self.context.get('includeTemporaryRegs', False))
        return obj.capacity - obj.numRegistered(includeTemporaryRegs)

    class Meta:
        model = EventRole
        fields = [
            'sku', 'description', 'price', 'quantity_available', 'model_class',
            'id', 'capacity', 'count_registrations'
        ]


class SyntheticVariantSerializer(serializers.Serializer):
    sku = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    price = serializers.FloatField(default=0, read_only=True)
    quantity_available = serializers.CharField(read_only=True)
    model_class = serializers.CharField(default='None', read_only=True)
    capacity = serializers.CharField(read_only=True)


class VariantsField(serializers.ListField):
    '''
    A hybrid field that constructs the full set of variants available for each
    event. This includes separate items for each specified event role, a general
    register option if no roles are specified, and a drop-in option in cases
    where the event permits drop-in registrations.
    '''
    child = serializers.DictField()

    def to_representation(self, event):
        roles = getattr(event, 'eventrole_set', None) or []

        if hasattr(roles, "all"):  # If it's a RelatedManager
            roles = list(roles.filter(capacity__gt=0))

        role_data = EventRoleSerializer(roles, many=True, context=self.context).data

        # For Series events with no EventRole records, fall back to DanceType
        # roles so that the catalog SKUs match the buttons the template renders.
        if not roles:
            from .models import Series
            if isinstance(event, Series):
                try:
                    dtype_roles = list(
                        event.classDescription.danceTypeLevel.danceType.roles.all()
                    )
                except Exception:
                    dtype_roles = []
                if dtype_roles:
                    payAtDoor = self.context.get('payAtDoor', False)
                    price = event.getBasePrice(payAtDoor=payAtDoor)
                    role_data = [
                        {
                            'sku': f'EVENT_{event.id}_ROLE_{role.id}',
                            'description': role.name,
                            'price': price,
                            'quantity_available': None,
                            'model_class': 'Event',
                            'id': event.id,
                            'capacity': event.capacity,
                            'count_registrations': None,
                        }
                        for role in dtype_roles
                    ]

        # Now compute synthetic ones
        synthetic_variants = []

        # Add general admission if no roles have been specified
        if not roles and not role_data:
            numRegistered = event.getNumRegistered(
                includeTemporaryRegs=self.context.get(
                    'includeTemporaryRegs', False
                ),
                dateTime=self.context.get('cart_datetime', None)
            )

            synthetic_variants.append({
                'sku': f'EVENT_{event.id}_GENERAL',
                'description': 'General Admission',
                'price': event.getBasePrice(payAtDoor=self.context.get('payAtDoor', False)),
                'quantity_available': (event.capacity - numRegistered),
                'model_class': 'Event',
                'id': event.id,
                'capacity': event.capacity,
                'count_registrations': numRegistered,
            })

        # Add drop-in variants for Series that allow drop-ins.
        # Only included when payAtDoor is True because CartItemSerializer.validate_dropIn
        # rejects dropIn=True for non-door registrations.
        from .models import Series
        if (
            self.context.get('payAtDoor', False) and
            isinstance(event, Series) and
            getattr(event, 'allowDropins', False)
        ):
            dropin_price = event.getBasePrice(dropIns=1)
            if role_data:
                for role_variant in role_data:
                    role_id = role_variant.get('id')
                    dropin_sku = (
                        f'EVENT_{event.id}_DROPIN_ROLE_{role_id}'
                        if role_id else f'EVENT_{event.id}_DROPIN_GENERAL'
                    )
                    synthetic_variants.append({
                        **role_variant,
                        'sku': dropin_sku,
                        'description': _('Drop-in: %s') % role_variant.get('description', ''),
                        'price': dropin_price,
                        'dropIn': True,
                    })
            else:
                # No roles at all — add a single general drop-in variant.
                numRegistered = event.getNumRegistered(
                    includeTemporaryRegs=self.context.get('includeTemporaryRegs', False),
                    dateTime=self.context.get('cart_datetime', None),
                )
                synthetic_variants.append({
                    'sku': f'EVENT_{event.id}_DROPIN_GENERAL',
                    'description': _('Drop-in Registration'),
                    'price': dropin_price,
                    'quantity_available': (event.capacity - numRegistered),
                    'model_class': 'Event',
                    'id': event.id,
                    'capacity': event.capacity,
                    'count_registrations': numRegistered,
                    'dropIn': True,
                })

        # Merge them
        all_variants = role_data + synthetic_variants
        return all_variants



class EventSerializer(serializers.ModelSerializer):
    variants = VariantsField(source='*')

    class Meta:
        model = Event
        fields = [
            'id', 'name', 'shortDescription', 'firstOccurrenceTime',
            'nextOccurrenceTime', 'lastOccurrenceTime', 'durationMinutes',
            'basePrice', 'registrationEnabled', 'variants'
        ]


class PurchasableItemSerializer(serializers.Serializer):
    """
    Given a list of (instance, serializer_class) tuples,
    dynamically dispatch to the right serializer.
    """
    def to_representation(self, obj):
        serializer_map = self.context.get("serializer_map", {})

        obj_cls = type(obj)
        serializer_class = serializer_map.get(obj_cls)

        if serializer_class is None:
            # Try resolving to the registered based model (for polymorphic cases)
            for registered_cls in serializer_map.keys():
                if isinstance(obj, registered_cls):
                    serializer_class = serializer_map.get(registered_cls)
                    break

        if serializer_class is None:
            raise ValueError(f'No serializer registered for model: {type(obj).__name__}')

        data = serializer_class(obj, context=self.context).data
        data["item_type"] = serializer_class.__name__
        return data


class CartItemSerializer(serializers.Serializer):
    item_type = serializers.CharField()
    item_id = serializers.IntegerField()
    sku = serializers.CharField()
    # variant_id = serializers.CharField(allow_null=True, required=False)
    quantity = serializers.IntegerField(default=1, min_value=1)


    # These are properties that can only be set for door registrations:
    dropIn = serializers.BooleanField(required=False)
    dropInOccurrence = serializers.IntegerField(required=False, allow_null=True)
    requireFull = serializers.BooleanField(required=False)
    autoSubmit = serializers.BooleanField(required=False)
    autoFulfill = serializers.BooleanField(required=False)
    paymentMethod = serializers.CharField(required=False, allow_blank=True)

    def check_door_only_field(self, value: bool, permitted: bool=False) -> bool:
        payAtDoor = self.context.get('payAtDoor', False)
        if (value not in [permitted, None]) and not payAtDoor:
            raise serializers.ValidationError(
                'This option is unavailable for online registrations.'
            )
        return value

    def validate_dropIn(self, value):
        # Drop-in requires both user-level permissions and for the door
        # context check to pass
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if value and user and (
            user.has_perm('core.register_dropins') or
            user.has_perm('core.override_register_dropins')
        ):
            return self.check_door_only_field(value)
        elif value:
            raise serializers.ValidationError(
                'You do not have permission to register for drop-ins'
            )

    def validate_requireFull(self, value):
        return self.check_door_only_field(value, permitted=True)

    def validate_autoSubmit(self, value):
        return self.check_door_only_field(value, permitted=True)

    def validate_autoFulfill(self, value):
        return self.check_door_only_field(value, permitted=True)

    def validate_paymentMethod(self, value):
        payAtDoor = self.context.get('payAtDoor', False)
        if value and not payAtDoor:
            raise serializers.ValidationError(
                'This option is unavailable for online registrations.'
            )
        return value

    def validate(self, data):
        purchasable_items = self.context.get('purchasable_items', [])

        valid = False
        for qs, _ in purchasable_items:
            if qs.filter(id=data["item_id"]).exists():
                instance = qs.get(id=data["item_id"])

                # If variants are relevant, validate variant_id as well.
                if hasattr(instance, "variants"):
                    variants = [v["id"] for v in instance.variants] if isinstance(instance.variants, list) \
                               else instance.variants.values_list("id", flat=True)
                    if data.get("variant_id") and str(data["variant_id"]) not in map(str, variants):
                        raise serializers.ValidationError("Invalid variant for selected item.")

                # linkOnly events may only be added to the cart by users who
                # arrived via the event's private UUID link in this session.
                if (
                    isinstance(instance, Event) and
                    instance.status == Event.RegStatus.linkOnly
                ):
                    request = self.context.get('request')
                    session = getattr(request, 'session', {})
                    authorized_ids = session.get(REG_VALIDATION_STR, {}).get(
                        'link_authorized', []
                    )
                    if instance.pk not in authorized_ids:
                        raise serializers.ValidationError(
                            _('Registration for this event requires a direct link.')
                        )

                valid = True
                break

        if not valid:
            raise serializers.ValidationError("Item not found in available purchasable items.")

        return data
    

class CartSerializer(serializers.Serializer):
    items = CartItemSerializer(many=True)
    discount_code = serializers.CharField(required=False, allow_blank=True)
    marketing_id = serializers.CharField(required=False, allow_blank=True)
    checkout = serializers.BooleanField(required=False, default=False)

    firstName = serializers.CharField(required=False, max_length=100, allow_blank=True)
    lastName = serializers.CharField(required=False, max_length=100, allow_blank=True)
    email = serializers.CharField(required=False, max_length=200, allow_blank=True)

    student = serializers.BooleanField(required=False, default=False)

    def validate_discount_code(self, value):
        import re
        if value and not re.match(r'^[a-zA-Z0-9\-_]+$', value):
            raise serializers.ValidationError('Invalid discount code format.')
        return value
