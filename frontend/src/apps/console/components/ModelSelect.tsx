import { useEffect, useState } from "react";
import type { LLMModel } from "../../../types";
import { api } from "../../../api/os";

interface Props {
  value: string;
  onChange: (name: string) => void;
  allowEmpty?: boolean;
  emptyLabel?: string;
}

export default function ModelSelect({ value, onChange, allowEmpty = true, emptyLabel = "默认" }: Props) {
  const [models, setModels] = useState<LLMModel[]>([]);
  useEffect(() => { api.listModels().then(setModels); }, []);

  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      {allowEmpty && <option value="">{emptyLabel}</option>}
      {models.map((m) => (
        <option key={m.id} value={m.name}>{m.display_name || m.name}</option>
      ))}
    </select>
  );
}
