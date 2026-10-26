'''
Database checks, run by "manage.py check --database default".

They catch a database that has drifted from what the models declare, which
Django's migrations cannot see: a unique constraint that has gone missing
(as Customer's once did, letting duplicate customers accumulate), or text
indexes that may have been invalidated by an operating system upgrade.
'''
from django.apps import apps
from django.core.checks import Tags, Warning, register
from django.db import connections, models


def declared_unique_column_sets(model):
    ''' Column sets that model declares unique, unconditionally. '''
    sets = [tuple(u) for u in model._meta.unique_together]
    sets += [
        tuple(c.fields) for c in model._meta.constraints
        if isinstance(c, models.UniqueConstraint) and c.fields and c.condition is None
        and not c.include
    ]
    sets += [(f.name,) for f in model._meta.local_fields if f.unique and not f.primary_key]
    return [frozenset(model._meta.get_field(f).column for f in fields) for fields in sets]


@register(Tags.database)
def check_unique_constraints(app_configs=None, databases=None, **kwargs):
    errors = []
    for alias in databases or []:
        connection = connections[alias]
        with connection.cursor() as cursor:
            tables = set(connection.introspection.table_names(cursor))
            for model in apps.get_models():
                if model._meta.proxy or not model._meta.managed or model._meta.db_table not in tables:
                    continue
                expected = declared_unique_column_sets(model)
                if not expected:
                    continue
                present = {
                    frozenset(info['columns'])
                    for info in connection.introspection.get_constraints(
                        cursor, model._meta.db_table
                    ).values()
                    if info.get('unique')
                }
                for columns in expected:
                    if columns not in present:
                        errors.append(Warning(
                            '%s declares (%s) unique, but database "%s" does not enforce it.'
                            % (model._meta.label, ', '.join(sorted(columns)), alias),
                            hint=(
                                'Duplicates may already exist.  For Customer, run '
                                '"manage.py merge_duplicate_customers", which then restores the '
                                'constraint.'
                            ) if model._meta.label == 'core.Customer' else None,
                            obj=model,
                            id='danceschool.W001',
                        ))
    return errors


@register(Tags.database)
def check_collation_version(app_configs=None, databases=None, **kwargs):
    '''
    PostgreSQL 15+ records the collation version that a database's text
    indexes were built with.  If the operating system's version differs (an
    OS or container image upgrade), those indexes may silently miss rows.
    '''
    errors = []
    for alias in databases or []:
        connection = connections[alias]
        if connection.vendor != 'postgresql' or connection.pg_version < 150000:
            continue
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT datcollversion, pg_database_collation_actual_version(oid) '
                'FROM pg_database WHERE datname = current_database()'
            )
            recorded, actual = cursor.fetchone()
        if recorded and actual and recorded != actual:
            errors.append(Warning(
                'Database "%s" was built with collation version %s, but the system now uses %s, so '
                'its text indexes may be unreliable.' % (alias, recorded, actual),
                hint=(
                    'Run "REINDEX DATABASE <name>;" and then "ALTER DATABASE <name> REFRESH '
                    'COLLATION VERSION;".  Merge duplicate customers first, or the unique '
                    'indexes cannot be rebuilt.'
                ),
                id='danceschool.W002',
            ))
    return errors
