//! A tiny sample crate used to test the Rust code analyzer.

use std::fmt;

/// A point in 2D space.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

impl Point {
    /// Creates a new point at the origin.
    pub fn origin() -> Self {
        Point { x: 0.0, y: 0.0 }
    }

    /// Creates a new point with the given coordinates.
    pub fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }

    /// Computes the Euclidean distance to another point.
    pub fn distance(&self, other: &Point) -> f64 {
        ((self.x - other.x).powi(2) + (self.y - other.y).powi(2)).sqrt()
    }
}

impl fmt::Display for Point {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "({}, {})", self.x, self.y)
    }
}

/// Shapes that can compute their own area.
pub trait Shape {
    fn area(&self) -> f64;
    fn name(&self) -> &str {
        "shape"
    }
}

/// A circle defined by a center point and radius.
pub struct Circle {
    pub center: Point,
    pub radius: f64,
}

impl Shape for Circle {
    fn area(&self) -> f64 {
        std::f64::consts::PI * self.radius * self.radius
    }

    fn name(&self) -> &str {
        "circle"
    }
}

/// Color enum used for rendering shapes.
#[derive(Debug, Clone, Copy)]
pub enum Color {
    Red,
    Green,
    Blue,
    Custom(u8, u8, u8),
}

pub const MAX_SHAPES: usize = 100;

mod utils {
    pub fn clamp(v: f64, lo: f64, hi: f64) -> f64 {
        if v < lo { lo } else if v > hi { hi } else { v }
    }

    pub struct Buffer {
        pub data: Vec<u8>,
    }
}

/// Adds two integers together.
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}
