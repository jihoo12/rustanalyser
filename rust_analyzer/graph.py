"""Builds a call graph (execution-flow graph) from the data stored in the
RustCodeDB and renders it as Graphviz DOT/SVG/PNG or an interactive HTML
page (vis-network).
"""

import hashlib
import json
import re
import shutil
import subprocess
from collections import deque
from typing import Optional, Set, Tuple, List, Dict

from .db import RustCodeDB

NODE_COLORS = {
    "function": "#4C72B0",
    "method": "#55A868",
    "external": "#B0B0B0",
}

MAX_LABEL_LEN = 60


def _clean(s: Optional[str]) -> str:
    """Collapse whitespace/newlines and truncate. Anything downstream (DOT
    labels, HTML/JSON) treats the result as plain text to be escaped there."""
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > MAX_LABEL_LEN:
        s = s[:MAX_LABEL_LEN - 1] + "\u2026"
    return s


def _safe_id(*parts: Optional[str]) -> str:
    """Deterministic, DOT/HTML-safe node id built from arbitrary text."""
    key = "\x1f".join(p or "" for p in parts)
    return "n" + hashlib.md5(key.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _item_node_id(item_id: int) -> str:
    return f"item{item_id}"


def _external_node_id(name: str, receiver: Optional[str], is_method_call: bool) -> str:
    # Method-call receivers are usually arbitrary local variable names
    # (x.clone(), y.clone(), self.clone()...) so collapse purely by method
    # name, or every unique variable would become its own node. Scoped
    # calls (Type::method / module::func) have bounded, meaningful
    # receivers, so keep those distinct.
    if is_method_call:
        return _safe_id("ext_method", name)
    return _safe_id("ext_fn", receiver, name)


def _node_label(name: str, target: Optional[str]) -> str:
    name = _clean(name)
    target = _clean(target) if target else None
    return f"{target}::{name}" if target else name


class CallGraph:
    def __init__(self):
        self.nodes: Dict[str, dict] = {}   # node_id -> {label, kind, item_id}
        self.edges: Set[Tuple[str, str]] = set()

    def add_node(self, node_id: str, label: str, kind: str, item_id: Optional[int]):
        if node_id not in self.nodes:
            self.nodes[node_id] = {"label": label, "kind": kind, "item_id": item_id}

    def add_edge(self, src: str, dst: str):
        if src != dst:
            self.edges.add((src, dst))


def build_whole_graph(db: RustCodeDB, include_unresolved: bool = False,
                       kinds: Optional[Set[str]] = None) -> CallGraph:
    g = CallGraph()
    for row in db.all_call_edges():
        if not include_unresolved and row["callee_id"] is None:
            continue
        caller_kind = row["caller_kind"]
        if kinds and caller_kind not in kinds:
            continue
        src_id = _item_node_id(row["caller_id"])
        g.add_node(src_id, _node_label(row["caller_name"], row["caller_target"]), caller_kind, row["caller_id"])

        if row["callee_id"] is not None:
            dst_kind = row["callee_kind"]
            dst_id = _item_node_id(row["callee_id"])
            g.add_node(dst_id, _node_label(row["callee_name"], row["callee_target"]), dst_kind, row["callee_id"])
        else:
            dst_id = _external_node_id(row["callee_name"], row["receiver"], bool(row["is_method_call"]))
            g.add_node(dst_id, _node_label(row["callee_name"], row["receiver"] if not row["is_method_call"] else None),
                       "external", None)
        g.add_edge(src_id, dst_id)
    return g


def build_subgraph(db: RustCodeDB, root_item_ids: List[int], depth: int = 2,
                    direction: str = "both", include_unresolved: bool = True) -> CallGraph:
    """BFS out from the given root item id(s) following callee edges
    ('callees'), caller edges ('callers'), or both, up to `depth` hops."""
    g = CallGraph()

    def ensure_item_node(item_id: int):
        row = db.get_item(item_id)
        if row is None:
            return
        node_id = _item_node_id(item_id)
        g.add_node(node_id, _node_label(row["name"], row["target"]), row["kind"], item_id)

    visited = set()
    queue = deque((rid, 0) for rid in root_item_ids)
    for rid in root_item_ids:
        ensure_item_node(rid)
        visited.add(rid)

    while queue:
        item_id, d = queue.popleft()
        if d >= depth:
            continue
        row = db.get_item(item_id)
        if row is None:
            continue
        src_id = _item_node_id(item_id)

        if direction in ("callees", "both"):
            for call in db.get_calls_from(item_id):
                if call["callee_id"] is not None:
                    ensure_item_node(call["callee_id"])
                    dst_id = _item_node_id(call["callee_id"])
                    g.add_edge(src_id, dst_id)
                    if call["callee_id"] not in visited:
                        visited.add(call["callee_id"])
                        queue.append((call["callee_id"], d + 1))
                elif include_unresolved:
                    is_method = bool(call["is_method_call"])
                    dst_id = _external_node_id(call["callee_name"], call["receiver"], is_method)
                    g.add_node(dst_id, _node_label(call["callee_name"], call["receiver"] if not is_method else None),
                               "external", None)
                    g.add_edge(src_id, dst_id)

        if direction in ("callers", "both"):
            for call in db.get_calls_to(item_id):
                ensure_item_node(call["caller_id"])
                caller_node_id = _item_node_id(call["caller_id"])
                g.add_edge(caller_node_id, src_id)
                if call["caller_id"] not in visited:
                    visited.add(call["caller_id"])
                    queue.append((call["caller_id"], d + 1))

    return g


def _dot_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def to_dot(g: CallGraph, title: str = "Call Graph") -> str:
    lines = [
        "digraph callgraph {",
        f'  label="{_dot_escape(title)}";',
        "  labelloc=t;",
        "  fontsize=18;",
        "  rankdir=LR;",
        "  node [shape=box, style=\"rounded,filled\", fontname=\"Helvetica\", fontsize=10];",
        "  edge [color=\"#888888\", arrowsize=0.7];",
    ]
    for node_id, meta in g.nodes.items():
        color = NODE_COLORS.get(meta["kind"], "#CCCCCC")
        label = _dot_escape(meta["label"]) or "?"
        shape = "ellipse" if meta["kind"] == "external" else "box"
        lines.append(f'  "{node_id}" [label="{label}", fillcolor="{color}", shape={shape}, fontcolor="white"];')
    for src, dst in sorted(g.edges):
        lines.append(f'  "{src}" -> "{dst}";')
    lines.append("}")
    return "\n".join(lines)


def render_dot(dot_str: str, out_path: str, fmt: str) -> bool:
    """Render a DOT string to `fmt` (svg/png/pdf) via the `dot` binary.
    Returns True on success, False if graphviz isn't installed."""
    if shutil.which("dot") is None:
        return False
    proc = subprocess.run(
        ["dot", f"-T{fmt}", "-o", out_path],
        input=dot_str.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
    return True


VIS_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/vis-network.min.js"
        onerror="document.getElementById('graph').innerHTML =
          '<div style=\\'color:#eee;padding:24px;font-family:sans-serif\\'>' +
          'Could not load vis-network from the CDN (cdnjs.cloudflare.com). ' +
          'This page needs internet access in your browser to draw the graph. ' +
          'Use <code>--format svg</code> or <code>--format png</code> instead if you are offline.</div>'"></script>
<style>
  html, body {{ margin:0; height:100%; font-family: Helvetica, Arial, sans-serif; background:#1e1e1e; }}
  #header {{ padding: 10px 16px; color: #eee; background:#252526; border-bottom: 1px solid #333; }}
  #graph {{ width: 100%; height: calc(100% - 52px); background:#1e1e1e; }}
  .legend span {{ display:inline-block; margin-right:16px; }}
  .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:4px; }}
  #status {{ color:#999; font-size:12px; margin-left:12px; }}
</style>
</head>
<body>
<div id="header">
  <strong>{title}</strong>
  <span id="status">{node_count} nodes, {edge_count} edges</span>
  <div class="legend" style="margin-top:4px; font-size:12px;">
    <span><span class="dot" style="background:#4C72B0;"></span>function</span>
    <span><span class="dot" style="background:#55A868;"></span>method</span>
    <span><span class="dot" style="background:#B0B0B0;"></span>external/unresolved</span>
  </div>
</div>
<div id="graph"></div>
<script>
try {{
  const nodesData = {nodes_json};
  const edgesData = {edges_json};
  const nodes = new vis.DataSet(nodesData);
  const edges = new vis.DataSet(edgesData);
  const container = document.getElementById('graph');
  const data = {{ nodes, edges }};
  const nodeCount = nodesData.length;

  // Large graphs: use a cheaper solver and stop physics as soon as it
  // stabilizes (or after a bounded number of iterations) so the view
  // doesn't sit spinning forever on a big project.
  const big = nodeCount > 400;
  const options = {{
    nodes: {{ shape: 'box', margin: 8, font: {{ color: '#fff', size: 13 }}, borderWidth: 0 }},
    edges: {{ arrows: 'to', color: {{ color: '#666', highlight: '#fff' }}, smooth: {{ type: 'dynamic' }} }},
    physics: {{
      solver: big ? 'barnesHut' : 'forceAtlas2Based',
      stabilization: {{ enabled: true, iterations: big ? 150 : 250, fit: true }}
    }},
    interaction: {{ hover: true, tooltipDelay: 100 }}
  }};

  const network = new vis.Network(container, data, options);
  const statusEl = document.getElementById('status');

  network.once('stabilizationIterationsDone', function () {{
    network.setOptions({{ physics: false }});
    network.fit();
    statusEl.textContent = nodeCount + ' nodes, ' + edgesData.length + ' edges (layout complete, physics off)';
  }});

  // Safety net: even if stabilization never fully completes on a huge
  // graph, force it to stop spinning and fit the view after a timeout.
  setTimeout(function () {{
    network.setOptions({{ physics: false }});
    network.fit();
  }}, 8000);
}} catch (err) {{
  document.getElementById('graph').innerHTML =
    '<div style="color:#f66;padding:24px;font-family:monospace;white-space:pre-wrap">' +
    'Error rendering graph: ' + err + '</div>';
  console.error(err);
}}
</script>
</body>
</html>
"""


def to_html(g: CallGraph, title: str = "Call Graph") -> str:
    nodes = [
        {
            "id": nid,
            "label": meta["label"] or "?",
            "color": NODE_COLORS.get(meta["kind"], "#CCCCCC"),
            "shape": "ellipse" if meta["kind"] == "external" else "box",
            "title": f"{meta['kind']}: {meta['label']}",
        }
        for nid, meta in g.nodes.items()
    ]
    edges = [{"from": s, "to": d} for s, d in sorted(g.edges)]
    return VIS_HTML_TEMPLATE.format(
        title=title,
        node_count=len(nodes),
        edge_count=len(edges),
        nodes_json=json.dumps(nodes),
        edges_json=json.dumps(edges),
    )
