import json

from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, connections, transaction

from danceschool.core.models import Customer
from danceschool.core.utils.customers import (
    CUSTOMER_FIELDS, Merger, choose_survivor, customer_constraint_status, duplicate_groups,
    ensure_customer_constraint, exact_duplicate_groups,
)


class Rollback(Exception):
    pass


class Command(BaseCommand):
    help = (
        'Merge customers that share a name and email into one record each, moving their '
        'registrations and everything else to the record that is kept, and then make the '
        'database enforce uniqueness.  Reports what it would do unless --commit is given.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Apply the merge (default: dry run).')
        parser.add_argument(
            '--ignore-case', action='store_true',
            help='Also merge customers whose name and email differ only in case or whitespace.',
        )
        parser.add_argument('--report', metavar='FILE', help='Write a JSON record of every merge.')
        parser.add_argument('--database', default=DEFAULT_DB_ALIAS)

    def handle(self, *args, **options):
        using = options['database']
        groups = duplicate_groups(Customer, using, ignore_case=options['ignore_case'])
        merger = Merger(Customer, using)
        report = []
        skipped = 0

        try:
            with transaction.atomic(using=using):
                for group in groups:
                    survivor, reason = choose_survivor(group)
                    exact = len({tuple(getattr(c, f) for f in CUSTOMER_FIELDS) for c in group}) == 1
                    entry = {
                        'survivor': survivor.id if survivor else None,
                        'customers': [c.id for c in group],
                        'names': sorted({'%s %s <%s>' % (c.first_name, c.last_name, c.email) for c in group}),
                        'exact': exact,
                    }
                    if reason:
                        skipped += 1
                        entry['skipped'] = reason
                        self.stdout.write(self.style.WARNING(
                            'Skipped %s: %s' % (', '.join(entry['names']), reason)
                        ))
                    else:
                        others = [c for c in group if c.id != survivor.id]
                        entry.update(merger.merge(survivor, others))
                        self.stdout.write('%s customer %s <- %s: %s%s' % (
                            'Merged' if options['commit'] else 'Would merge', survivor.id,
                            ', '.join(str(c.id) for c in others), entry['names'][0],
                            '' if exact else ' (case or whitespace variants: %s)' % ' / '.join(entry['names']),
                        ))
                    report.append(entry)

                partners = merger.remap_partner_ids()
                remaining = exact_duplicate_groups(Customer, using)

                merged = sum(len(e['customers']) - 1 for e in report if 'skipped' not in e)
                self.stdout.write(self.style.SUCCESS(
                    '%s %s groups (%s duplicate records removed); %s skipped; %s partner references '
                    'updated.' % (
                        'Merged' if options['commit'] else 'Would merge',
                        len(report) - skipped, merged, skipped, partners,
                    )
                ))
                if not options['commit']:
                    raise Rollback
        except Rollback:
            self.stdout.write(
                'Dry run: nothing was changed.  Afterwards, %s.  Run again with --commit to apply.'
                % ('the database constraint could be enforced' if not remaining else
                   '%s groups of exact duplicates would remain, so the constraint could not be '
                   'enforced yet' % remaining)
            )
        else:
            status = customer_constraint_status(connections[using], Customer)
            if status == 'missing':
                # Outside the transaction: SQLite cannot alter tables inside one.
                with connections[using].schema_editor() as editor:
                    status = ensure_customer_constraint(editor, Customer)
            self.stdout.write({
                'exists': 'The database already enforces unique customers.',
                'created': self.style.SUCCESS('The database now enforces unique customers.'),
                'duplicates': self.style.WARNING(
                    'Exact duplicates remain (see the skipped groups), so the database still does '
                    'not enforce unique customers.'
                ),
            }[status])

        if options['report']:
            with open(options['report'], 'w') as f:
                json.dump({'committed': options['commit'], 'groups': report}, f, indent=2)
            self.stdout.write('Report written to %s.' % options['report'])
