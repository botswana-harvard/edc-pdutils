from __future__ import annotations

import sys
from copy import copy

import numpy as np
import pandas as pd
from django.apps import apps as django_apps
from django.db.models.constants import LOOKUP_SEP

from ..constants import ACTION_ITEM_COLUMNS, SYSTEM_COLUMNS
from .value_getter import ValueGetter, ValueGetterInvalidLookup


class ModelToDataframeError(Exception):
    pass


class ModelToDataframe:
    """
    m = ModelToDataframe(model='edc_pdutils.crf')
    my_df = m.dataframe
    """

    value_getter_cls = ValueGetter
    sys_field_names = ["_state", "_user_container_instance", "_domain_cache", "using", "slug"]
    edc_sys_columns = SYSTEM_COLUMNS
    action_item_columns = ACTION_ITEM_COLUMNS

    def __init__(
        self,
        model=None,
        queryset=None,
        query_filter=None,
        decrypt=None,
        drop_sys_columns=None,
        drop_action_item_columns=None,
        verbose=None,
        remove_timezone=None,
        include_historical: bool | None = None,
        **kwargs,
    ):
        self._columns = None
        self._list_columns = None
        self._encrypted_columns = None
        self._dataframe = pd.DataFrame()
        self.drop_sys_columns = True if drop_sys_columns is None else drop_sys_columns
        self.drop_action_item_columns = (
            True if drop_action_item_columns is None else drop_action_item_columns
        )
        self.decrypt = decrypt
        self.m2m_columns = []
        self.query_filter = query_filter or {}
        self.verbose = verbose
        self.remove_timezone = True if remove_timezone is None else remove_timezone
        if queryset:
            self.model = queryset.model._meta.label_lower
        else:
            self.model = model
        self.queryset = queryset or self.model_cls.objects.all()

    @property
    def dataframe(self):
        """Returns a pandas dataframe."""
        if self._dataframe.empty:
            row_count = self.queryset.count()
            if row_count > 0:
                if self.decrypt and self.has_encrypted_fields:
                    self._dataframe = self.get_dataframe_with_encrypted_fields(row_count)
                else:
                    self._dataframe = self.get_dataframe_without_encrypted_fields()
                self.merge_dataframe_with_pivoted_m2ms()
                self._dataframe.rename(columns=self.columns, inplace=True)
                self.convert_datetimetz_to_datetime()
                self.convert_bool_types_to_int()
                self.convert_unknown_types_to_str()
                self.convert_timedelta_to_secs()
                self._dataframe.fillna(value=np.nan, axis=0, inplace=True)
        return self._dataframe

    def get_dataframe_without_encrypted_fields(self) -> pd.DataFrame:
        queryset = self.queryset.values_list(*self.columns).filter(**self.query_filter)
        return pd.DataFrame(list(queryset), columns=[v for v in self.columns])

    def get_dataframe_with_encrypted_fields(self, row_count: int) -> pd.DataFrame:
        if self.verbose:
            sys.stdout.write("   PII will be decrypted! ... \n")
        queryset = self.queryset.filter(**self.query_filter)
        data = []
        for index, model_obj in enumerate(queryset.order_by("id")):
            if self.verbose:
                sys.stdout.write(f"   {self.model} {index + 1}/{row_count} ... \r")
            row = []
            for lookup, column_name in self.columns.items():
                try:
                    value = self.get_column_value(
                        model_obj=model_obj,
                        column_name=column_name,
                        lookup=lookup,
                    )
                except ValueGetterInvalidLookup as e:
                    print(f"{e.message}. Model: {model_obj._meta.label_lower}.")
                else:
                    row.append(value)
            data.append(row)
        return pd.DataFrame(data, columns=[col for col in self.columns])

    def convert_datetimetz_to_datetime(self) -> None:
        if self.remove_timezone:
            for column in list(self._dataframe.select_dtypes(include=["datetimetz"]).columns):
                self._dataframe[column] = pd.to_datetime(
                    self._dataframe[column]
                ).dt.tz_localize(None)

    def convert_bool_types_to_int(self):
        for column in list(self._dataframe.select_dtypes(include=["bool"]).columns):
            self._dataframe[column] = self._dataframe[column].replace({True: 1, False: 0})

    def convert_unknown_types_to_str(self):
        for column in list(self._dataframe.select_dtypes(include=["object"]).columns):
            self._dataframe[column].fillna(value="", inplace=True)
            self._dataframe[column] = self._dataframe[column].astype("str")

    def convert_timedelta_to_secs(self):
        for column in list(self._dataframe.select_dtypes(include=["timedelta64"]).columns):
            self._dataframe[column] = self._dataframe[column].dt.total_seconds()

    def move_sys_columns_to_end(self, columns: dict[str, str]):
        new_columns = {k: v for k, v in columns.items() if k not in SYSTEM_COLUMNS}
        if len(new_columns.keys()) != len(columns.keys()) and not self.drop_sys_columns:
            new_columns.update({k: k for k in SYSTEM_COLUMNS})
        return new_columns

    def move_action_item_columns(self, columns: dict[str, str]):
        new_columns = {k: v for k, v in columns.items() if k not in ACTION_ITEM_COLUMNS}
        if (
            len(new_columns.keys()) != len(columns.keys())
            and not self.drop_action_item_columns
        ):
            new_columns.update({k: k for k in ACTION_ITEM_COLUMNS})
        return new_columns

    def merge_dataframe_with_pivoted_m2ms(self) -> list[str]:
        """For each m2m field, merge in a single pivoted field."""
        m2m_fields = []
        for m2m_field in self.queryset.model._meta.many_to_many:
            m2m_fields.append(m2m_field.name)
            m2m_values_list = self.get_m2m_values_list(m2m_field)
            df_m2m = pd.DataFrame.from_records(m2m_values_list, columns=["id", m2m_field.name])
            df_m2m = df_m2m[df_m2m[m2m_field.name].notnull()]
            df_pivot = pd.pivot_table(
                df_m2m,
                values=m2m_field.name,
                index=["id"],
                aggfunc=lambda x: ";".join(str(v) for v in x),
            )
            self._dataframe = pd.merge(self._dataframe, df_pivot, how="left", on="id")
        return m2m_fields

    def get_m2m_values_list(self, m2m_field):
        m2m_values_list = {}
        for obj in self.queryset.model.objects.filter(**{f"{m2m_field.name}__isnull": False}):
            values = []
            for m2m_obj in getattr(obj, m2m_field.name).all():
                try:
                    values.append(m2m_obj.name)
                except AttributeError:
                    values.append(str(m2m_obj))
            m2m_values_list.update(**{str(obj.id): (obj.id, ";".join(values))})
        return tuple(v for v in m2m_values_list.values())

    def get_column_value(self, model_obj=None, column_name=None, lookup=None):
        """Returns the column value."""
        lookups = {column_name: lookup} if LOOKUP_SEP in lookup else None
        value_getter = self.value_getter_cls(
            field_name=column_name,
            model_obj=model_obj,
            lookups=lookups,
            encrypt=not self.decrypt,
        )
        return value_getter.value

    @property
    def model_cls(self):
        return django_apps.get_model(self.model)

    @property
    def has_encrypted_fields(self):
        """Returns True if at least one field uses encryption."""
        for field in self.queryset.model._meta.get_fields():
            if hasattr(field, "field_cryptor"):
                return True
        return False

    @property
    def columns(self) -> dict[str, str]:
        """Return a dictionary of column names."""
        if not self._columns:
            columns_list = list(self.queryset[0].__dict__.keys())
            for name in self.sys_field_names:
                try:
                    columns_list.remove(name)
                except ValueError:
                    pass
            if not self.decrypt and self.has_encrypted_fields:
                columns_list = [
                    col for col in columns_list if col not in self.encrypted_columns
                ]
            columns = dict(zip(columns_list, columns_list))
            for column_name in columns_list:
                if column_name.endswith("_visit") or column_name.endswith("_visit_id"):
                    columns = self.add_columns_for_subject_visit(
                        column_name=column_name, columns=columns
                    )
                if column_name.endswith("_requisition") or column_name.endswith(
                    "requisition_id"
                ):
                    columns = self.add_columns_for_subject_requisitions(columns)
            columns = self.add_list_model_name_columns(columns)
            columns = self.add_other_columns(columns)
            columns = self.add_subject_identifier_column(columns)
            columns = self.move_action_item_columns(columns)
            columns = self.move_sys_columns_to_end(columns)
            self._columns = columns
        return self._columns

    @property
    def encrypted_columns(self):
        """Return a list of column names that use encryption."""
        if not self._encrypted_columns:
            self._encrypted_columns = ["identity"]
            for field in self.queryset.model._meta.get_fields():
                if hasattr(field, "field_cryptor"):
                    self._encrypted_columns.append(field.name)
            self._encrypted_columns = list(set(self._encrypted_columns))
            self._encrypted_columns.sort()
        return self._encrypted_columns

    @property
    def list_columns(self):
        """Return a list of column names with fk to a list model."""
        from edc_list_data.model_mixins import ListModelMixin

        if not self._list_columns:
            list_columns = []
            for fld_cls in self.queryset.model._meta.get_fields():
                if (
                    hasattr(fld_cls, "related_model")
                    and fld_cls.related_model
                    and issubclass(fld_cls.related_model, (ListModelMixin,))
                ):
                    list_columns.append(fld_cls.attname)
            self._list_columns = list(set(list_columns))
        return self._list_columns

    @property
    def other_columns(self):
        """Return other column names with fk to a common models."""
        from edc_lab.models import Panel
        from edc_sites.models import Site

        related_model = [Site, Panel]
        if not self._list_columns:
            list_columns = []
            for fld_cls in self.queryset.model._meta.get_fields():
                if (
                    hasattr(fld_cls, "related_model")
                    and fld_cls.related_model
                    and fld_cls.related_model in related_model
                ):
                    list_columns.append(fld_cls.attname)
            self._list_columns = list(set(list_columns))
        return self._list_columns

    def add_subject_identifier_column(self, columns: dict[str, str]) -> dict[str, str]:
        if "subject_identifier" not in [v for v in columns.values()]:
            subject_identifier_column = None
            id_columns = [col.replace("_id", "") for col in columns if col.endswith("_id")]
            for col in id_columns:
                field = getattr(self.model_cls, col)
                if [
                    fld.name
                    for fld in field.field.related_model._meta.get_fields()
                    if fld.name == "subject_identifier"
                ]:
                    subject_identifier_column = f"{col}__subject_identifier"
                    break
            if subject_identifier_column:
                columns.update({subject_identifier_column: "subject_identifier"})
        return columns

    @staticmethod
    def add_columns_for_subject_visit(
        column_name: str = None, columns: dict[str, str] = None
    ) -> dict[str, str]:
        if "subject_identifier" not in [v for v in columns.values()]:
            columns.update(
                {f"{column_name}__appointment__subject_identifier": "subject_identifier"}
            )
        columns.update({f"{column_name}__appointment__appt_datetime": "appointment_datetime"})
        columns.update({f"{column_name}__appointment__visit_code": "visit_code"})
        columns.update(
            {f"{column_name}__appointment__visit_code_sequence": "visit_code_sequence"}
        )
        columns.update({f"{column_name}__report_datetime": "visit_datetime"})
        columns.update({f"{column_name}__reason": "visit_reason"})
        return columns

    @staticmethod
    def add_columns_for_subject_requisitions(columns: dict[str, str] = None) -> dict[str, str]:
        for col in copy(columns):
            if col.endswith("_requisition_id"):
                col_prefix = col.split("_")[0]
                column_name = col.split("_id")[0]
                columns.update(
                    {
                        f"{column_name}__requisition_identifier": (
                            f"{col_prefix}_requisition_identifier"
                        )
                    }
                )
                columns.update(
                    {f"{column_name}__drawn_datetime": f"{col_prefix}_drawn_datetime"}
                )
                columns.update({f"{column_name}__is_drawn": f"{col_prefix}_is_drawn"})
        return columns

    def add_list_model_name_columns(self, columns: dict[str, str] = None) -> dict[str, str]:
        for col in copy(columns):
            if col in self.list_columns:
                column_name = col.split("_id")[0]
                columns.update({f"{column_name}__name": f"{column_name}_name"})
        return columns

    def add_other_columns(self, columns: dict[str, str] = None) -> dict[str, str]:
        for col in copy(columns):
            if col in self.other_columns:
                column_name = col.split("_id")[0]
                columns.update({f"{column_name}__name": f"{column_name}_name"})
        return columns
