from django.db import models
from django.db.models import Lookup


class LTreeField(models.TextField):
    """PostgreSQL ltree. Labels are [A-Za-z0-9_]; callers build them with ltree_label()."""

    description = "PostgreSQL ltree path"

    def db_type(self, connection):
        return "ltree"


class _LTreeBinary(Lookup):
    sql_operator = ""

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f"{lhs} {self.sql_operator} ({rhs})::ltree", [*lhs_params, *rhs_params]


@LTreeField.register_lookup
class DescendantOf(_LTreeBinary):
    """path__descendant_of='a.b' -> rows at or below a.b (ltree <@)."""

    lookup_name = "descendant_of"
    sql_operator = "<@"


@LTreeField.register_lookup
class AncestorOf(_LTreeBinary):
    lookup_name = "ancestor_of"
    sql_operator = "@>"


def ltree_label(text: str) -> str:
    """Make a single ltree label from arbitrary text."""
    import re

    label = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
    return label or "x"
