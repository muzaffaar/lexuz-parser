from django.contrib import admin


class ReadOnlyAdmin(admin.ModelAdmin):
    """Browse-only. Legal data is append-only in the database itself (triggers); the admin must not even
    offer add/change/delete, and it shows the raw rows so nothing is hidden behind a form."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    show_full_result_count = False


class ReadOnlyTabularInline(admin.TabularInline):
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
