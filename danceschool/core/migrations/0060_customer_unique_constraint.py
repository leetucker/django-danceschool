from django.db import migrations, models


def enforce_unique_customers(apps, schema_editor):
    '''
    Replace the unique_together index from 0001 with a named constraint,
    built afresh.  If the database already holds duplicate customers (its
    old index went missing, or stopped working), the constraint is left out
    with a warning rather than failing the deploy: merge_duplicate_customers
    merges the duplicates and then creates it.
    '''
    from danceschool.core.utils.customers import ensure_customer_constraint
    Customer = apps.get_model('core', 'Customer')
    ensure_customer_constraint(schema_editor, Customer, replace_others=True)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0059_alter_classdescription_description_and_more'),
    ]

    operations = [
        # Django's view of the model; the database changes follow.
        migrations.SeparateDatabaseAndState(state_operations=[
            migrations.AlterUniqueTogether(name='customer', unique_together=set()),
            migrations.AddConstraint(
                model_name='customer',
                constraint=models.UniqueConstraint(
                    fields=('last_name', 'first_name', 'email'), name='core_customer_unique_name_email',
                ),
            ),
        ]),
        migrations.RunPython(enforce_unique_customers, migrations.RunPython.noop),
    ]
