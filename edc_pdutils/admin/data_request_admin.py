from django.contrib import admin
from edc_base import get_utcnow

from ..admin_site import edc_pdutils_admin
from ..forms import DataRequestForm
from ..models import DataRequest, DataRequestHistory


class DataRequestHistoryInline(admin.TabularInline):

    model = DataRequestHistory

    list_display = ('emailed_to', 'exported_datetime', 'created')


@admin.register(DataRequest, site=edc_pdutils_admin)
class DataRequestAdmin(admin.ModelAdmin):

    actions = ['export_selected']

    inlines = [DataRequestHistoryInline]

    form = DataRequestForm

    fields = ('name', 'models', 'export_format', 'decrypt', )

    list_display = ('name', 'description', 'export_format',
                    'decrypt', 'user_created', 'created')

    list_filter = ('name', 'user_created', 'created')

    def export_selected(self, request, queryset):
        for obj in queryset:
            DataRequestHistory.objects.create(
                data_request=obj)
            rows_updated = queryset.update(exported_datetime=get_utcnow())
            if rows_updated == 1:
                message_bit = "1 data request was"
            else:
                message_bit = "%s data requests were" % rows_updated
            self.message_user(
                request, "%s successfully exported." % message_bit)
    export_selected.short_description = "Export selected data requests"
