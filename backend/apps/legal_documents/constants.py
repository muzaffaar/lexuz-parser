from django.db import models


class Language(models.TextChoices):
    UZ = "uz", "Uzbek"
    RU = "ru", "Russian"
    EN = "en", "English"
    UNKNOWN = "", "Unknown"


class Script(models.TextChoices):
    CYRL = "cyrl", "Cyrillic"
    LATN = "latn", "Latin"
    UNKNOWN = "", "Unknown"


class DocumentStatus(models.TextChoices):
    ACTIVE = "active"
    EXPIRED = "expired"                       # lost legal force
    NOT_YET_EFFECTIVE = "not_yet_effective"
    DRAFT = "draft"
    UNKNOWN = "unknown"                       # source did not say; never guessed


class TextStatus(models.TextChoices):
    OK = "ok"                # full text captured
    STUB = "stub"            # source page has only header/signature (e.g. untranslated RU/EN pages)
    NEEDS_OCR = "needs_ocr"  # scanned PDF: no usable embedded text; must not be chunked/embedded
    EMPTY = "empty"


class Representation(models.TextChoices):
    HTML = "html"
    PDF_ONLY = "pdf_only"


class TextSource(models.TextChoices):
    HTML = "html"
    PDF_EMBEDDED = "pdf_embedded"
    OCR = "ocr"


class ValidFromSource(models.TextChoices):
    """Honest provenance of a version's start date. lex.uz does not publish a machine-readable
    'this text became valid on X' for the current edition, so we record where the date came from."""

    EDITION = "edition"                    # the site's own ?ONDATE= edition date
    CARD_EFFECTIVE = "card_effective"      # legal-analysis card "date of entry into force"
    PUBLISHED = "published"                # official publication date (an act cannot be in force before it)
    ADOPTED = "adopted"                    # adoption date from the act itself
    OBSERVED = "observed"                  # the day our parser first saw this text
    OBSERVED_ADJUSTED = "observed_adjusted"  # observed, nudged +1 day to avoid overlapping a sibling version


class SectionType(models.TextChoices):
    # header / requisites
    TITLE = "title"
    FORM = "form"            # "qarori", "buyrug'i"
    BODY = "body"            # adopting body
    NUMBER = "number"
    PLACE_DATE = "place_date"
    # structure
    APPENDIX = "appendix"
    CHAPTER = "chapter"      # roman-numeral / "bob" / "Глава" headings
    PART = "part"            # "qism" / "часть" heading-style
    SECTION = "section"      # "boʻlim" / "раздел"
    ARTICLE = "article"      # "modda" / "статья"
    CLAUSE = "clause"        # "1." numbered paragraph (band)
    SUBCLAUSE = "subclause"  # "1.2." (kichik band)
    ITEM = "item"            # "a)" / "(a)" enumeration
    PARAGRAPH = "paragraph"  # unnumbered running text (xatboshi)
    TABLE = "table"
    FOOTNOTE = "footnote"
    SIGNATURE = "signature"
    NOTE = "note"            # registration notes, "Kelishildi:" etc.
    PDF_PAGE = "pdf_page"


class RelationType(models.TextChoices):
    CITES = "cites"
    LANGUAGE_VARIANT = "language_variant"  # same act in another language / script
    EDITION = "edition"                    # link to another edition of the same act
    OTHER = "other"


class AttachmentRole(models.TextChoices):
    PRIMARY_PDF = "primary_pdf"  # the ONLY source of a PDF-only act (scan or text)
    PDF_EXPORT = "pdf_export"    # the site's PDF rendering of an HTML act, for side-by-side reading
    SOURCE_ZIP = "source_zip"    # the downloaded /files/<n>.zip the act's PDF was unpacked from (provenance)


class CardKind(models.TextChoices):
    PASSPORT = "passport"
    CARD1 = "card1"          # legal analysis card: type, status, effective dates
    CARD2 = "card2"          # classifier indexing
    BASREV = "basrev"        # documents revised by this one
    REVHIS = "revhis"        # documents that revised this one
    CORRESPONDENTS = "correspondents"
    RESPONDENTS = "respondents"
