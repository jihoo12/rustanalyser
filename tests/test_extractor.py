"""Tests for the extractor module."""

import pytest

from rust_analyzer.extractor import (
    CallEdge,
    ExtractedItem,
    FileExtraction,
    GenericParam,
    LifetimeParam,
    ComplexityMetrics,
    extract_file,
    extract_calls,
    _extract_generic_params,
    _compute_complexity,
)


SAMPLE_RS = b"""\
use std::collections::HashMap;
use serde::{Serialize, Deserialize};
extern crate log;

/// A point in 2D space.
#[derive(Debug, Clone)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

impl Point {
    /// Create a new point.
    pub fn new(x: f64, y: f64) -> Self {
        Self { x, y }
    }

    pub fn distance(&self, other: &Point) -> f64 {
        let dx = self.x - other.x;
        let dy = self.y - other.y;
        (dx * dx + dy * dy).sqrt()
    }
}

pub trait Drawable {
    fn draw(&self);
    fn bounding_box(&self) -> (f64, f64, f64, f64);
}

impl Drawable for Point {
    fn draw(&self) {
        println!("Drawing point at ({}, {})", self.x, self.y);
    }

    fn bounding_box(&self) -> (f64, f64, f64, f64) {
        (self.x, self.y, self.x, self.y)
    }
}

pub enum Color {
    Red,
    Green,
    Blue,
    Rgb(u8, u8, u8),
}

pub const MAX_POINTS: usize = 1000;
pub static ORIGIN: Point = Point { x: 0.0, y: 0.0 };

pub fn complex_function(x: i32) -> String {
    if x > 0 {
        if x > 100 {
            return "large".to_string();
        } else if x > 50 {
            return "medium".to_string();
        } else {
            return "small".to_string();
        }
    } else if x < -100 {
        match x {
            -200..=-101 => "very negative".to_string(),
            _ => "negative".to_string(),
        }
    } else {
        for i in 0..x.abs() {
            if i % 2 == 0 {
                continue;
            }
        }
        "zero-ish".to_string()
    }
}

mod inner {
    pub fn helper() -> i32 {
        42
    }
}

macro_rules! test_macro {
    ($x:expr) => { $x + 1 };
}
"""


class TestExtraction:
    def test_extract_file_returns_file_extraction(self) -> None:
        result = extract_file(SAMPLE_RS)
        assert isinstance(result, FileExtraction)
        assert result.total_lines > 0

    def test_extract_structs(self) -> None:
        result = extract_file(SAMPLE_RS)
        structs = [i for i in result.items if i.kind == "struct"]
        assert len(structs) >= 1
        assert structs[0].name == "Point"

    def test_extract_enums(self) -> None:
        result = extract_file(SAMPLE_RS)
        enums = [i for i in result.items if i.kind == "enum"]
        assert len(enums) >= 1
        assert enums[0].name == "Color"

    def test_extract_traits(self) -> None:
        result = extract_file(SAMPLE_RS)
        traits = [i for i in result.items if i.kind == "trait"]
        assert len(traits) >= 1
        assert traits[0].name == "Drawable"

    def test_extract_impls(self) -> None:
        result = extract_file(SAMPLE_RS)
        impls = [i for i in result.items if i.kind == "impl"]
        assert len(impls) >= 2

    def test_extract_methods(self) -> None:
        result = extract_file(SAMPLE_RS)
        # Point impl methods (inherent impl + Drawable impl = 2 impls for Point)
        point_impls = [i for i in result.items if i.kind == "impl" and "Point" in i.name]
        assert len(point_impls) >= 1
        all_methods = []
        for impl in point_impls:
            all_methods.extend([c for c in impl.children if c.kind == "method"])
        assert len(all_methods) >= 4

    def test_extract_functions(self) -> None:
        result = extract_file(SAMPLE_RS)
        fns = [i for i in result.items if i.kind == "function"]
        assert any(f.name == "complex_function" for f in fns)

    def test_extract_mods(self) -> None:
        result = extract_file(SAMPLE_RS)
        mods = [i for i in result.items if i.kind == "mod"]
        assert len(mods) >= 1
        assert mods[0].name == "inner"

    def test_extract_macros(self) -> None:
        result = extract_file(SAMPLE_RS)
        macros = [i for i in result.items if i.kind == "macro"]
        assert len(macros) >= 1

    def test_extract_visibility(self) -> None:
        result = extract_file(SAMPLE_RS)
        pub_structs = [i for i in result.items if i.kind == "struct" and i.is_pub]
        assert len(pub_structs) >= 1

    def test_extract_doc_comments(self) -> None:
        result = extract_file(SAMPLE_RS)
        structs = [i for i in result.items if i.kind == "struct"]
        assert structs[0].doc is not None
        assert "point" in structs[0].doc.lower()

    def test_extract_attributes(self) -> None:
        result = extract_file(SAMPLE_RS)
        structs = [i for i in result.items if i.kind == "struct"]
        assert structs[0].attributes is not None
        assert "derive" in structs[0].attributes

    def test_extract_use_declarations(self) -> None:
        result = extract_file(SAMPLE_RS)
        assert len(result.use_declarations) >= 2
        paths = [u.path for u in result.use_declarations]
        assert "std::collections::HashMap" in paths

    def test_extract_extern_crates(self) -> None:
        result = extract_file(SAMPLE_RS)
        assert len(result.extern_crates) >= 1
        assert result.extern_crates[0].name == "log"

    def test_extract_generics_on_struct(self) -> None:
        code = b"pub struct Registry<K, V: Clone> { map: HashMap<K, V> }\n"
        result = extract_file(code)
        assert len(result.items) == 1
        item = result.items[0]
        assert len(item.generic_params) == 2
        assert item.generic_params[0].name == "K"
        assert item.generic_params[1].name == "V"
        assert "Clone" in item.generic_params[1].bounds

    def test_extract_lifetimes(self) -> None:
        code = b"pub fn borrow<'a>(x: &'a str) -> &'a str { x }\n"
        result = extract_file(code)
        assert len(result.items) == 1
        item = result.items[0]
        assert len(item.lifetime_params) == 1
        assert item.lifetime_params[0].name == "'a"


