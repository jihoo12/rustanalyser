"""Extracts Rust code entities using tree-sitter and computes static metrics.

Extracts: structs, enums, traits, impls, functions/methods, consts, statics,
mods, macros, type aliases, unions, use declarations, and extern crate
declarations.  Also computes cyclomatic complexity, line counts, and extracts
generic parameters and lifetime annotations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import tree_sitter_rust as tsrust
from tree_sitter import Language, Node, Parser

from .exceptions import ParseError
from .logging import get_logger

log = get_logger("extractor")

RUST_LANGUAGE = Language(tsrust.language())

# tree-sitter node types that represent top-level Rust items
TOP_LEVEL_KINDS: dict[str, str] = {
    "struct_item": "struct",
    "enum_item": "enum",
    "trait_item": "trait",
    "impl_item": "impl",
    "function_item": "function",
    "function_signature_item": "function_sig",
    "const_item": "const",
    "static_item": "static",
    "mod_item": "mod",
    "macro_definition": "macro",
    "type_item": "type_alias",
    "union_item": "union",
    "use_declaration": "use",
    "extern_crate_declaration": "extern_crate",
}

COMMENT_TYPES = frozenset({"line_comment", "block_comment"})
ATTRIBUTE_TYPES = frozenset({"attribute_item", "inner_attribute_item"})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GenericParam:
    """A single generic type parameter."""

    name: str
    bounds: list[str] = field(default_factory=list)
    default: Optional[str] = None

    def to_signature(self) -> str:
        parts = [self.name]
        if self.bounds:
            parts.append(f": {', '.join(self.bounds)}")
        if self.default:
            parts.append(f"= {self.default}")
        return "".join(parts)


@dataclass
class LifetimeParam:
    """A lifetime parameter (e.g. ``'a``)."""

    name: str
    bounds: list[str] = field(default_factory=list)


@dataclass
class ComplexityMetrics:
    """Static metrics computed from a function/method body."""

    cyclomatic: int = 1
    lines_of_code: int = 0
    cognitive: int = 0
    nesting_depth: int = 0
    num_branches: int = 0
    num_function_calls: int = 0


@dataclass
class ExtractedItem:
    """A single extracted code entity."""

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
    children: list[ExtractedItem] = field(default_factory=list)
    body_node: Optional[Node] = None  # transient, not persisted
    # new fields
    generic_params: list[GenericParam] = field(default_factory=list)
    lifetime_params: list[LifetimeParam] = field(default_factory=list)
    complexity: Optional[ComplexityMetrics] = None
    is_pub: bool = False
    is_const_fn: bool = False
    is_async: bool = False
    is_unsafe: bool = False


@dataclass
class UseDeclaration:
    """A ``use`` statement."""

    path: str
    alias: Optional[str] = None
    is_glob: bool = False
    start_line: int = 0
    end_line: int = 0


@dataclass
class ExternCrate:
    """An ``extern crate`` declaration."""

    name: str
    alias: Optional[str] = None
    start_line: int = 0
    end_line: int = 0


@dataclass
class CallEdge:
    """A detected function/method call in a body."""

    callee_name: str
    receiver: Optional[str]
    line: int
    is_method_call: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(node: Optional[Node], src: bytes) -> Optional[str]:
    if node is None:
        return None
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _leading_metadata(node: Node, src: bytes) -> tuple[Optional[str], Optional[str]]:
    """Walk backwards over preceding sibling comment/attribute nodes."""
    doc_lines: list[str] = []
    attr_lines: list[str] = []
    collected: list[Node] = []
    sib = node.prev_sibling
    while sib is not None and (sib.type in COMMENT_TYPES or sib.type in ATTRIBUTE_TYPES):
        collected.append(sib)
        sib = sib.prev_sibling
    collected.reverse()
    for n in collected:
        text = _text(n, src)
        if text is None:
            continue
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
    end = body.start_byte if body is not None else node.end_byte
    sig = src[node.start_byte:end].decode("utf-8", errors="replace")
    return sig.strip().rstrip("{").strip()


def _has_modifier(node: Node, modifier: str) -> bool:
    """Check if a function node has a modifier like 'async', 'const', 'unsafe'."""
    for child in node.children:
        if child.type == modifier:
            return True
        if child.type == "function_modifiers":
            mod_text = _text(child, None)
            if mod_text and modifier in mod_text:
                return True
    return False


# ---------------------------------------------------------------------------
# Generic / lifetime extraction
# ---------------------------------------------------------------------------

def _extract_generic_params(node: Node, src: bytes) -> tuple[list[GenericParam], list[LifetimeParam]]:
    """Extract generic type parameters and lifetime parameters from a node's
    optional ``type_parameters`` child (the ``<...>`` block)."""
    generics: list[GenericParam] = []
    lifetimes: list[LifetimeParam] = []

    params_node = node.child_by_field_name("type_parameters")
    if params_node is None:
        return generics, lifetimes

    for child in params_node.named_children:
        if child.type == "type_parameter":
            name_node = child.child_by_field_name("name")
            name = _text(name_node, src) or "?"
            bounds: list[str] = []
            default: Optional[str] = None
            for c in child.named_children:
                if c.type == "trait_bounds":
                    for bound in c.named_children:
                        bound_text = _text(bound, src)
                        if bound_text:
                            bounds.append(bound_text.strip())
                elif c.type == "type_bound":
                    bound_text = _text(c, src)
                    if bound_text:
                        bounds.append(bound_text.strip())
                elif c.type == "type":
                    default = _text(c, src)
            generics.append(GenericParam(name=name, bounds=bounds, default=default))
        elif child.type in ("lifetime", "lifetime_parameter"):
            name = _text(child, src) or "'?"
            lifetimes.append(LifetimeParam(name=name))
        elif child.type == "const_parameter":
            name_node = child.child_by_field_name("name")
            name = _text(name_node, src) or "?"
            generics.append(GenericParam(name=name))

    return generics, lifetimes


# ---------------------------------------------------------------------------
# Complexity metrics
# ---------------------------------------------------------------------------

_BRANCH_NODES = frozenset({
    "if_expression",
    "if_statement",
    "match_expression",
    "match_arm",
    "loop_expression",
    "while_expression",
    "for_expression",
    "if_let_expression",
    "while_let_expression",
})

_FUNCTION_CALL_NODES = frozenset({"call_expression", "method_call_expression"})


def _compute_complexity(body_node: Optional[Node], src: bytes) -> ComplexityMetrics:
    """Compute cyclomatic complexity and other metrics for a function body."""
    metrics = ComplexityMetrics()
    if body_node is None:
        return metrics

    lines = src[body_node.start_byte:body_node.end_byte].decode("utf-8", errors="replace")
    metrics.lines_of_code = lines.count("\n") + 1

    max_depth = 0

    def _walk(node: Node, depth: int) -> None:
        nonlocal max_depth
        if depth > max_depth:
            max_depth = depth
        if node.type in _BRANCH_NODES and node.type != "match_arm":
            metrics.cyclomatic += 1
            metrics.num_branches += 1
        if node.type in _FUNCTION_CALL_NODES:
            metrics.num_function_calls += 1
        if node.type == "binary_expression":
            op = node.child_by_field_name("operator")
            if op is not None:
                op_text = _text(op, src) or ""
                if op_text in ("&&", "||"):
                    metrics.cyclomatic += 1
        for child in node.children:
            _walk(child, depth + 1)

    _walk(body_node, 0)
    metrics.nesting_depth = max_depth
    # Cognitive complexity: penalize nesting on top of branching
    metrics.cognitive = metrics.cyclomatic + max_depth
    return metrics


# ---------------------------------------------------------------------------
# Item extraction
# ---------------------------------------------------------------------------

def _extract_function(node: Node, src: bytes, kind: str) -> ExtractedItem:
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) or "<anonymous>"
    doc, attrs = _leading_metadata(node, src)
    generics, lifetimes = _extract_generic_params(node, src)
    body_node = node.child_by_field_name("body")
    complexity = _compute_complexity(body_node, src)
    return ExtractedItem(
        kind=kind,
        name=name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        source=_text(node, src) or "",
        visibility=_visibility(node, src),
        signature=_function_signature(node, src),
        doc=doc,
        attributes=attrs,
        body_node=body_node,
        generic_params=generics,
        lifetime_params=lifetimes,
        complexity=complexity,
        is_pub=_visibility(node, src) is not None,
        is_const_fn=_has_modifier(node, "const"),
        is_async=_has_modifier(node, "async"),
        is_unsafe=_has_modifier(node, "unsafe"),
    )


