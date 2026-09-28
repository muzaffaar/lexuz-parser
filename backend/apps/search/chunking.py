"""Legal-aware chunking (TZ 61-63). Pure functions over section trees; no database.

Rules
  * The unit is the structural node (article -> clause -> subclause), never "every N characters".
  * A node whose whole subtree fits the budget becomes one piece; a bigger node is decomposed into
    its own text + its children, each carrying the ancestors' headings as metadata.
  * Small consecutive pieces are merged, but never across a chapter / article / appendix boundary,
    and a new clause starts a new chunk once the current one is big enough (citation granularity).
  * An oversized *leaf* is split at table-row / sentence boundaries; only a single sentence longer
    than the whole budget is cut on a word boundary (flagged `forced_split`).
  * Signatures and registration notes are not retrieval material and are left out.

Token counts are heuristic estimates until the embedding model's tokenizer is fixed (TZ 65).
"""
import math
import re
from dataclasses import dataclass, field
from typing import Protocol

from apps.common.hashing import sha256_hex
from apps.legal_documents.constants import SectionType as T

CHUNKER_VERSION = 2  # 2: every chunk carries its legal document's identity (title, number, id, url) in metadata and search text

_HEADER_TYPES = {T.TITLE, T.FORM, T.BODY, T.NUMBER, T.PLACE_DATE}
_SKIP_TYPES = {T.SIGNATURE, T.NOTE}
_BOUNDARY_TYPES = {T.APPENDIX, T.PART, T.SECTION, T.CHAPTER, T.ARTICLE}
_UNIT_STARTS = _BOUNDARY_TYPES | {T.CLAUSE}
_HEADING_LABEL_TYPES = _BOUNDARY_TYPES
_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")
_CYR = re.compile(r"[Ѐ-ӿ]")


class TokenCounter(Protocol):
    name: str

    def count(self, text: str) -> int: ...


class HeuristicTokenCounter:
    """chars / k with k chosen by script (Cyrillic costs more tokens per char in byte-level BPEs)."""

    name = "heuristic-v1"

    def count(self, text: str) -> int:
        if not text:
            return 0
        cyr = len(_CYR.findall(text))
        k = 2.2 if cyr > len(text) * 0.4 else 3.2
        return max(1, math.ceil(len(text) / k))


@dataclass
class ChunkDraft:
    chunk_index: int
    text: str
    heading_path: list[str]
    sections: list  # SectionDrafts (or DB rows) whose text is inside the chunk, in order
    token_count: int
    kind: str = "body"  # header | body
    forced_split: bool = False

    @property
    def first_section(self):
        return self.sections[0] if self.sections else None

    @property
    def embedding_input(self) -> str:
        """What should be embedded / hashed: headings give the chunk its context (TZ 62)."""
        return "\n".join([*self.heading_path, self.text]) if self.heading_path else self.text

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.embedding_input)

    def metadata(self, counter_name: str) -> dict:
        orders = [s.order_index for s in self.sections]
        return {
            "kind": self.kind,
            "heading_path": self.heading_path,
            "section_orders": [min(orders), max(orders)] if orders else [],
            "anchors": [a for a in (s.source_anchor for s in self.sections) if a][:20],
            "forced_split": self.forced_split,
            "token_estimator": counter_name,
        }


@dataclass
class _Piece:
    sections: list
    text: str
    tokens: int
    heading_path: list[str]
    group: object
    starts_unit: bool
    atomic: bool = False  # already a size-limited split part: stands alone
    forced: bool = False


def _heading_label(node) -> str:
    if node.section_type in _HEADING_LABEL_TYPES:
        return re.sub(r"\s+", " ", node.text).strip()[:120]
    return (node.number or node.text[:40]).strip()