class TestComplexity:
    def test_simple_function(self) -> None:
        code = b"fn add(a: i32, b: i32) -> i32 { a + b }\n"
        result = extract_file(code)
        fn = result.items[0]
        assert fn.complexity is not None
        assert fn.complexity.cyclomatic == 1

    def test_if_branches(self) -> None:
        code = b"""\
fn check(x: i32) -> bool {
    if x > 0 {
        true
    } else if x < 0 {
        false
    } else {
        true
    }
}
"""
        result = extract_file(code)
        fn = result.items[0]
        assert fn.complexity is not None
        assert fn.complexity.cyclomatic >= 3  # base(1) + if + else if

    def test_match_complexity(self) -> None:
        code = b"""\
fn classify(x: i32) -> &'static str {
    match x {
        1 => "one",
        2 => "two",
        _ => "other",
    }
}
"""
        result = extract_file(code)
        fn = result.items[0]
        assert fn.complexity is not None
        assert fn.complexity.cyclomatic >= 2

    def test_loop_complexity(self) -> None:
        code = b"""\
fn iterate() {
    for i in 0..10 {
        if i % 2 == 0 {
            continue;
        }
    }
}
"""
        result = extract_file(code)
        fn = result.items[0]
        assert fn.complexity is not None
        assert fn.complexity.cyclomatic >= 3  # base + for + if


class TestCallExtraction:
    def test_extract_calls(self) -> None:
        code = b"""\
fn main() {
    foo();
    bar().baz();
    self.helper();
}
"""
        result = extract_file(code)
        fn = result.items[0]
        assert fn.body_node is not None
        calls = extract_calls(fn.body_node, code)
        assert len(calls) >= 2

    def test_extract_method_calls(self) -> None:
        code = b"""\
fn process(&self) {
    self.data.push(1);
    self.flush();
}
"""
        result = extract_file(code)
        fn = result.items[0]
        assert fn.body_node is not None
        calls = extract_calls(fn.body_node, code)
        method_calls = [c for c in calls if c.is_method_call]
        assert len(method_calls) >= 2


class TestGenericParam:
    def test_to_signature(self) -> None:
        gp = GenericParam(name="T", bounds=["Debug", "Clone"])
        sig = gp.to_signature()
        assert "T" in sig
        assert "Debug" in sig
        assert "Clone" in sig

    def test_to_signature_with_default(self) -> None:
        gp = GenericParam(name="T", bounds=[], default="i32")
        sig = gp.to_signature()
        assert "= i32" in sig
