import json
import os
import tempfile
from io import StringIO

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.db.migrations.state import ProjectState
from django.test import TestCase, TransactionTestCase

from danceschool.core.checks import check_unique_constraints
from danceschool.core.models import (
    Customer, CustomerGroup, DanceRole, EventRegistration, Invoice, Registration,
)
from danceschool.core.models.customers import CUSTOMER_UNIQUE_CONSTRAINT
from danceschool.core.tests.defaults import DefaultSchoolTestCase
from danceschool.core.utils.customers import (
    Merger, customer_unique_indexes, duplicate_groups, ensure_customer_constraint,
    remap_customer_ids,
)
from danceschool.prerequisites.models import CustomerRequirement, Requirement


class GetOrCreateCustomerTest(TestCase):

    def test_creates_a_new_customer(self):
        customer, created = Customer.objects.get_or_create_customer(' Jane ', 'Doe', 'jane@x.com ', phone='555-1234')
        self.assertTrue(created)
        self.assertEqual(
            (customer.first_name, customer.last_name, customer.email, customer.phone),
            ('Jane', 'Doe', 'jane@x.com', '555-1234'),
        )

    def test_matches_ignoring_case_and_whitespace(self):
        existing = Customer.objects.create(first_name='Jane', last_name='Doe', email='jane@x.com')
        customer, created = Customer.objects.get_or_create_customer(' jane', 'DOE ', 'Jane@X.com')
        self.assertFalse(created)
        self.assertEqual(customer, existing)
        self.assertEqual(Customer.objects.count(), 1)

    def test_prefers_an_exact_match_then_a_user_then_the_oldest(self):
        oldest = Customer.objects.create(first_name='jane', last_name='doe', email='jane@x.com')
        with_user = Customer.objects.create(
            first_name='JANE', last_name='DOE', email='jane@x.com',
            user=User.objects.create_user('jane', 'jane@x.com', 'pw'),
        )
        exact = Customer.objects.create(first_name='Jane', last_name='Doe', email='jane@x.com')
        self.assertEqual(Customer.objects.get_or_create_customer('Jane', 'Doe', 'jane@x.com')[0], exact)
        self.assertEqual(Customer.objects.get_or_create_customer('Jane', 'DOE', 'jane@x.com')[0], with_user)
        with_user.delete()
        self.assertEqual(Customer.objects.get_or_create_customer('Jane', 'DOE', 'jane@x.com')[0], oldest)

    def test_phone_is_saved_but_never_blanked(self):
        existing = Customer.objects.create(first_name='Jane', last_name='Doe', email='jane@x.com', phone='555-1234')
        Customer.objects.get_or_create_customer('Jane', 'Doe', 'jane@x.com', phone=None)
        existing.refresh_from_db()
        self.assertEqual(existing.phone, '555-1234')
        Customer.objects.get_or_create_customer('Jane', 'Doe', 'jane@x.com', phone='555-9999')
        existing.refresh_from_db()
        self.assertEqual(existing.phone, '555-9999')


class MergerTest(DefaultSchoolTestCase):
    '''
    Merging, on customers that differ only in case (the database allows those,
    so no schema changes are needed here).
    '''

    def setUp(self):
        self.survivor = Customer.objects.create(
            first_name='Jane', last_name='Doe', email='jane@x.com', data={'a': 1, 'shared': 'survivor'},
        )
        self.duplicate = Customer.objects.create(
            first_name='jane', last_name='doe', email='Jane@X.com', phone='555-1234',
            data={'b': 2, 'shared': 'duplicate'},
            user=User.objects.create_user('jane', 'jane@x.com', 'pw'),
        )
        self.series = self.create_series()
        role = DanceRole.objects.get(name='Lead')
        invoice = Invoice.objects.create(firstName='Jane', lastName='Doe', email='jane@x.com')
        registration = Registration.objects.create(invoice=invoice)
        self.er = EventRegistration.objects.create(
            registration=registration, event=self.series, customer=self.duplicate, role=role,
        )
        self.partner_er = EventRegistration.objects.create(
            registration=registration, event=self.create_series(), customer=self.survivor, role=role,
            data={'partner': {'firstName': 'jane', 'customerId': self.duplicate.id},
                  'er_7_partner_customerId': str(self.duplicate.id)},
        )
        self.group = CustomerGroup.objects.create(name='Volunteers')
        self.duplicate.groups.add(self.group)
        requirement = Requirement.objects.create(name='Level 1 first')
        other = Requirement.objects.create(name='Waiver')
        # The same requirement for both: a conflict.  Another only on the duplicate.
        CustomerRequirement.objects.create(customer=self.survivor, requirement=requirement, role=role, met=True)
        CustomerRequirement.objects.create(customer=self.duplicate, requirement=requirement, role=role, met=False)
        CustomerRequirement.objects.create(customer=self.duplicate, requirement=other, role=role, met=True)

    def merge(self):
        groups = duplicate_groups(Customer, 'default', ignore_case=True)
        self.assertEqual(len(groups), 1)
        merger = Merger(Customer, 'default')
        summary = merger.merge(self.survivor, [self.duplicate])
        merger.remap_partner_ids()
        return summary

    def test_merge(self):
        summary = self.merge()
        self.assertFalse(Customer.objects.filter(pk=self.duplicate.pk).exists())
        self.survivor.refresh_from_db()
        self.er.refresh_from_db()
        self.partner_er.refresh_from_db()

        self.assertEqual(self.er.customer, self.survivor)
        self.assertEqual(self.partner_er.data['partner']['customerId'], self.survivor.id)
        self.assertEqual(self.partner_er.data['er_7_partner_customerId'], str(self.survivor.id))
        self.assertEqual(list(self.survivor.groups.all()), [self.group])
        # Fields: the survivor's values win; gaps are filled from the duplicate.
        self.assertEqual(self.survivor.phone, '555-1234')
        self.assertEqual(self.survivor.data, {'a': 1, 'b': 2, 'shared': 'survivor'})
        self.assertEqual(self.survivor.user.username, 'jane')
        # Requirements: the conflicting duplicate row is dropped (and reported), the other moves.
        reqs = CustomerRequirement.objects.filter(customer=self.survivor)
        self.assertEqual(reqs.count(), 2)
        self.assertTrue(reqs.get(requirement__name='Level 1 first').met)
        self.assertEqual(summary['moved']['prerequisites.CustomerRequirement'], 1)
        self.assertEqual(len(summary['removed_conflicts']), 1)
        self.assertEqual(summary['moved']['core.EventRegistration'], 1)
        json.dumps(summary)

    def test_registration_save_side_effects_are_avoided(self):
        items_before = self.er.invoiceItem_id
        self.merge()
        self.er.refresh_from_db()
        self.assertEqual(self.er.invoiceItem_id, items_before)

    def test_groups_with_two_users_are_skipped(self):
        self.survivor.user = User.objects.create_user('other', 'o@x.com', 'pw')
        self.survivor.save()
        out = StringIO()
        call_command('merge_duplicate_customers', '--ignore-case', '--commit', stdout=out)
        self.assertIn('2 different user accounts', out.getvalue())
        self.assertTrue(Customer.objects.filter(pk=self.duplicate.pk).exists())

    def test_remap_leaves_other_ids_alone(self):
        data = {'partner': {'customerId': 5}, 'er_1_partner_customerId': '', 'other': 3}
        self.assertEqual(remap_customer_ids(data, {6: 1}), data)


