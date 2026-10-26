'''
Keeping Customer records unique.

Customers are unique by (last_name, first_name, email).  A database can lack
the constraint that enforces this (production lost it once, and Django's
migration state did not notice), and then duplicates accumulate.  The
functions here find and merge duplicates, and (re)create the constraint.
They are used by migration 0060 and by the merge_duplicate_customers
management command.
'''
from collections import Counter, defaultdict
import logging

from django.db import IntegrityError, connections, models, transaction
from django.db.models import Count, QuerySet


logger = logging.getLogger(__name__)

CUSTOMER_FIELDS = ('last_name', 'first_name', 'email')


# The database constraint

def customer_unique_indexes(connection, model):
    '''
    {name: introspection info} for the unique constraints and indexes on
    exactly (last_name, first_name, email).
    '''
    columns = {model._meta.get_field(f).column for f in CUSTOMER_FIELDS}
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, model._meta.db_table)
    return {
        name: info for name, info in constraints.items()
        if info.get('unique') and not info.get('primary_key') and set(info['columns']) == columns
    }


def exact_duplicate_groups(model, using):
    ''' The number of (last_name, first_name, email) values shared by several rows. '''
    return (
        QuerySet(model, using=using).values(*CUSTOMER_FIELDS)
        .annotate(n=Count('id')).filter(n__gt=1).count()
    )


def customer_constraint_status(connection, model):
    '''
    'exists' if the database enforces Customer's unique constraint;
    otherwise 'duplicates' if exact duplicates prevent it, or 'missing'.
    '''
    from ..models.customers import CUSTOMER_UNIQUE_CONSTRAINT

    if CUSTOMER_UNIQUE_CONSTRAINT in customer_unique_indexes(connection, model):
        return 'exists'
    return 'duplicates' if exact_duplicate_groups(model, connection.alias) else 'missing'


def ensure_customer_constraint(schema_editor, model, replace_others=False):
    '''
    Make sure the database enforces Customer's unique constraint (named in
    model._meta.constraints).  Returns 'exists', 'created', or 'duplicates'
    if it cannot be created until the duplicates are merged.

    With replace_others, any other unique index on the same columns (the
    unique_together index from migration 0001) is dropped first.  The
    constraint is then built afresh, so an index that has silently gone bad
    (for instance after an operating system upgrade changed how text sorts)
    is replaced by a correct one.
    '''
    from ..models.customers import CUSTOMER_UNIQUE_CONSTRAINT

    connection = schema_editor.connection
    constraint = next(c for c in model._meta.constraints if c.name == CUSTOMER_UNIQUE_CONSTRAINT)
    existing = customer_unique_indexes(connection, model)

    if replace_others:
        for name, info in existing.items():
            if name == CUSTOMER_UNIQUE_CONSTRAINT:
                continue
            sql = schema_editor.sql_delete_index if info.get('index') else schema_editor.sql_delete_unique
            schema_editor.execute(schema_editor._delete_constraint_sql(sql, model, name))
        existing = customer_unique_indexes(connection, model)

    if CUSTOMER_UNIQUE_CONSTRAINT in existing:
        return 'exists'
    if exact_duplicate_groups(model, connection.alias):
        logger.warning(
            'Customer names and emails are not unique in the database, so the constraint %s '
            'was not created.  Run "manage.py merge_duplicate_customers" to merge the '
            'duplicates; it then creates the constraint.', CUSTOMER_UNIQUE_CONSTRAINT,
        )
        return 'duplicates'
    schema_editor.add_constraint(model, constraint)
    return 'created'


# Finding duplicates

def normalized(value):
    return ' '.join((value or '').split()).casefold()


def duplicate_groups(model, using, ignore_case=False):
    '''
    Lists of customers (each ordered by id) that share a name and email:
    exactly, or with ignore_case, ignoring case and whitespace.
    '''
    rows = QuerySet(model, using=using).order_by('id')
    buckets = defaultdict(list)
    for customer in rows:
        if ignore_case:
            key = tuple(normalized(getattr(customer, f)) for f in CUSTOMER_FIELDS)
        else:
            key = tuple(getattr(customer, f) for f in CUSTOMER_FIELDS)
        buckets[key].append(customer)
    return [group for group in buckets.values() if len(group) > 1]


def choose_survivor(group):
    '''
    The customer to keep: the one linked to a user account, otherwise the
    oldest.  Returns (survivor, None), or (None, reason) if the group cannot
    be merged automatically.
    '''
    users = {c.user_id for c in group if c.user_id}
    if len(users) > 1:
        return None, 'linked to %s different user accounts' % len(users)
    with_user = [c for c in group if c.user_id]
    return (with_user[0] if with_user else group[0]), None


# Merging

def unique_sets(model):
    ''' The field-name sets that model's unconditional unique constraints cover. '''
    sets = [set(u) for u in model._meta.unique_together]
    sets += [
        set(c.fields) for c in model._meta.constraints
        if isinstance(c, models.UniqueConstraint) and c.fields and c.condition is None
    ]
    sets += [{f.name} for f in model._meta.local_fields if f.unique and not f.primary_key]
    return sets


def customer_relations(model):
    ''' (related model, name of its field pointing at Customer) for every reverse relation. '''
    return [
        (rel.related_model, rel.field.name)
        for rel in model._meta.related_objects
        if not rel.many_to_many
    ]


