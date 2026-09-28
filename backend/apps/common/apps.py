from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _apply_grants(sender, using=None, **kwargs):
    from .db import apply_role_grants

    apply_role_grants(using or "default")


class CommonConfig(AppConfig):
    name = "apps.common"
    label = "common"

    def ready(self):
        # Table privileges follow the model list, so they are (re)applied after every
        # migrate instead of being frozen inside individual migrations.
        post_migrate.connect(_apply_grants, sender=self, dispatch_uid="yurist.apply_role_grants")
