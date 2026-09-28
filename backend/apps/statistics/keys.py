from apps.legal_documents.text.normalize import normalize_search


def category_key(text: str) -> str:
    """Key of a document kind as the card / statistics page names it. Cyrillic (Russian or Uzbek) and Latin
    spellings of the same Uzbek name fold together; Russian words are folded too, but identically on both sides
    of every lookup, which is all a key needs."""
    return normalize_search(text or "", language="uz")