def _split_long_row(row: str, budget: int, counter: "TokenCounter") -> tuple[list[str], bool]:
    """Split one oversized table row by sentences of its longest cell, repeating the other cells (row number,
    category code...) on every piece so each piece still says which row it belongs to."""
    cells = row.split(" | ")
    if len(cells) < 2:
        return split_text(row, budget, counter)
    idx = max(range(len(cells)), key=lambda i: len(cells[i]))
    others = [c for i, c in enumerate(cells) if i != idx]
    room = budget - counter.count(" | ".join(others)) - 2
    if room < 30:  # the fixed cells alone eat the budget: fall back to plain text splitting
        return split_text(row, budget, counter)
    pieces, forced = split_text(cells[idx], room, counter)
    return [" | ".join([*cells[:idx], piece, *cells[idx + 1:]]) for piece in pieces], forced


def split_text(text: str, max_tokens: int, counter: TokenCounter) -> tuple[list[str], bool]:
    """Split oversized text on table-row / sentence boundaries. Returns (parts, forced_split)."""
    lines = text.split("\n")
    is_table = len(lines) > 1 and all(" | " in ln for ln in lines[: min(3, len(lines))])
    forced = False
    parts: list[str] = []
    if is_table:
        header, rows = lines[0], lines[1:]
        header_tokens = counter.count(header)
        expanded: list[str] = []
        for row in rows:  # a single row can be a whole paragraph (real acts have 80k-token tables): split inside it
            if header_tokens + counter.count(row) > max_tokens:
                pieces, was_forced = _split_long_row(row, max(max_tokens - header_tokens, 40), counter)
                forced = forced or was_forced
                expanded.extend(pieces)
            else:
                expanded.append(row)
        cur, cur_tokens = [], header_tokens
        for row in expanded:
            t = counter.count(row)
            if cur and cur_tokens + t > max_tokens:
                parts.append("\n".join([header, *cur]))
                cur, cur_tokens = [], header_tokens
            cur.append(row)
            cur_tokens += t
        if cur:
            parts.append("\n".join([header, *cur]))
        return parts, forced

    units = []
    for line in lines:
        units.extend(u for u in _SENTENCE_END.split(line) if u)
    cur, cur_tokens = [], 0
    for unit in units:
        t = counter.count(unit)
        if t > max_tokens:  # one sentence over budget: last resort, cut on word boundaries
            if cur:
                parts.append(" ".join(cur))
                cur, cur_tokens = [], 0
            words, buf = unit.split(" "), []
            for w in words:
                if buf and counter.count(" ".join([*buf, w])) > max_tokens:
                    parts.append(" ".join(buf))
                    buf = []
                buf.append(w)
            if buf:
                parts.append(" ".join(buf))
            forced = True
            continue
        if cur and cur_tokens + t > max_tokens:
            parts.append(" ".join(cur))
            cur, cur_tokens = [], 0
        cur.append(unit)
        cur_tokens += t
    if cur:
        parts.append(" ".join(cur))
    return parts, forced


_LABEL_ONLY = re.compile(r"[\s\d.()a-zA-Zа-яА-Я]{1,8}")


def _is_bare_label(chunk: "ChunkDraft") -> bool:
    """A lone clause/item number such as '40.' or '(a)' - a label whose body landed in the next chunk."""
    return (
        chunk.kind == "body"
        and len(chunk.sections) == 1
        and chunk.sections[0].section_type in (T.CLAUSE, T.SUBCLAUSE, T.ITEM)
        and bool(_LABEL_ONLY.fullmatch(chunk.text))
    )


