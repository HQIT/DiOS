import { useEffect, useState } from "react";
import type { Connector, ConnectorConfigProperty, ConnectorType } from "../../../types";
import { api } from "../../../api/os";
import Drawer from "../../../components/Drawer";

function findConnector(connectors: Connector[], manifest: ConnectorType): Connector | undefined {
  return connectors.find(
    (connector) => connector.type === manifest.type || manifest.aliases.includes(connector.type),
  );
}

function defaultConfig(manifest: ConnectorType): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  for (const [name, property] of Object.entries(manifest.config_schema.properties ?? {})) {
    if (property.default !== undefined) config[name] = property.default;
    else if (property.enum?.length) config[name] = property.enum[0];
    else if (property.type === "boolean") config[name] = false;
    else config[name] = "";
  }
  return config;
}

function needsConfiguration(manifest: ConnectorType, config: Record<string, unknown>): boolean {
  return (manifest.config_schema.required ?? []).some((name) => {
    const value = config[name];
    return value === undefined || value === null || value === "";
  });
}

function ConfigField({
  name,
  property,
  required,
  value,
  onChange,
}: {
  name: string;
  property: ConnectorConfigProperty;
  required: boolean;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const label = `${property.title ?? name}${required ? " *" : ""}`;
  let control;

  if (property.enum?.length) {
    control = (
      <select value={String(value ?? "")} onChange={(event) => onChange(event.target.value)}>
        {property.enum.map((option) => (
          <option key={String(option)} value={String(option)}>{String(option)}</option>
        ))}
      </select>
    );
  } else if (property.type === "boolean") {
    control = (
      <input
        type="checkbox"
        checked={Boolean(value)}
        onChange={(event) => onChange(event.target.checked)}
      />
    );
  } else if (property.type === "integer" || property.type === "number") {
    control = (
      <input
        type="number"
        value={typeof value === "number" ? value : ""}
        onChange={(event) => onChange(event.target.value === "" ? "" : Number(event.target.value))}
      />
    );
  } else {
    control = (
      <input
        type={property.writeOnly ? "password" : "text"}
        value={String(value ?? "")}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }

  return (
    <>
      <label>{label}</label>
      {control}
      {property.description && (
        <p className="text-muted" style={{ fontSize: 12 }}>{property.description}</p>
      )}
    </>
  );
}

export default function ConnectorsPage() {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [connectorTypes, setConnectorTypes] = useState<ConnectorType[]>([]);
  const [editType, setEditType] = useState<string | null>(null);
  const [localConfig, setLocalConfig] = useState<Record<string, unknown>>({});
  const [error, setError] = useState("");

  const load = async () => {
    try {
      const [instances, manifests] = await Promise.all([
        api.listConnectors(),
        api.listConnectorTypes(),
      ]);
      setConnectors(instances);
      setConnectorTypes(manifests);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Connector 加载失败");
    }
  };

  useEffect(() => {
    let active = true;
    Promise.all([api.listConnectors(), api.listConnectorTypes()])
      .then(([instances, manifests]) => {
        if (!active) return;
        setConnectors(instances);
        setConnectorTypes(manifests);
        setError("");
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Connector 加载失败");
      });
    return () => { active = false; };
  }, []);

  const editManifest = connectorTypes.find((manifest) => manifest.type === editType);

  const openConfig = (manifest: ConnectorType) => {
    const existing = findConnector(connectors, manifest);
    const defaults = defaultConfig(manifest);
    if (existing && manifest.aliases.includes(existing.type) && "platform" in defaults) {
      defaults.platform = existing.type;
    }
    setLocalConfig({ ...defaults, ...(existing?.config ?? {}) });
    setError("");
    setEditType(manifest.type);
  };

  const toggleEnabled = async (manifest: ConnectorType) => {
    const existing = findConnector(connectors, manifest);
    const defaults = defaultConfig(manifest);
    if (!existing && needsConfiguration(manifest, defaults)) {
      openConfig(manifest);
      return;
    }
    try {
      if (existing) {
        await api.updateConnector(existing.id, { enabled: !existing.enabled });
      } else {
        await api.createConnector({
          type: manifest.type,
          name: manifest.label,
          enabled: true,
          config: defaults,
        });
      }
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Connector 更新失败");
    }
  };

  const saveConfig = async () => {
    if (!editManifest) return;
    try {
      const existing = findConnector(connectors, editManifest);
      if (existing) {
        await api.updateConnector(existing.id, { config: localConfig });
      } else {
        await api.createConnector({
          type: editManifest.type,
          name: editManifest.label,
          enabled: true,
          config: localConfig,
        });
      }
      setEditType(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Connector 保存失败");
    }
  };

  const webhookSource = editManifest?.capabilities.includes("webhook")
    ? String(localConfig.platform || editManifest.type)
    : "";

  return (
    <div className="panel">
      <p className="text-muted" style={{ marginBottom: 12 }}>
        事件源负责将外部事件接入 DiOS，用于触发 Agent。类型和配置项由 Connector Manifest 提供。
      </p>

      {error && <p style={{ color: "var(--color-danger)", marginBottom: 12 }}>{error}</p>}

      <div className="card-grid">
        {connectorTypes.map((manifest) => {
          const connector = findConnector(connectors, manifest);
          const enabled = connector?.enabled ?? false;
          return (
            <div key={manifest.type} className={`entity-card ${enabled ? "" : "connector-disabled"}`}>
              <div className="entity-card-header">
                <span className="entity-card-name">{manifest.label}</span>
                <label className="sub-toggle" onClick={(event) => event.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={() => { void toggleEnabled(manifest); }}
                  />
                  <span>{enabled ? "已启用" : "未启用"}</span>
                </label>
              </div>
              <p className="entity-card-desc">{manifest.description}</p>
              <div className="entity-card-actions">
                <button className="btn-sm btn-secondary" onClick={() => openConfig(manifest)}>配置</button>
              </div>
            </div>
          );
        })}
      </div>

      <Drawer
        open={!!editManifest}
        title={`配置 - ${editManifest?.label ?? ""}`}
        onClose={() => setEditType(null)}
      >
        {editManifest && (
          <div className="drawer-form">
            {Object.entries(editManifest.config_schema.properties ?? {}).map(([name, property]) => (
              <ConfigField
                key={name}
                name={name}
                property={property}
                required={(editManifest.config_schema.required ?? []).includes(name)}
                value={localConfig[name]}
                onChange={(value) => setLocalConfig((current) => ({ ...current, [name]: value }))}
              />
            ))}

            {webhookSource && (
              <>
                <label>Webhook 路径</label>
                <input
                  readOnly
                  value={`/api/os/events/webhook/${webhookSource}`}
                  onClick={(event) => event.currentTarget.select()}
                  style={{ color: "var(--color-info)", cursor: "pointer" }}
                />
              </>
            )}

            {error && <p style={{ color: "var(--color-danger)" }}>{error}</p>}
            <div className="drawer-actions">
              <button onClick={() => { void saveConfig(); }}>保存</button>
              <button className="btn-secondary" onClick={() => setEditType(null)}>取消</button>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