class Merger:
    '''
    Merges groups of duplicate customers into one survivor each.  Writes use
    QuerySet.update() (never save(), whose side effects include creating
    invoice items for event registrations).  Run inside a transaction.
    '''

    def __init__(self, model, using):
        self.model = model
        self.using = using
        self.remap = {}       # merged customer id -> survivor id

    def qs(self, model):
        return QuerySet(model, using=self.using)

    def merge(self, survivor, others):
        '''
        Move everything that belongs to others to survivor, combine their
        fields, and delete them.  Returns a summary for the report.
        '''
        other_ids = [o.id for o in others]
        moved = Counter()
        removed = []

        for related, field in customer_relations(self.model):
            label = related._meta.label
            rows = self.qs(related).filter(**{'%s__in' % field: other_ids})
            if any(field in s for s in unique_sets(related)):
                # Moving a row may collide with one the survivor already has
                # (the same requirement, the same lesson).  Then keep the
                # survivor's and delete the duplicate's, recording it.
                for pk in list(rows.values_list('pk', flat=True)):
                    try:
                        with transaction.atomic(using=self.using):
                            self.qs(related).filter(pk=pk).update(**{field: survivor.pk})
                        moved[label] += 1
                    except IntegrityError:
                        row = self.qs(related).filter(pk=pk)
                        removed.append({'model': label, 'values': _plain(row.values().first())})
                        row.delete()
            else:
                n = rows.update(**{field: survivor.pk})
                if n:
                    moved[label] += n

        for field in self.model._meta.many_to_many:
            through = field.remote_field.through
            source = field.m2m_field_name()
            target = field.m2m_reverse_field_name()
            theirs = set(self.qs(through).filter(**{'%s__in' % source: other_ids}).values_list(
                '%s_id' % target, flat=True
            ))
            mine = set(self.qs(through).filter(**{source: survivor.pk}).values_list('%s_id' % target, flat=True))
            new = theirs - mine
            self.qs(through).bulk_create([
                through(**{'%s_id' % source: survivor.pk, '%s_id' % target: value}) for value in new
            ])
            if new:
                moved['%s.%s' % (self.model._meta.label, field.name)] += len(new)

        updates = {}
        if not survivor.phone:
            phones = [o.phone for o in sorted(others, key=lambda o: -o.id) if o.phone]
            if phones:
                updates['phone'] = phones[0]
        data = {}
        for other in others:
            data.update(other.data or {})
        data.update(survivor.data or {})
        if data != (survivor.data or {}):
            updates['data'] = data
        if not survivor.user_id:
            user_ids = [o.user_id for o in others if o.user_id]
            if user_ids:
                # A user has at most one customer: unlink it first.
                self.qs(self.model).filter(id__in=other_ids).update(user=None)
                updates['user_id'] = user_ids[0]
        if updates:
            self.qs(self.model).filter(pk=survivor.pk).update(**updates)

        # Nothing may still point at the customers about to be deleted
        # (event registrations would otherwise lose their customer).
        for related, field in customer_relations(self.model):
            left = self.qs(related).filter(**{'%s__in' % field: other_ids}).count()
            if left:
                raise RuntimeError(
                    '%s %s rows still point at customers %s' % (left, related._meta.label, other_ids)
                )
        self.qs(self.model).filter(id__in=other_ids).delete()

        for other_id in other_ids:
            self.remap[other_id] = survivor.pk
        return {'moved': dict(moved), 'removed_conflicts': removed, 'updated_fields': sorted(updates)}

    def remap_partner_ids(self):
        '''
        Event registrations name a partner by customer id inside their JSON
        data ('partner': {'customerId': n}, and leftover form fields named
        'er_<n>_partner_customerId').  Point them at the survivors.  Returns
        the number of registrations changed.
        '''
        from ..models import EventRegistration

        if not self.remap:
            return 0
        changed = 0
        rows = self.qs(EventRegistration).exclude(data={}).values_list('pk', 'data')
        for pk, data in rows.iterator():
            if not isinstance(data, dict):
                continue
            new = remap_customer_ids(data, self.remap)
            if new != data:
                self.qs(EventRegistration).filter(pk=pk).update(data=new)
                changed += 1
        return changed


def remap_customer_ids(data, remap):
    ''' A copy of an EventRegistration's data with partner customer ids remapped. '''
    def mapped(value):
        try:
            return remap.get(int(value), value) if value not in (None, '') else value
        except (TypeError, ValueError):
            return value

    new = dict(data)
    partner = new.get('partner')
    if isinstance(partner, dict) and 'customerId' in partner:
        new['partner'] = dict(partner, customerId=mapped(partner['customerId']))
    for key, value in data.items():
        if key.endswith('_partner_customerId'):
            new[key] = mapped(value)
            if isinstance(value, str) and new[key] != value:
                new[key] = str(new[key])
    return new


def _plain(values):
    ''' A values() dict made JSON-safe for the report. '''
    if values is None:
        return None
    return {k: v if isinstance(v, (int, float, str, bool, type(None), dict, list)) else str(v)
            for k, v in values.items()}


def schema_editor_for(using):
    return connections[using].schema_editor()