class DuplicateCustomersEndToEndTest(TransactionTestCase):
    '''
    The production situation: the constraint is missing and exact duplicates
    exist.  Needs schema changes, so it runs outside a test transaction.
    '''

    def setUp(self):
        # Remove the constraint as Django would for a model without it (on
        # SQLite, the table is rebuilt from the model's definition).
        constraint = next(c for c in Customer._meta.constraints if c.name == CUSTOMER_UNIQUE_CONSTRAINT)
        state = ProjectState.from_apps(apps)
        state.models['core', 'customer'].options['constraints'] = []
        with connection.schema_editor() as editor:
            editor.remove_constraint(state.apps.get_model('core', 'Customer'), constraint)
        self.first = Customer.objects.create(first_name='Jane', last_name='Doe', email='jane@x.com')
        self.second = Customer.objects.create(first_name='Jane', last_name='Doe', email='jane@x.com')

    def tearDown(self):
        # Leave the schema as the other tests expect it.
        Customer.objects.all().delete()
        with connection.schema_editor() as editor:
            ensure_customer_constraint(editor, Customer)

    def warnings(self):
        return [w for w in check_unique_constraints(databases=['default']) if w.obj is Customer]

    def test_check_reports_the_missing_constraint(self):
        self.assertEqual(len(self.warnings()), 1)
        self.assertIn('merge_duplicate_customers', self.warnings()[0].hint)

    def test_migration_skips_the_constraint_while_duplicates_exist(self):
        migration = __import__(
            'danceschool.core.migrations.0060_customer_unique_constraint', fromlist=['enforce_unique_customers']
        )
        with self.assertLogs('danceschool.core.utils.customers', 'WARNING'):
            with connection.schema_editor() as editor:
                migration.enforce_unique_customers(apps, editor)
        self.assertNotIn(CUSTOMER_UNIQUE_CONSTRAINT, customer_unique_indexes(connection, Customer))

    def test_dry_run_then_commit(self):
        out = StringIO()
        call_command('merge_duplicate_customers', stdout=out)
        self.assertIn('Would merge customer %s <- %s' % (self.first.id, self.second.id), out.getvalue())
        self.assertIn('could be enforced', out.getvalue())
        self.assertEqual(Customer.objects.count(), 2)

        report = os.path.join(tempfile.mkdtemp(), 'report.json')
        out = StringIO()
        call_command('merge_duplicate_customers', '--commit', '--report', report, stdout=out)
        self.assertIn('now enforces unique customers', out.getvalue())
        self.assertEqual(list(Customer.objects.values_list('id', flat=True)), [self.first.id])
        self.assertIn(CUSTOMER_UNIQUE_CONSTRAINT, customer_unique_indexes(connection, Customer))
        self.assertEqual(self.warnings(), [])
        with open(report) as f:
            self.assertEqual(json.load(f)['groups'][0]['customers'], [self.first.id, self.second.id])


class CustomerConstraintTest(TestCase):

    def test_migrated_database_has_only_the_named_constraint(self):
        self.assertEqual(list(customer_unique_indexes(connection, Customer)), [CUSTOMER_UNIQUE_CONSTRAINT])
        self.assertEqual([w for w in check_unique_constraints(databases=['default']) if w.obj is Customer], [])
