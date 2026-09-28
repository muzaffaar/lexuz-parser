from django.contrib import admin

from .models import EmbeddingProfile


@admin.register(EmbeddingProfile)
class EmbeddingProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "model_name", "model_version", "dimension", "distance_metric", "is_active")
    list_filter = ("is_active",)