def chunk_sections(sections: list, *, max_tokens: int, min_tokens: int, counter: TokenCounter | None = None) -> list[ChunkDraft]:
    counter = counter or HeuristicTokenCounter()
    children: dict = {}
    roots = []
    for s in sections:
        (children.setdefault(s.parent.id, []) if s.parent is not None else roots).append(s)

    tokens_cache: dict = {}

    def flatten(node):
        out = [node]
        for c in children.get(node.id, []):
            out.extend(flatten(c))
        return out

    def subtree_tokens(node):
        if node.id not in tokens_cache:
            tokens_cache[node.id] = sum(counter.count(s.text) for s in flatten(node) if s.section_type not in _SKIP_TYPES)
        return tokens_cache[node.id]

    pieces: list[_Piece] = []

    def add_leaf(node, heading, group):
        """One section on its own; splits when over budget."""
        text = node.text
        if not text or node.section_type in _SKIP_TYPES:
            return
        t = counter.count(text)
        starts = node.section_type in _UNIT_STARTS
        if t <= max_tokens:
            pieces.append(_Piece([node], text, t, heading, group, starts))
            return
        parts, forced = split_text(text, max_tokens, counter)
        for part in parts:
            pieces.append(_Piece([node], part, counter.count(part), heading, group, starts, atomic=True, forced=forced))

    def visit(node, heading, group):
        # act-level requisites are covered by the header chunk; a TITLE *inside* an appendix is content
        if (node.parent is None and node.section_type in _HEADER_TYPES) or node.section_type in _SKIP_TYPES:
            return
        group = node.id if node.section_type in _BOUNDARY_TYPES else group
        if subtree_tokens(node) <= max_tokens:
            flat = [s for s in flatten(node) if s.text and s.section_type not in _SKIP_TYPES]
            if not flat:
                return
            text = "\n".join(s.text for s in flat)
            joined = counter.count(text)
            # The estimator picks its chars-per-token ratio per text (Cyrillic vs. digits/tables), so parts that fit
            # separately can exceed the budget once joined: the JOINED count is what a chunk really costs.
            if joined <= max_tokens or len(flat) == 1:
                pieces.append(_Piece(flat, text, joined, heading, group, node.section_type in _UNIT_STARTS))
                return
        # too big: own text, then children, each with this node's heading
        add_leaf(node, heading, group)
        child_heading = heading + [_heading_label(node)]
        for child in children.get(node.id, []):
            visit(child, child_heading, group)

    header_sections = [s for s in roots if s.section_type in _HEADER_TYPES and s.text]
    for root in roots:
        visit(root, [], None)

    chunks: list[ChunkDraft] = []
    if header_sections:
        text = "\n".join(s.text for s in header_sections)
        chunks.append(ChunkDraft(0, text, [], header_sections, counter.count(text), kind="header"))

    buf: list[_Piece] = []

    def buf_tokens():
        return sum(p.tokens for p in buf)

    def flush():
        if not buf:
            return
        sections_in = []
        for p in buf:
            for s in p.sections:
                if s not in sections_in:
                    sections_in.append(s)
        text = "\n".join(p.text for p in buf)
        chunks.append(
            ChunkDraft(len(chunks), text, buf[0].heading_path, sections_in, counter.count(text), forced_split=any(p.forced for p in buf))
        )
        buf.clear()

    for piece in pieces:
        if piece.atomic:
            flush()
            buf.append(piece)
            flush()
            continue
        if buf:
            same_group = buf[-1].group == piece.group
            too_big = counter.count("\n".join([*(p.text for p in buf), piece.text])) > max_tokens
            unit_break = piece.starts_unit and buf_tokens() >= min_tokens
            if not same_group or too_big or unit_break:
                flush()
        buf.append(piece)
    flush()

    # A bare label such as "40." whose body is a big table lands alone; glue such fragments to the
    # chunk that follows them (may exceed the budget by a few tokens, which is the lesser evil).
    merged: list[ChunkDraft] = []
    carry: ChunkDraft | None = None
    for c in chunks:
        if carry is not None:
            c = ChunkDraft(0, carry.text + "\n" + c.text, carry.heading_path or c.heading_path,
                           [*carry.sections, *[s for s in c.sections if s not in carry.sections]],
                           carry.token_count + c.token_count, c.kind, carry.forced_split or c.forced_split)
            carry = None
        if _is_bare_label(c):
            carry = c
            continue
        merged.append(c)
    if carry is not None:
        merged.append(carry)
    for i, c in enumerate(merged):
        c.chunk_index = i
    return merged
