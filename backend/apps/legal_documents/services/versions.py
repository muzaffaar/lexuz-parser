"""Version chain maintenance (TZ 45-47, 77; critical defects 7-9).

`rebuild_validity` is the single place that enforces the invariants:
  * at most one is_current version per document: the one the site serves without ?ONDATE= (a POINTER,
    never inferred from sort position);
  * validity windows form a gap-free chain ordered by start date: each version ends the day before the
    next begins; the last one is open-ended unless the act lost force;
  * start dates are strictly increasing. A version whose start date is only *our best guess*
    (adoption/publication/card date, first-seen date) is nudged forward when it would collide with an
    earlier edition; two *authoritative* edition dates that collide are a data error and raise;
  * the current text sorts after every edition that has already started (a guessed start can never
    precede a real edition), but BEFORE an edition dated in the future relative to `reference`
    (a scheduled amendment must not remove the act from today's search).
"""
from datetime import date, timedelta

from django.db import connection

from ..constants import ValidFromSource
from ..models import LegalDocumentVersion

_STRONG = {ValidFromSource.EDITION}
ONE_DAY = timedelta(days=1)


class VersionConflict(Exception):
    """Two authoritative editions claim the same start date, or a historical edition changed."""


def next_version_number(document) -> int:
    last = document.versions.order_by("-version_number").values_list("version_number", flat=True).first()
    return (last or 0) + 1


def rebuild_validity(
    document, current_version: LegalDocumentVersion | None, expire_on: date | None = None, reference: date | None = None
) -> None:
    """Recompute windows and the current flag. `reference` is "today" for the observation being applied
    (callers pass the observation date so results do not depend on when the code happens to run)."""
    reference = reference or date.today()
    versions = list(LegalDocumentVersion.objects.select_for_update().filter(document=document))
    if not versions:
        return
    current_id = getattr(current_version, "id", None)

    def group(v):
        if v.id == current_id:
            return 1
        return 2 if v.valid_from > reference else 0  # future-dated editions come after the current text

    versions.sort(key=lambda v: (group(v), v.valid_from, v.version_number))

    previous = None
    for v in versions:
        if previous is not None and v.valid_from <= previous.valid_from:
            if v.valid_from_source in _STRONG and previous.valid_from_source in _STRONG:
                raise VersionConflict(
                    f"Editions {previous.version_number} and {v.version_number} of {document.external_id} "
                    f"both start on {v.valid_from}"
                )
            v.valid_from = previous.valid_from + ONE_DAY
            v.valid_from_source = ValidFromSource.OBSERVED_ADJUSTED
        previous = v

    for i, v in enumerate(versions):
        if i + 1 < len(versions):
            v.valid_to = versions[i + 1].valid_from - ONE_DAY
        elif expire_on is None:
            v.valid_to = None
        else:
            # an act cannot lose force before this text began: a one-day window, never an open-ended one
            v.valid_to = max(expire_on, v.valid_from)

    # Clear the flag first: the partial unique index (one current per document) is not deferrable.
    LegalDocumentVersion.objects.filter(document=document, is_current=True).exclude(pk=current_id).update(is_current=False)
    for v in versions:
        LegalDocumentVersion.objects.filter(pk=v.pk).update(
            valid_from=v.valid_from,
            valid_to=v.valid_to,
            valid_from_source=v.valid_from_source,
            is_current=v.id == current_id,
        )
    # Surface an overlap now (inside the caller's try/except) rather than at COMMIT.
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS no_overlapping_versions IMMEDIATE")
        cur.execute("SET CONSTRAINTS no_overlapping_versions DEFERRED")


def version_as_of(document, as_of: date):
    """The edition of `document` in force on `as_of` (TZ 77), or None."""
    return document.versions.filter(valid_period__contains=as_of).first()
