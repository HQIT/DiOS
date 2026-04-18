import { useEffect, useState, useCallback, useRef } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  useNodesState,
  useEdgesState,
  Handle,
  Position,
  type Node,
  type Edge,
  type NodeProps,
  MarkerType,
  Panel,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import dagre from "@dagrejs/dagre";
import type { Agent, Connector, ConnectorSourcePattern, EventCatalogType, Subscription } from "../../../types";
import { api } from "../../../api/os";
import Drawer from "../../../components/Drawer";

/* ─── Constants ─── */

const NODE_WIDTH = 220;
const NODE_HEIGHT = 100;

/* ─── Dagre Layout ─── */

function getLayoutedElements(
  nodes: Node[],
  edges: Edge[],
): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "LR", nodesep: 60, ranksep: 180 });

  nodes.forEach((n) => g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  edges.forEach((e) => g.setEdge(e.source, e.target));
  dagre.layout(g);

  const positioned = nodes.map((n) => {
    const pos = g.node(n.id);
    return { ...n, position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 } };
  });
  return { nodes: positioned, edges };
}

/* ─── Custom Nodes ─── */

function ConnectorNode({ data }: NodeProps) {
  const d = data as { label: string; type: string; enabled: boolean };
  return (
    <div className={`topo-node topo-connector ${d.enabled ? "" : "topo-disabled"}`}>
      <div className="topo-node-badge">{d.type}</div>
      <div className="topo-node-label">{d.label}</div>
      <div className="topo-node-status">{d.enabled ? "● Enabled" : "○ Disabled"}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function AgentNode({ data }: NodeProps) {
  const d = data as { label: string; group: string; model: string; skillCount: number; subCount: number };
  return (
    <div className="topo-node topo-agent">
      <div className="topo-node-header">
        <span className="topo-node-label">{d.label}</span>
        {d.group && <span className="topo-node-tag">{d.group}</span>}
      </div>
      <div className="topo-node-meta">
        {d.model && <span>{d.model}</span>}
        {d.skillCount > 0 && <span>Skills: {d.skillCount}</span>}
      </div>
      <Handle type="target" position={Position.Left} />
    </div>
  );
}

const nodeTypes = { connector: ConnectorNode, agent: AgentNode };

/* ─── Match source_pattern to connector ─── */

function isPatternInNamespace(sourcePattern: string, namespacePattern: string): boolean {
  const ns = namespacePattern.replace(/\/\*$/, "");
  return sourcePattern === namespacePattern || sourcePattern === ns || sourcePattern.startsWith(`${ns}/`);
}

function matchConnector(
  sourcePattern: string,
  connectors: Connector[],
  sourceOptions: ConnectorSourcePattern[],
): Connector | undefined {
  const option = sourceOptions.find(
    (o) => !!o.connector_id && isPatternInNamespace(sourcePattern, o.source_pattern),
  );
  if (!option?.connector_id) return undefined;
  return connectors.find((c) => c.id === option.connector_id);
}

function eventOptionsForSource(
  sourcePattern: string,
  sourceOptions: ConnectorSourcePattern[],
  allEventTypes: EventCatalogType[],
): { value: string; label: string }[] {
  const matched = sourceOptions.find((o) => isPatternInNamespace(sourcePattern, o.source_pattern));
  const allowed = new Set(matched?.event_types || allEventTypes.map((e) => e.type));
  return allEventTypes
    .filter((e) => allowed.has(e.type))
    .map((e) => ({ value: e.type, label: e.description || e.type }));
}

/* ─── Subscription View ─── */

function SubscriptionDetail({
  sub,
  agents,
  connectors,
  sourceOptions,
  allEventTypes,
}: {
  sub: Subscription;
  agents: Agent[];
  connectors: Connector[];
  sourceOptions: ConnectorSourcePattern[];
  allEventTypes: EventCatalogType[];
}) {
  const agent = agents.find((a) => a.id === sub.agent_id);
  const conn = matchConnector(sub.source_pattern, connectors, sourceOptions);
  const eventOptions = eventOptionsForSource(sub.source_pattern, sourceOptions, allEventTypes);
  const labelMap = new Map(eventOptions.map((o) => [o.value, o.label]));

  return (
    <>
      <div className="topo-detail-body">
        <div className="topo-detail-row">
          <span className="topo-detail-label">Source</span>
          <span>{conn?.name || sub.source_pattern}</span>
        </div>
        <div className="topo-detail-row">
          <span className="topo-detail-label">Agent</span>
          <span>{agent?.name || sub.agent_id}</span>
        </div>
        <div className="topo-detail-row">
          <span className="topo-detail-label">Pattern</span>
          <span className="mono">{sub.source_pattern}</span>
        </div>
        <div className="topo-detail-row">
          <span className="topo-detail-label">Status</span>
          <span>{sub.enabled ? "Enabled" : "Disabled"}</span>
        </div>
        <label style={{ marginTop: 12 }}>Event Types</label>
        <div className="event-type-grid">
          {sub.event_types.map((ev) => (
            <span key={ev} className="event-type-badge">
              {labelMap.get(ev) || ev}
            </span>
          ))}
        </div>
      </div>
    </>
  );
}

/* ─── Detail Panels ─── */

function ConnectorDetail({ connector }: { connector: Connector }) {
  return (
    <div className="topo-detail-body">
      <div className="topo-detail-row"><span className="topo-detail-label">Name</span><span>{connector.name}</span></div>
      <div className="topo-detail-row"><span className="topo-detail-label">Type</span><span className="topo-node-badge" style={{ display: "inline-block" }}>{connector.type}</span></div>
      <div className="topo-detail-row"><span className="topo-detail-label">Status</span><span>{connector.enabled ? "✓ Enabled" : "✗ Disabled"}</span></div>
      <div className="topo-detail-row"><span className="topo-detail-label">Created</span><span>{new Date(connector.created_at).toLocaleString()}</span></div>
    </div>
  );
}

function AgentDetail({ agent, subCount }: { agent: Agent; subCount: number }) {
  return (
    <div className="topo-detail-body">
      <div className="topo-detail-row"><span className="topo-detail-label">Name</span><span>{agent.name}</span></div>
      {agent.group && <div className="topo-detail-row"><span className="topo-detail-label">Group</span><span>{agent.group}</span></div>}
      {agent.model && <div className="topo-detail-row"><span className="topo-detail-label">Model</span><span className="mono">{agent.model}</span></div>}
      {agent.description && <div className="topo-detail-row"><span className="topo-detail-label">Description</span><span>{agent.description}</span></div>}
      <div className="topo-detail-row"><span className="topo-detail-label">Skills</span><span>{agent.skills?.length || 0}</span></div>
      <div className="topo-detail-row"><span className="topo-detail-label">Subscriptions</span><span>{subCount}</span></div>
      <div className="topo-detail-row"><span className="topo-detail-label">Created</span><span>{new Date(agent.created_at).toLocaleString()}</span></div>
    </div>
  );
}

/* ─── Main Component ─── */

type PanelState =
  | { kind: "none" }
  | { kind: "connector"; connector: Connector }
  | { kind: "agent"; agent: Agent; subCount: number }
  | { kind: "sub-view"; sub: Subscription };

export default function TopologyPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [sourceOptions, setSourceOptions] = useState<ConnectorSourcePattern[]>([]);
  const [allEventTypes, setAllEventTypes] = useState<EventCatalogType[]>([]);
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [loading, setLoading] = useState(true);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [panel, setPanel] = useState<PanelState>({ kind: "none" });
  const dataRef = useRef({ agents: [] as Agent[], connectors: [] as Connector[], subscriptions: [] as Subscription[] });

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [a, c, s, so, catalog] = await Promise.all([
        api.listAgents(),
        api.listConnectors(),
        api.listAllSubscriptions(),
        api.listConnectorSourcePatterns(),
        api.getEventCatalog(),
      ]);
      setAgents(a);
      setConnectors(c);
      setSubscriptions(s);
      setSourceOptions(so);
      setAllEventTypes(catalog.event_types || []);
      dataRef.current = { agents: a, connectors: c, subscriptions: s };
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // Build graph whenever data changes
  useEffect(() => {
    if (loading) return;

    const rawNodes: Node[] = [];
    const rawEdges: Edge[] = [];

    // Connector nodes (left side)
    connectors.forEach((c) => {
      rawNodes.push({
        id: `conn-${c.id}`,
        type: "connector",
        position: { x: 0, y: 0 },
        data: { label: c.name, type: c.type, enabled: c.enabled, _id: c.id },
      });
    });

    // Agent nodes (right side)
    agents.forEach((a) => {
      const subCount = subscriptions.filter((s) => s.agent_id === a.id).length;
      rawNodes.push({
        id: `agent-${a.id}`,
        type: "agent",
        position: { x: 0, y: 0 },
        data: {
          label: a.name,
          group: a.group,
          model: a.model,
          skillCount: a.skills?.length || 0,
          subCount,
          _id: a.id,
        },
      });
    });

    // Edges from subscriptions
    subscriptions.forEach((sub) => {
      const conn = matchConnector(sub.source_pattern, connectors, sourceOptions);
      const sourceId = conn ? `conn-${conn.id}` : null;
      const targetId = `agent-${sub.agent_id}`;

      // If source_pattern=* create edges to a virtual "any" node, or if matched
      if (sourceId) {
        rawEdges.push({
          id: `sub-${sub.id}`,
          source: sourceId,
          target: targetId,
          label: sub.event_types.length <= 2 ? sub.event_types.join(", ") : `${sub.event_types.length} types`,
          animated: sub.enabled,
          style: { stroke: sub.enabled ? "var(--color-primary)" : "var(--color-muted)", strokeWidth: 2 },
          markerEnd: { type: MarkerType.ArrowClosed, color: sub.enabled ? "var(--color-primary)" : "var(--color-muted)" },
          data: { _subId: sub.id },
        });
      } else {
        // Create a virtual node for unmatched source patterns
        const virtualId = `virtual-${sub.source_pattern}`;
        if (!rawNodes.find((n) => n.id === virtualId)) {
          rawNodes.push({
            id: virtualId,
            type: "connector",
            position: { x: 0, y: 0 },
            data: { label: sub.source_pattern, type: "pattern", enabled: true, _id: null },
          });
        }
        rawEdges.push({
          id: `sub-${sub.id}`,
          source: virtualId,
          target: targetId,
          label: sub.event_types.length <= 2 ? sub.event_types.join(", ") : `${sub.event_types.length} types`,
          animated: sub.enabled,
          style: { stroke: sub.enabled ? "var(--color-primary)" : "var(--color-muted)", strokeWidth: 2 },
          markerEnd: { type: MarkerType.ArrowClosed, color: sub.enabled ? "var(--color-primary)" : "var(--color-muted)" },
          data: { _subId: sub.id },
        });
      }
    });

    const layout = getLayoutedElements(rawNodes, rawEdges);
    setNodes(layout.nodes);
    setEdges(layout.edges);
  }, [agents, connectors, sourceOptions, subscriptions, loading, setNodes, setEdges]);

  // Click node => detail panel
  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    const { agents, connectors, subscriptions } = dataRef.current;
    if (node.type === "connector") {
      const connId = (node.data as Record<string, unknown>)._id as string | null;
      const conn = connectors.find((c) => c.id === connId);
      if (conn) setPanel({ kind: "connector", connector: conn });
    } else if (node.type === "agent") {
      const agentId = (node.data as Record<string, unknown>)._id as string;
      const agent = agents.find((a) => a.id === agentId);
      const subCount = subscriptions.filter((s) => s.agent_id === agentId).length;
      if (agent) setPanel({ kind: "agent", agent, subCount });
    }
  }, []);

  // Click edge => edit subscription
  const onEdgeClick = useCallback((_: React.MouseEvent, edge: Edge) => {
    const subId = (edge.data as Record<string, unknown>)?._subId as string;
    const sub = dataRef.current.subscriptions.find((s) => s.id === subId);
    if (sub) setPanel({ kind: "sub-view", sub });
  }, []);

  const closePanel = useCallback(() => setPanel({ kind: "none" }), []);

  const empty = !loading && agents.length === 0 && connectors.length === 0;

  return (
    <div className="topo-container">
      {loading && (
        <div className="topo-loading">
          <div className="spinner" />
          <span>Loading topology...</span>
        </div>
      )}

      {empty && (
        <div className="topo-empty">
          <p>No Agents or Connectors found</p>
          <p style={{ fontSize: 13, color: "var(--text-secondary)" }}>
            Add data on the Agents and Connectors pages first
          </p>
        </div>
      )}

      {!loading && !empty && (
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={onNodeClick}
          onEdgeClick={onEdgeClick}
          nodesConnectable={false}
          nodeTypes={nodeTypes}
          fitView
          proOptions={{ hideAttribution: true }}
          style={{ background: "var(--bg)" }}
        >
          <Background color="var(--border)" gap={24} />
          <Controls
            showInteractive={false}
            style={{ background: "var(--bg-surface)", borderColor: "var(--border)" }}
          />
          <Panel position="top-right">
            <button className="btn-sm" onClick={loadData} style={{ background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
              ↻ Refresh
            </button>
          </Panel>
        </ReactFlow>
      )}

      {/* Detail / Edit Drawers */}
      <Drawer open={panel.kind === "connector"} title="Connector Details" onClose={closePanel}>
        {panel.kind === "connector" && <ConnectorDetail connector={panel.connector} />}
      </Drawer>
      <Drawer open={panel.kind === "agent"} title="Agent Details" onClose={closePanel}>
        {panel.kind === "agent" && <AgentDetail agent={panel.agent} subCount={panel.subCount} />}
      </Drawer>
      <Drawer open={panel.kind === "sub-view"} title="Subscription Details" onClose={closePanel}>
        {panel.kind === "sub-view" && (
          <SubscriptionDetail
            sub={panel.sub}
            agents={agents}
            connectors={connectors}
            sourceOptions={sourceOptions}
            allEventTypes={allEventTypes}
          />
        )}
      </Drawer>
    </div>
  );
}
