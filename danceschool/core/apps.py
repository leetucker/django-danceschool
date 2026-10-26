# Give this app a custom verbose name to avoid confusion
from django.apps import AppConfig, apps
from django.utils.translation import gettext_lazy as _
from .registries import (
    plugin_templates_registry, model_templates_registry,
    extras_templates_registry
)


class CoreAppConfig(AppConfig):
    name = 'danceschool.core'
    verbose_name = _('Core School Functions')

    def ready(self):
        # Ensure that signal handlers and database checks are loaded
        from . import checks, handlers
        from .managers import EventManager

        # This will load all cms_plugins.py files under
        # installed apps to identify custom plugin templates
        app_names = [app.name for app in apps.app_configs.values()]
        plugin_templates_registry.autodiscover(app_names)
        model_templates_registry.autodiscover(app_names)
        extras_templates_registry.autodiscover(app_names)

        # get the Event model from the registry (safe at this point)
        Event = apps.get_model(self.label, "Event")

        # attach a custom manager the Event model. It occurs here to avoid
        # issues with early import that would arise if the manager were
        # specified directly in models.py.
        Event.add_to_class("objects", EventManager())

        # DAL's Select2WidgetMixin.media lists select2.full.js without including
        # the surrounding jQuery files, so Django's media merger places it after
        # jquery.init.js (which comes from ModelAdmin.media).  At that point
        # window.jQuery is already undefined (noConflict removed it), so
        # select2.full.js's factory(jQuery) call registers nothing and every
        # subsequent $element.select2(...) call fails.
        #
        # Fix: mirror the ordering used by Django's own AutocompleteSelect widget
        # (jquery.min.js → select2.full.js → jquery.init.js) so that Select2
        # extends window.jQuery *before* noConflict stores it as django.jQuery.
        self._patch_dal_select2_media()

        # Add YouTube embed support to the Tiptap rich text editor.
        from .tiptap import register_youtube_extension
        register_youtube_extension()

    @staticmethod
    def _patch_dal_select2_media():
        try:
            from dal_select2.widgets import Select2WidgetMixin
        except ImportError:
            return

        from django import forms
        from django.conf import settings

        original_media = Select2WidgetMixin.media.fget

        def patched_media(self):
            extra = '' if settings.DEBUG else '.min'
            original = original_media(self)
            # Rebuild JS list with jquery → select2 → jquery.init ahead of the
            # rest so that Select2 is on window.jQuery before noConflict runs.
            prefix = [
                'admin/js/vendor/jquery/jquery%s.js' % extra,
                'admin/js/vendor/select2/select2.full%s.js' % extra,
                'admin/js/jquery.init.js',
            ]
            rest = [
                js for js in original._js
                if js not in prefix and js != 'admin/js/vendor/select2/select2.full.js'
            ]
            return forms.Media(js=prefix + rest, css=original._css)

        Select2WidgetMixin.media = property(patched_media)
