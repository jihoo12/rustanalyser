"""Tests for the database layer."""

import os
import tempfile

import pytest

from rust_analyzer.db import RustCodeDB
from rust_analyzer.exceptions import DatabaseError


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def db(db_path):
    instance = RustCodeDB(db_path)
    yield instance
    instance.close()


class TestFileManagement:
    def test_upsert_new_file(self, db):
        file_id = db.upsert_file("/foo/bar.rs", 1000.0, "abc123")
        assert file_id > 0

    def test_upsert_existing_file_unchanged(self, db):
        fid1 = db.upsert_file("/foo/bar.rs", 1000.0, "abc123")
        fid2 = db.upsert_file("/foo/bar.rs", 1000.0, "abc123")
        assert fid1 == fid2

    def test_upsert_file_changed_clears_items(self, db):
        fid = db.upsert_file("/foo/bar.rs", 1000.0, "abc123")
        db.insert_item(
            file_id=fid, kind="function", name="foo",
            start_line=1, end_line=5, source="fn foo() {}",
        )
        db.commit()
        fid2 = db.upsert_file("/foo/bar.rs", 1001.0, "def456")
        assert fid == fid2
        items = db.list_items(name="foo")
        assert len(items) == 0

    def test_file_unchanged(self, db):
        db.upsert_file("/foo/bar.rs", 1000.0, "abc123")
        assert db.file_unchanged("/foo/bar.rs", "abc123") is True
        assert db.file_unchanged("/foo/bar.rs", "def456") is False
        assert db.file_unchanged("/foo/baz.rs", "abc123") is False

    def test_update_file_lines(self, db):
        fid = db.upsert_file("/foo/bar.rs", 1000.0, "abc123")
        db.update_file_lines(fid, 42)
        db.commit()

    def test_context_manager(self, db_path):
        with RustCodeDB(db_path) as db:
            db.upsert_file("/test.rs", 0.0, "hash")
        # Should be closed now, operations should fail
        with pytest.raises(Exception):
            db.conn.execute("SELECT 1")