def _extract_simple(node: Node, src: bytes, kind: str) -> ExtractedItem:
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) or "<anonymous>"
    doc, attrs = _leading_metadata(node, src)
    generics, lifetimes = _extract_generic_params(node, src)
    return ExtractedItem(
        kind=kind,
        name=name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        source=_text(node, src) or "",
        visibility=_visibility(node, src),
        doc=doc,
        attributes=attrs,
        generic_params=generics,
        lifetime_params=lifetimes,
        is_pub=_visibility(node, src) is not None,
    )


def _extract_impl(node: Node, src: bytes) -> ExtractedItem:
    type_node = node.child_by_field_name("type")
    trait_node = node.child_by_field_name("trait")
    target = _text(type_node, src) or "<unknown>"
    trait_name = _text(trait_node, src)
    doc, attrs = _leading_metadata(node, src)
    generics, lifetimes = _extract_generic_params(node, src)
    display_name = f"impl {trait_name + ' for ' if trait_name else ''}{target}"
    item = ExtractedItem(
        kind="impl",
        name=display_name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        source=_text(node, src) or "",
        target=target,
        trait_name=trait_name,
        doc=doc,
        attributes=attrs,
        generic_params=generics,
        lifetime_params=lifetimes,
    )
    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.named_children:
            if child.type in ("function_item", "function_signature_item"):
                method = _extract_function(
                    child,
                    src,
                    "method" if child.type == "function_item" else "method_sig",
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
                    child,
                    src,
                    "method" if child.type == "function_item" else "method_sig",
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


def _extract_top_level_node(node: Node, src: bytes) -> list[ExtractedItem]:
    t = node.type
    if t == "impl_item":
        return [_extract_impl(node, src)]
    if t == "trait_item":
        return [_extract_trait(node, src)]
    if t == "mod_item":
        if node.child_by_field_name("body") is not None:
            return [_extract_mod(node, src)]
        return [_extract_simple(node, src, "mod_decl")]
    if t in ("function_item", "function_signature_item"):
        return [_extract_function(node, src, TOP_LEVEL_KINDS[t])]
    if t in TOP_LEVEL_KINDS:
        return [_extract_simple(node, src, TOP_LEVEL_KINDS[t])]
    return []


# ---------------------------------------------------------------------------
# Use / extern crate extraction
# ---------------------------------------------------------------------------

def _use_path_text(node: Node, src: bytes) -> str:
    """Recursively extract the full path text from a use_declaration's
    scoped_identifier tree."""
    if node.type == "scoped_identifier":
        path_child = node.child_by_field_name("path")
        name_child = node.child_by_field_name("name")
        if path_child is not None and name_child is not None:
            return _use_path_text(path_child, src) + "::" + (_text(name_child, src) or "")
        return _text(node, src) or ""
    return _text(node, src) or ""


def _extract_use_declarations(tree_root: Node, src: bytes) -> list[UseDeclaration]:
    """Extract all ``use`` declarations from the file."""
    results: list[UseDeclaration] = []

    def _walk(node: Node) -> None:
        if node.type == "use_declaration":
            # The child is typically a scoped_identifier or use_wildcard
            inner = None
            for child in node.named_children:
                if child.type in ("scoped_identifier", "use_wildcard", "use_as_clause", "identifier"):
                    inner = child
                    break
            if inner is None:
                for child in node.children:
                    if child.is_named:
                        inner = child
                        break

            text = ""
            is_glob = False
            alias: Optional[str] = None

            if inner is not None:
                if inner.type == "use_as_clause":
                    alias_node = inner.child_by_field_name("alias")
                    alias = _text(alias_node, src)
                    # The path part is the first named child
                    for child in inner.named_children:
                        if child != alias_node:
                            text = _use_path_text(child, src)
                            break
                elif inner.type == "use_wildcard":
                    text = _use_path_text(inner, src)
                    is_glob = True
                else:
                    text = _use_path_text(inner, src)

            results.append(UseDeclaration(
                path=text,
                alias=alias,
                is_glob=is_glob,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))
            return
        for child in node.children:
            _walk(child)

    _walk(tree_root)
    return results


def _extract_extern_crates(tree_root: Node, src: bytes) -> list[ExternCrate]:
    """Extract all ``extern crate`` declarations."""
    results: list[ExternCrate] = []
    for node in tree_root.named_children:
        if node.type == "extern_crate_declaration":
            name_node = node.child_by_field_name("name")
            name = _text(name_node, src) or "?"
            alias: Optional[str] = None
            for child in node.named_children:
                if child.type == "identifier" and child != name_node:
                    alias = _text(child, src)
            results.append(ExternCrate(
                name=name,
                alias=alias,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))
    return results


# ---------------------------------------------------------------------------
# Call extraction
# ---------------------------------------------------------------------------

def _call_target_name(fn_node: Node, src: bytes) -> Optional[tuple[str, Optional[str], bool]]:
    """Given the ``function`` child of a ``call_expression``, return
    ``(callee_name, receiver, is_method_call)`` or ``None``."""
    if fn_node.type == "identifier":
        return _text(fn_node, src), None, False
    if fn_node.type == "field_expression":
        field_node = fn_node.child_by_field_name("field")
        value = fn_node.child_by_field_name("value")
        name = _text(field_node, src)
        receiver = _text(value, src) if value is not None else None
        if receiver and len(receiver) > 40:
            receiver = receiver[:37] + "..."
        return name, receiver, True
    if fn_node.type == "scoped_identifier":
        path = fn_node.child_by_field_name("path")
        name_node = fn_node.child_by_field_name("name")
        name = _text(name_node, src)
        receiver = _text(path, src) if path is not None else None
        return name, receiver, False
    return None


def extract_calls(body_node: Node, src: bytes) -> list[CallEdge]:
    """Walk a function/method body and collect all direct call_expression
    targets, without recursing into nested function/closure definitions."""
    edges: list[CallEdge] = []

    def _walk(node: Node) -> None:
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            resolved = _call_target_name(fn_node, src) if fn_node is not None else None
            if resolved is not None:
                name, receiver, is_method = resolved
                edges.append(CallEdge(
                    callee_name=name,
                    receiver=receiver,
                    line=node.start_point[0] + 1,
                    is_method_call=is_method,
                ))
        if node.type in TOP_LEVEL_KINDS:
            return
        for child in node.children:
            _walk(child)

    _walk(body_node)
    return edges


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class FileExtraction:
    """Complete extraction result for a single Rust source file."""

    items: list[ExtractedItem]
    use_declarations: list[UseDeclaration]
    extern_crates: list[ExternCrate]
    total_lines: int = 0


def extract_file(source_code: bytes) -> FileExtraction:
    """Parse a Rust source file and extract all entities and metadata.

    Returns a ``FileExtraction`` containing items, use declarations,
    extern crate declarations, and total line count.

    Raises:
        ParseError: If tree-sitter fails to parse the source.
    """
    parser = Parser(RUST_LANGUAGE)
    tree = parser.parse(source_code)

    if tree.root_node.has_error:
        error_nodes = _find_error_nodes(tree.root_node)
        detail = "; ".join(
            f"line {n.start_point[0]+1}: {n.type}"
            for n in error_nodes[:3]
        )
        log.warning("Parse tree has errors: %s", detail)

    items: list[ExtractedItem] = []
    for child in tree.root_node.named_children:
        items.extend(_extract_top_level_node(child, source_code))

    uses = _extract_use_declarations(tree.root_node, source_code)
    externs = _extract_extern_crates(tree.root_node, source_code)
    total_lines = source_code.count(b"\n") + 1

    return FileExtraction(
        items=items,
        use_declarations=uses,
        extern_crates=externs,
        total_lines=total_lines,
    )


def _find_error_nodes(node: Node) -> list[Node]:
    """Collect all ERROR/MISSING nodes in the tree."""
    errors: list[Node] = []
    if node.type in ("ERROR", "MISSING"):
        errors.append(node)
    for child in node.children:
        errors.extend(_find_error_nodes(child))
    return errors
