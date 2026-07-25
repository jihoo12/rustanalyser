"""Extracts struct/enum/trait/impl/function/etc. definitions from Rust source
using tree-sitter, and yields dicts ready to be inserted into the DB.
"""

from dataclasses import dataclass, field
from typing import Optional, List

import tree_sitter_rust as tsrust
from tree_sitter import Language, Parser, Node

RUST_LANGUAGE = Language(tsrust.language())

TOP_LEVEL_KINDS = {
    "struct_item": "struct",
    "enum_item": "enum",
    "trait_item": "trait",
    "impl_item": "impl",
    "function_item": "function",
    "function_signature_item": "function_sig",  # trait method decl, no body
    "const_item": "const",
    "static_item": "static",
    "mod_item": "mod",
    "macro_definition": "macro",
    "type_item": "type_alias",
    "union_item": "union",
}

# node types that can precede an item as "attached" metadata (doc comments,
# attributes) which we walk backwards over to build doc/attributes strings.
COMMENT_TYPES = {"line_comment", "block_comment"}
ATTRIBUTE_TYPES = {"attribute_item", "inner_attribute_item"}


@dataclass
class ExtractedItem:
    kind: str
    name: str
    start_line: int
    end_line: int
    source: str
    target: Optional[str] = None
    trait_name: Optional[str] = None
    visibility: Optional[str] = None
    signature: Optional[str] = None
    doc: Optional[str] = None
    attributes: Optional[str] = None
    children: List["ExtractedItem"] = field(default_factory=list)


def _text(node: Optional[Node], src: bytes) -> Optional[str]:
    if node is None:
        return None
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _leading_metadata(node: Node, src: bytes):
    """Walk backwards over preceding sibling comment/attribute nodes attached
    to `node` (no blank-line gap) and return (doc_text, attrs_text)."""
    doc_lines = []
    attr_lines = []
    sib = node.prev_sibling
    collected = []
    while sib is not None and (sib.type in COMMENT_TYPES or sib.type in ATTRIBUTE_TYPES):
        collected.append(sib)
        sib = sib.prev_sibling
    collected.reverse()
    for n in collected:
        text = _text(n, src)
        if n.type in COMMENT_TYPES:
            if text.startswith("///") or text.startswith("//!") or text.startswith("/**"):
                doc_lines.append(text)
        elif n.type in ATTRIBUTE_TYPES:
            attr_lines.append(text)
    doc = "\n".join(doc_lines) if doc_lines else None
    attrs = "\n".join(attr_lines) if attr_lines else None
    return doc, attrs


def _visibility(node: Node, src: bytes) -> Optional[str]:
    for child in node.children:
        if child.type == "visibility_modifier":
            return _text(child, src)
    return None


def _function_signature(node: Node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    if body is not None:
        end = body.start_byte
    else:
        end = node.end_byte
    sig = src[node.start_byte:end].decode("utf-8", errors="replace")
    return sig.strip().rstrip("{").strip()


def _extract_function(node: Node, src: bytes, kind: str) -> ExtractedItem:
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) or "<anonymous>"
    doc, attrs = _leading_metadata(node, src)
    return ExtractedItem(
        kind=kind,
        name=name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        source=_text(node, src),
        visibility=_visibility(node, src),
        signature=_function_signature(node, src),
        doc=doc,
        attributes=attrs,
    )


def _extract_simple(node: Node, src: bytes, kind: str) -> ExtractedItem:
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) or "<anonymous>"
    doc, attrs = _leading_metadata(node, src)
    return ExtractedItem(
        kind=kind,
        name=name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        source=_text(node, src),
        visibility=_visibility(node, src),
        doc=doc,
        attributes=attrs,
    )


def _extract_impl(node: Node, src: bytes) -> ExtractedItem:
    type_node = node.child_by_field_name("type")
    trait_node = node.child_by_field_name("trait")
    target = _text(type_node, src) or "<unknown>"
    trait_name = _text(trait_node, src)
    doc, attrs = _leading_metadata(node, src)
    display_name = f"impl {trait_name + ' for ' if trait_name else ''}{target}"
    item = ExtractedItem(
        kind="impl",
        name=display_name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        source=_text(node, src),
        target=target,
        trait_name=trait_name,
        doc=doc,
        attributes=attrs,
    )
    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.named_children:
            if child.type in ("function_item", "function_signature_item"):
                method = _extract_function(
                    child, src, "method" if child.type == "function_item" else "method_sig"
                )
                method.target = target
                method.trait_name = trait_name
                item.children.append(method)
            elif child.type == "const_item":
                c = _extract_simple(child, src, "assoc_const")
                c.target = target
                item.children.append(c)
            elif child.type == "type_item":
                t = _extract_simple(child, src, "assoc_type")
                t.target = target
                item.children.append(t)
    return item


def _extract_trait(node: Node, src: bytes) -> ExtractedItem:
    item = _extract_simple(node, src, "trait")
    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.named_children:
            if child.type in ("function_item", "function_signature_item"):
                method = _extract_function(
                    child, src, "method" if child.type == "function_item" else "method_sig"
                )
                method.target = item.name
                item.children.append(method)
    return item


def _extract_mod(node: Node, src: bytes) -> ExtractedItem:
    item = _extract_simple(node, src, "mod")
    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.named_children:
            item.children.extend(_extract_top_level_node(child, src))
    return item


def _extract_top_level_node(node: Node, src: bytes) -> List[ExtractedItem]:
    t = node.type
    if t == "impl_item":
        return [_extract_impl(node, src)]
    if t == "trait_item":
        return [_extract_trait(node, src)]
    if t == "mod_item":
        # only recurse if it's an inline module (has a body); `mod foo;` has none
        if node.child_by_field_name("body") is not None:
            return [_extract_mod(node, src)]
        return [_extract_simple(node, src, "mod_decl")]
    if t in ("function_item", "function_signature_item"):
        return [_extract_function(node, src, TOP_LEVEL_KINDS[t])]
    if t in TOP_LEVEL_KINDS:
        return [_extract_simple(node, src, TOP_LEVEL_KINDS[t])]
    return []


def extract_file(source_code: bytes) -> List[ExtractedItem]:
    parser = Parser(RUST_LANGUAGE)
    tree = parser.parse(source_code)
    items: List[ExtractedItem] = []
    for child in tree.root_node.named_children:
        items.extend(_extract_top_level_node(child, source_code))
    return items