class TestItemManagement:
    def test_insert_item(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        item_id = db.insert_item(
            file_id=fid, kind="function", name="main",
            start_line=1, end_line=3, source="fn main() {}",
        )
        assert item_id > 0

    def test_insert_item_with_parent(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        parent_id = db.insert_item(
            file_id=fid, kind="impl", name="impl Foo",
            start_line=1, end_line=10, source="impl Foo {}",
        )
        child_id = db.insert_item(
            file_id=fid, kind="method", name="bar",
            start_line=2, end_line=4, source="fn bar(&self) {}",
            parent_id=parent_id,
        )
        assert child_id > 0

    def test_insert_item_with_complexity(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        item_id = db.insert_item(
            file_id=fid, kind="function", name="complex",
            start_line=1, end_line=20, source="fn complex() {}",
            cyclomatic_complexity=8,
            cognitive_complexity=12,
            lines_of_code=20,
        )
        item = db.get_item(item_id)
        assert item["cyclomatic_complexity"] == 8
        assert item["cognitive_complexity"] == 12

    def test_insert_generic_params(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        item_id = db.insert_item(
            file_id=fid, kind="struct", name="Registry",
            start_line=1, end_line=5, source="struct Registry<K, V> {}",
        )
        db.insert_generic_params(item_id, [
            ("K", "", None),
            ("V", "Clone + Debug", None),
        ])
        db.commit()
        gens = db.get_generics(item_id)
        assert len(gens) == 2
        assert gens[0]["name"] == "K"
        assert gens[1]["name"] == "V"
        assert "Clone" in gens[1]["bounds"]

    def test_insert_lifetime_params(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        item_id = db.insert_item(
            file_id=fid, kind="function", name="borrow",
            start_line=1, end_line=2, source="fn borrow<'a>(x: &'a str) {}",
        )
        db.insert_lifetime_params(item_id, [("'a", "")])
        db.commit()
        lts = db.get_lifetimes(item_id)
        assert len(lts) == 1
        assert lts[0]["name"] == "'a"


class TestUseAndExtern:
    def test_insert_use_decl(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        use_id = db.insert_use_decl(
            fid, "std::collections::HashMap", None, False, 1, 1,
        )
        assert use_id > 0

    def test_insert_extern_crate(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        ext_id = db.insert_extern_crate(fid, "serde", None, 1, 1)
        assert ext_id > 0

    def test_get_extern_crates(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        db.insert_extern_crate(fid, "serde", None, 1, 1)
        db.insert_extern_crate(fid, "tokio", None, 2, 2)
        db.commit()
        crates = db.all_extern_crates()
        assert len(crates) == 2
        names = [c["name"] for c in crates]
        assert "serde" in names
        assert "tokio" in names


class TestCallGraph:
    def test_insert_and_resolve_calls(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        fn1_id = db.insert_item(
            file_id=fid, kind="function", name="caller",
            start_line=1, end_line=3, source="fn caller() { callee(); }",
        )
        fn2_id = db.insert_item(
            file_id=fid, kind="function", name="callee",
            start_line=5, end_line=7, source="fn callee() {}",
        )
        db.insert_call(fn1_id, "callee", 2)
        db.commit()
        total, resolved = db.resolve_calls()
        assert resolved >= 1

    def test_call_graph_stats(self, db):
        total, resolved = db.call_graph_stats()
        assert total == 0
        assert resolved == 0

    def test_get_calls_from(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        fn_id = db.insert_item(
            file_id=fid, kind="function", name="main",
            start_line=1, end_line=3, source="fn main() {}",
        )
        db.insert_call(fn_id, "foo", 2)
        db.commit()
        calls = db.get_calls_from(fn_id)
        assert len(calls) == 1

    def test_get_calls_to(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        fn1 = db.insert_item(
            file_id=fid, kind="function", name="a",
            start_line=1, end_line=2, source="fn a() {}",
        )
        fn2 = db.insert_item(
            file_id=fid, kind="function", name="b",
            start_line=3, end_line=4, source="fn b() { a(); }",
        )
        db.insert_call(fn2, "a", 3)
        db.commit()
        db.resolve_calls()
        calls = db.get_calls_to(fn1)
        assert len(calls) >= 1


class TestQueries:
    def test_list_items(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        db.insert_item(
            file_id=fid, kind="function", name="foo",
            start_line=1, end_line=2, source="fn foo() {}",
        )
        db.insert_item(
            file_id=fid, kind="struct", name="Bar",
            start_line=5, end_line=8, source="struct Bar {}",
        )
        db.commit()
        all_items = db.list_items()
        assert len(all_items) == 2
        fns = db.list_items(kind="function")
        assert len(fns) == 1

    def test_list_items_by_name(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        db.insert_item(
            file_id=fid, kind="function", name="parse_token",
            start_line=1, end_line=2, source="fn parse_token() {}",
        )
        db.insert_item(
            file_id=fid, kind="function", name="render",
            start_line=3, end_line=4, source="fn render() {}",
        )
        db.commit()
        rows = db.list_items(name="parse")
        assert len(rows) == 1
        assert rows[0]["name"] == "parse_token"

    def test_get_methods_of(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        impl_id = db.insert_item(
            file_id=fid, kind="impl", name="impl Point",
            start_line=1, end_line=10, source="impl Point {}",
        )
        db.insert_item(
            file_id=fid, kind="method", name="distance",
            start_line=2, end_line=4, source="fn distance(&self) {}",
            parent_id=impl_id, target="Point",
        )
        db.commit()
        methods = db.get_methods_of("Point")
        assert len(methods) == 1
        assert methods[0]["name"] == "distance"

    def test_stats(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        db.insert_item(
            file_id=fid, kind="function", name="foo",
            start_line=1, end_line=2, source="fn foo() {}",
        )
        db.commit()
        n_files, rows = db.stats()
        assert n_files == 1
        assert len(rows) == 1

    def test_complexity_report(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        db.insert_item(
            file_id=fid, kind="function", name="simple",
            start_line=1, end_line=2, source="fn simple() {}",
            cyclomatic_complexity=1,
        )
        db.insert_item(
            file_id=fid, kind="function", name="complex",
            start_line=5, end_line=20, source="fn complex() {}",
            cyclomatic_complexity=10,
        )
        db.commit()
        report = db.complexity_report(min_complexity=5)
        assert len(report) == 1
        assert report[0]["name"] == "complex"

    def test_api_surface(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        db.insert_item(
            file_id=fid, kind="function", name="pub_fn",
            start_line=1, end_line=2, source="pub fn pub_fn() {}",
            is_pub=1,
        )
        db.insert_item(
            file_id=fid, kind="function", name="priv_fn",
            start_line=3, end_line=4, source="fn priv_fn() {}",
            is_pub=0,
        )
        db.commit()
        surface = db.api_surface()
        assert len(surface) == 1
        assert surface[0]["name"] == "pub_fn"

    def test_most_complex_functions(self, db):
        fid = db.upsert_file("/test.rs", 0.0, "h")
        db.insert_item(
            file_id=fid, kind="function", name="a",
            start_line=1, end_line=2, source="fn a() {}",
            cyclomatic_complexity=1,
        )
        db.insert_item(
            file_id=fid, kind="function", name="b",
            start_line=5, end_line=20, source="fn b() {}",
            cyclomatic_complexity=15,
        )
        db.commit()
        top = db.most_complex_functions(limit=1)
        assert len(top) == 1
        assert top[0]["name"] == "b"

    def test_largest_files(self, db):
        fid1 = db.upsert_file("/a.rs", 0.0, "h1")
        fid2 = db.upsert_file("/b.rs", 0.0, "h2")
        for _ in range(5):
            db.insert_item(
                file_id=fid1, kind="function", name="f",
                start_line=1, end_line=2, source="fn f() {}",
                lines_of_code=10,
            )
        db.insert_item(
            file_id=fid2, kind="function", name="g",
            start_line=1, end_line=2, source="fn g() {}",
            lines_of_code=5,
        )
        db.commit()
        top = db.largest_files(limit=1)
        assert len(top) == 1
        assert top[0]["path"] == "/a.rs"
